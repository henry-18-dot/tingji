"""School settings and free official-calendar import."""
from datetime import date
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import require_csrf,require_user
from .database import get_db
from .models import User,utcnow
from .school_models import SchoolSettings
from .schools import school_json,school_calendar,official_domain,source_url,OfficialClient,source_text,parse_calendar_text,is_sustech
from .timetable_models import TimetableState

router=APIRouter(prefix='/api/school',tags=['school'])


class SchoolPatch(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schoolName: str|None=Field(default=None,min_length=1,max_length=120)
    officialDomain: str|None=Field(default=None,max_length=253)
    calendarUrl: str|None=Field(default=None,max_length=1800)
    syllabusUrl: str|None=Field(default=None,max_length=1800)
    semesterStart: str|None=None
    semesterEnd: str|None=None


def _date(value,monday=False):
    if not value:return None
    try:parsed=date.fromisoformat(value)
    except (ValueError,TypeError):raise ValueError('日期格式应为 YYYY-MM-DD。') from None
    if monday and parsed.weekday()!=0:raise ValueError('第一教学周请选择周一。')
    return parsed.isoformat()


@router.get('')
def get_school(user:User=Depends(require_user),db:Session=Depends(get_db)):
    return school_json(db,user.id)


@router.patch('',dependencies=[Depends(require_csrf)])
def patch_school(body:SchoolPatch,user:User=Depends(require_user),db:Session=Depends(get_db)):
    db.scalar(select(User).where(User.id==user.id).with_for_update())
    previous=school_json(db,user.id);values={**previous,**body.model_dump(exclude_unset=True)}
    if not values['schoolName'] or not values['schoolName'].strip():raise ValueError('请填写学校名称。')
    values['officialDomain']=official_domain(values['officialDomain'] or '')
    changed=(values['officialDomain']!=previous['officialDomain'] or values['schoolName']!=previous['schoolName'])
    if changed:
        for key in ('calendarUrl','syllabusUrl','semesterStart','semesterEnd'):
            if key not in body.model_fields_set:values[key]=None if key.startswith('semester') else ''
    for key in ('calendarUrl','syllabusUrl'):values[key]=source_url(values[key] or '',values['officialDomain'])
    start,end=_date(values['semesterStart'],True),_date(values['semesterEnd'])
    if start and end and end<start:raise ValueError('学期结束日期不能早于第一周。')
    row=db.get(SchoolSettings,user.id)
    if row is None:row=SchoolSettings(user_id=user.id);db.add(row)
    row.name,row.official_domain=values['schoolName'].strip(),values['officialDomain']
    row.calendar_url,row.syllabus_url=values['calendarUrl'],values['syllabusUrl']
    row.semester_start,row.semester_end=start,end
    if changed or row.calendar_url!=previous['calendarUrl']:row.calendar={};row.source_excerpt='';row.checked_at=None
    if changed or 'semesterStart' in body.model_fields_set:
        state=db.get(TimetableState,user.id)
        if state is None:state=TimetableState(user_id=user.id,weeks=20,revision=0);db.add(state)
        state.semester_start=start;state.revision+=1
    if changed or row.syllabus_url!=previous['syllabusUrl']:
        from .syllabus_models import CourseSyllabusJob
        for job in db.scalars(select(CourseSyllabusJob).where(CourseSyllabusJob.user_id==user.id)):
            job.status,job.error,job.source_url,job.course_code='queued','','',''
            job.attempts,job.available_at,job.checked_at=0,utcnow(),None
    db.commit()
    return school_json(db,user.id)


@router.post('/refresh',dependencies=[Depends(require_csrf)])
def refresh_calendar(user:User=Depends(require_user),db:Session=Depends(get_db)):
    settings=school_json(db,user.id)
    if not settings['calendarUrl']:raise HTTPException(400,'请填写学校官网校历链接。')
    try:
        with OfficialClient(settings['officialDomain']) as client:raw,url=client.get(settings['calendarUrl'],8*1024*1024)
        text=source_text(raw,url)
    except (ValueError,OSError) as exc:raise HTTPException(400,str(exc)) from None
    db.scalar(select(User).where(User.id==user.id).with_for_update());db.expire_all()
    if school_json(db,user.id)!=settings:raise HTTPException(409,'学校设置已更改，请重新读取。')
    parsed=parse_calendar_text(text)
    row=db.get(SchoolSettings,user.id)
    if row is None:
        row=SchoolSettings(user_id=user.id,name=settings['schoolName'],official_domain=settings['officialDomain'],calendar_url=settings['calendarUrl'],syllabus_url=settings['syllabusUrl']);db.add(row)
    # The default PDF grid was visually confirmed. General grid layouts never
    # borrow its dates; unambiguous textual labels may populate other schools.
    if is_sustech(db,user.id) and url in {'https://sustech.edu.cn/uploads/files/2025/11/25155108_33786.pdf','https://www.sustech.edu.cn/uploads/files/2025/11/25155108_33786.pdf'}:
        from .academic_calendar import AUTUMN_2026
        parsed=dict(AUTUMN_2026)
    row.calendar={**parsed,'sourceUrl':url}
    if not row.semester_start and parsed.get('semesterStart'):row.semester_start=parsed['semesterStart']
    if not row.semester_end and parsed.get('end'):row.semester_end=parsed['end']
    row.source_excerpt=text[:12000];row.checked_at=utcnow()
    state=db.get(TimetableState,user.id)
    if row.semester_start and (state is None or not state.semester_start):
        if state is None:state=TimetableState(user_id=user.id,weeks=20,revision=0);db.add(state)
        state.semester_start=row.semester_start;state.revision+=1
    db.commit()
    return {'school':school_json(db,user.id),'academicCalendar':school_calendar(db,user.id,state.semester_start if state else None),
            'message':'已读取官网校历。' if parsed.get('semesterStart') else '已读取官网资料，请填写第一周周一和学期结束日期。'}
