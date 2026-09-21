"""Free, conservative coursework extraction from retained text and slide OCR."""
from __future__ import annotations
from datetime import date
import hashlib
import re
from sqlalchemy import select
from .assignment_models import Assignment
from .models import Note,User
from .slide_models import SlideDeck,SlidePage

_MARKER=re.compile(r'(?im)^\s*(?:#{1,6}\s*)?(?:homework|assignments?|课后作业|本次作业|作业(?:要求|任务)?)(?=\s|[:：（(\d]|$)|(?:这次|本次|今天|本周)(?:的)?作业(?:是|要求|[:：])')
_ACTION=re.compile(r'提交|完成|撰写|写出|计算|构建|建立|设计|制作|调研|整理|查找|阅读|实现|列出|收集|比较|至少|\b(?:submit|complete|build|design|prepare|create|write|find|compare|construct|implement|read|calculate|list|at least)\b',re.I)
_DUE=re.compile(r'(?:ddl|deadline|due(?:\s+date)?|截止(?:日期|时间)?|最晚|提交(?:日期|时间)?)[\s:：为是]*',re.I)
_POLICY_HEADING=re.compile(r'^(?:#{1,6}\s*)?(?:(?:homework|assignments?)\s+(?:polic(?:y|ies)|rules|grading|submission\s+(?:polic(?:y|ies)|guidelines|instructions))|作业(?:提交形式|提交规范|评分标准|评分规则|管理规定|政策))\b',re.I)


def deadline(text: str, reference: date):
    # Month/day without a year uses the nearest occurrence to this source's
    # recorded date. Relative phrases alone remain unconfirmed.
    for label in _DUE.finditer(text):
        tail=text[label.end():label.end()+100]
        match=re.search(r'(?<!\d)(?:(20\d{2})\s*[年/.-]\s*)?(\d{1,2})\s*[月/.-]\s*(\d{1,2})(?:\s*日)?(?:\s*(\d{1,2})[:：](\d{2}))?',tail)
        if not match: continue
        year,month,day,hour,minute=match.groups()
        candidates=[]
        for y in ([int(year)] if year else [reference.year-1,reference.year,reference.year+1]):
            try: candidates.append(date(y,int(month),int(day)))
            except ValueError: continue
        if not candidates: continue
        found=min(candidates,key=lambda d:abs((d-reference).days))
        clock=None
        if hour is not None and ((0<=int(hour)<=23 and 0<=int(minute)<=59) or (int(hour)==24 and int(minute)==0)):
            clock=f'{int(hour):02d}:{int(minute):02d}'
        return found.isoformat(),clock
    return None,None


def extract_assignments(text: str, reference: date, *, continuation=False) -> list[dict]:
    text=text.replace('\r\n','\n').replace('\r','\n')
    matches=list(_MARKER.finditer(text))
    if not matches and continuation:
        # Only an explicitly numbered next item immediately after a homework
        # page qualifies; an ordinary following lecture slide does not.
        first=re.sub(r'^\s*\d{1,3}\s*\n','',text).lstrip()
        if not re.match(r'^(?:[（(]?[2-9][.)）、]|[②-⑨])\s*',first): return []
        blocks=[first]
    else:
        blocks=[]
        for i,match in enumerate(matches):
            end=matches[i+1].start() if i+1<len(matches) else len(text)
            block=text[match.start():end]
            # Stop at the next Markdown heading or several unrelated prose
            # paragraphs; transcripts are bounded to this assignment context.
            stop=re.search(r'\n#{1,6}\s+',block[1:])
            if stop: block=block[:stop.start()+1]
            blocks.append(block[:2400])
    found=[]
    for block in blocks:
        block=block.strip()
        if len(block)<25 or not _ACTION.search(block): continue
        # Submission/grading policies describe every future assignment. Their
        # generic "write answers / submit code" verbs are not a concrete task.
        if _POLICY_HEADING.search(block): continue
        if re.search(r'不(?:布置|需要交|留)作业|没有作业|no homework',block,re.I): continue
        lines=[re.sub(r'^\s*[•●·\-*\d.、()（）]+\s*','',s).strip() for s in block.splitlines() if s.strip()]
        meaningful=[s for s in lines if _ACTION.search(s) and len(s)>8]
        title=(meaningful[0] if meaningful else '课后作业')[:150]
        due,clock=deadline(block,reference)
        found.append({'title':title,'details':block,'sourceExcerpt':block,'dueDate':due,'dueTime':clock})
    return found[:12]


def _upsert(db,*,user_id,course_id,key,source_type,note_id=None,deck_id=None,page_number=None,value):
    row=db.scalar(select(Assignment).where(Assignment.user_id==user_id,Assignment.source_key==key))
    if row is None:
        row=Assignment(user_id=user_id,source_key=key,source_type=source_type,note_id=note_id,deck_id=deck_id,page_number=page_number,
                       course_id=course_id,title=value['title'],details=value['details'],source_excerpt=value['sourceExcerpt'],
                       due_date=value['dueDate'],due_time=value['dueTime'],status='open',user_edited=False)
        db.add(row);db.flush()
    else:
        row.course_id=course_id
        # A completion or manual date/title correction survives every rescan.
        if not row.user_edited:
            row.title,row.details,row.source_excerpt=value['title'],value['details'],value['sourceExcerpt']
            row.due_date,row.due_time=value['dueDate'],value['dueTime']
    return row


def scan_note_assignments(db,note):
    if note.deleted_at is not None or not note.transcript.strip(): return 0
    db.scalar(select(User).where(User.id==note.user_id).with_for_update())
    reference=(note.uploaded_at or note.created_at).date()
    count=0
    for value in extract_assignments(note.transcript,reference):
        fingerprint=hashlib.sha256(re.sub(r'\s+','',value['sourceExcerpt']).encode()).hexdigest()[:32]
        _upsert(db,user_id=note.user_id,course_id=note.course_id,key=f'note:{note.id}:{fingerprint}',source_type='note',note_id=note.id,value=value);count+=1
    return count


def scan_deck_assignments(db,deck):
    if deck.status!='ready': return 0
    db.scalar(select(User).where(User.id==deck.user_id).with_for_update())
    previous=None;previous_number=None;previous_item=None;count=0
    for page in db.scalars(select(SlidePage).where(SlidePage.deck_id==deck.id).order_by(SlidePage.number)):
        numbered=re.findall(r'(?m)^\s*[（(]?([1-9])[.)）、]\s*',page.text)
        current_item=int(numbered[0]) if numbered else None
        continuation=(previous is not None and previous_number==page.number-1 and previous_item is not None and current_item==previous_item+1)
        values=extract_assignments(page.text,deck.created_at.date(),continuation=continuation)
        for index,value in enumerate(values):
            continued_item=not _MARKER.search(page.text) or re.search(r'(?m)^\s*(?:[（(]?[2-9][.)）、]|[②-⑨])\s*',page.text)
            if continuation and continued_item and not value['dueDate']:
                value['dueDate'],value['dueTime']=previous['dueDate'],previous['dueTime']
            _upsert(db,user_id=deck.user_id,course_id=deck.course_id,key=f'slide:{deck.id}:{page.number}:{index}',source_type='slide',
                    deck_id=deck.id,note_id=deck.note_id,page_number=page.number,value=value);count+=1
        previous=values[-1] if values else None;previous_number=page.number
        previous_item=int(numbered[-1]) if values and numbered else None
    return count


def scan_user_assignments(db,user_id):
    # Serialize scans for one user; source-key uniqueness is a second guard.
    db.scalar(select(User).where(User.id==user_id).with_for_update())
    count=0
    for note in db.scalars(select(Note).where(Note.user_id==user_id,Note.deleted_at.is_(None))): count+=scan_note_assignments(db,note)
    for deck in db.scalars(select(SlideDeck).where(SlideDeck.user_id==user_id,SlideDeck.status=='ready')): count+=scan_deck_assignments(db,deck)
    return count


def assignment_json(row):
    source=f'/?deck={row.deck_id}&page={row.page_number}' if row.source_type=='slide' else f'/?note={row.note_id}'
    return {'id':row.id,'courseId':row.course_id,'title':row.title,'details':row.details,'dueDate':row.due_date,'dueTime':row.due_time,
            'status':row.status,'needsDateConfirmation':not bool(row.due_date),'sourceType':row.source_type,'noteId':row.note_id,
            'deckId':row.deck_id,'pageNumber':row.page_number,'sourceUrl':source,'sourceExcerpt':row.source_excerpt,'userEdited':row.user_edited}


def assignments_json(db,user_id):
    return [assignment_json(row) for row in db.scalars(select(Assignment).where(Assignment.user_id==user_id).order_by(Assignment.due_date,Assignment.created_at))
            if not row.note_id or row.source_type=='slide' or ((note:=db.get(Note,row.note_id)) is not None and note.deleted_at is None)]
