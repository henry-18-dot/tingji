"""User-provided official sources, with pinned public-IP HTTPS downloads."""
from __future__ import annotations
from datetime import date
import html
from http.client import HTTPSConnection
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit
from .academic_calendar import AUTUMN_2026, calendar_for
from .school_models import SchoolSettings
from .timetable_models import TimetableState

DEFAULT_DOMAIN = "sustech.edu.cn"


def official_domain(value: str) -> str:
    value=value.strip().lower().rstrip('.')
    if not value or re.search(r'[^a-z0-9.\-]',value) or '.' not in value or len(value)>253:
        raise ValueError("官网域名只填域名，例如 sustech.edu.cn。")
    if any(not p or p.startswith('-') or p.endswith('-') for p in value.split('.')):
        raise ValueError("官网域名格式不正确。")
    try: ipaddress.ip_address(value)
    except ValueError: pass
    else: raise ValueError("官网来源不能使用 IP 地址。")
    if value.endswith(('.localhost','.local','.internal','.test','.invalid')) or value in {'localhost','local'}:
        raise ValueError("官网来源必须是公开网站。")
    return value


def source_url(value: str, domain: str) -> str:
    if not value: return ''
    try:
        p=urlsplit(value)
        valid=(p.scheme=='https' and p.hostname and (p.hostname==domain or p.hostname.endswith('.'+domain))
               and not p.username and not p.password and p.port in (None,443) and not re.search(r'[\s\\\x00-\x1f]',value))
    except ValueError: valid=False
    if not valid: raise ValueError("资料链接须为所填学校官网域名下的 HTTPS 地址。")
    return value


def public_addresses(host: str) -> list[str]:
    addresses=sorted({item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("官网地址不能指向本机或内网。")
    return addresses


class OfficialClient:
    def __init__(self, domain): self.domain=official_domain(domain); self.deadline=time.monotonic()+35
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def get(self,url,limit):
        for _ in range(4):
            source_url(url,self.domain)
            p=urlsplit(url); addresses=public_addresses(p.hostname)
            if time.monotonic()>self.deadline: raise ValueError("官网读取超时。")
            connection=HTTPSConnection(p.hostname,443,timeout=min(10,max(.2,self.deadline-time.monotonic())))
            # TLS still validates the original hostname. The socket connects
            # only to the already checked address, preventing DNS rebinding.
            connection._create_connection=lambda address,timeout,source_address=None: socket.create_connection((addresses[0],443),timeout,source_address)
            try:
                connection.request('GET',(p.path or '/')+('?' + p.query if p.query else ''),headers={'User-Agent':'Tingji/3 official course reader','Accept-Encoding':'identity'})
                response=connection.getresponse()
                if response.status in {301,302,303,307,308}:
                    location=response.getheader('Location')
                    if not location: raise ValueError("官网跳转缺少地址。")
                    url=source_url(urljoin(url,location),self.domain);continue
                if response.status!=200: raise ValueError("官网暂时无法读取该资料。")
                if int(response.getheader('Content-Length','0'))>limit: raise ValueError("官网资料超过大小上限。")
                chunks=[];size=0
                while True:
                    chunk=response.read(min(65536,limit-size+1))
                    if not chunk: break
                    chunks.append(chunk);size+=len(chunk)
                    if size>limit or time.monotonic()>self.deadline: raise ValueError("官网资料超过大小或时间上限。")
                return b''.join(chunks),url
            finally: connection.close()
        raise ValueError("官网跳转次数过多。")


def school_json(db,user_id):
    row=db.get(SchoolSettings,user_id)
    state=db.get(TimetableState,user_id)
    start=(state.semester_start if state and state.semester_start else row.semester_start if row else None)
    default=calendar_for(start) if row is None else None
    if row is None and not start:start=(default or {}).get('semesterStart')
    return {'schoolName':row.name if row else '南方科技大学','officialDomain':row.official_domain if row else DEFAULT_DOMAIN,
            'calendarUrl':row.calendar_url if row else AUTUMN_2026['sourceUrl'],
            'syllabusUrl':row.syllabus_url if row else 'https://mirrors.sustech.edu.cn/courses/syllabus/',
            'semesterStart':start,'semesterEnd':row.semester_end if row else (default or {}).get('end'),
            'checkedAt':row.checked_at.isoformat() if row and row.checked_at else None}


def sync_school_start(db,user_id,start):
    """Persist explicit timetable edits, including clearing the default date."""
    row=db.get(SchoolSettings,user_id)
    if row is None:
        values=school_json(db,user_id)
        row=SchoolSettings(user_id=user_id,name=values['schoolName'],official_domain=values['officialDomain'],
                           calendar_url=values['calendarUrl'],syllabus_url=values['syllabusUrl'],semester_end=values['semesterEnd'])
        db.add(row)
    row.semester_start=start


def is_sustech(db,user_id):
    school=school_json(db,user_id)
    return school['officialDomain']==DEFAULT_DOMAIN and school['schoolName'] in {'南方科技大学','SUSTech','Southern University of Science and Technology'}


def school_calendar(db,user_id,semester_start=None):
    row=db.get(SchoolSettings,user_id)
    start=semester_start or (row.semester_start if row else None)
    if row and not start:return None
    known_url=not row or not row.calendar_url or row.calendar_url.replace('https://www.','https://')==AUTUMN_2026['sourceUrl']
    if is_sustech(db,user_id) and known_url:
        result=calendar_for(start)
        if result:
            if row:result['end']=row.semester_end
            return result
    if row and (start or row.calendar):
        result={**(row.calendar or {}),'sourceUrl':row.calendar_url,'semesterStart':start,'end':row.semester_end,'holidays':(row.calendar or {}).get('holidays',[]),'adjustments':(row.calendar or {}).get('adjustments',[])}
        result['teachingEnd']=result.get('teachingEnd') or row.semester_end
        return result
    return None


def source_text(raw: bytes, url: str) -> str:
    if raw.startswith(b'%PDF-'):
        from .course_materials import extract_material
        return extract_material('school-source.pdf',raw)
    value=raw.decode('utf-8-sig',errors='replace')
    value=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>','',value,flags=re.I|re.S)
    return html.unescape(re.sub(r'<[^>]*>','\n',value))[:120000]


def parse_calendar_text(text: str) -> dict:
    """Only explicit full dates beside start/end labels; no guessing PDF grids."""
    dates={}
    value=re.sub(r'[ \t]+',' ',text)
    pattern=r'(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?'
    for key,label in [('semesterStart',r'第一(?:教学)?周(?:周一|星期一)?|教学开始|开始上课|上课日期|classes begin'),('end',r'学期结束|期末考试结束|semester ends')]:
        match=re.search('(?:'+label+r')\s*[:：]?\s*'+pattern,value,re.I)
        if match:
            try: found=date(*map(int,match.groups())).isoformat()
            except ValueError: continue
            if key=='semesterStart' and date.fromisoformat(found).weekday()!=0: continue
            dates[key]=found
    return dates


def discover_school_syllabus(course,settings):
    """Read exact course links from the supplied school's public catalogue."""
    from .syllabus import SyllabusUnavailable,summarize_material,_normal
    if not settings['syllabusUrl']: raise SyllabusUnavailable("请在学校设置中填写官网大纲目录。")
    try:
        with OfficialClient(settings['officialDomain']) as client:
            raw,url=client.get(settings['syllabusUrl'],3*1024*1024)
            candidates=[(raw,url)] if raw.startswith(b'%PDF-') else []
            if not raw.startswith(b'%PDF-'):
                value=raw.decode('utf-8-sig',errors='replace')
                headings=[html.unescape(re.sub(r'<[^>]*>','',part)).strip() for part in re.findall(r'<h[12]\b[^>]*>(.*?)</h[12]>',value,re.I|re.S)]
                if any(_normal(re.sub(r'(?:课程)?教学大纲|课程大纲|syllabus','',heading,flags=re.I))==_normal(course.name) for heading in headings):
                    candidates.append((raw,url))
                for href,label in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',value,re.I|re.S):
                    label=html.unescape(re.sub(r'<[^>]*>','',label)).strip()
                    if _normal(re.sub(r'(?:课程)?教学大纲|课程大纲|syllabus|\.pdf$','',label,flags=re.I))!=_normal(course.name): continue
                    target=source_url(urljoin(url,html.unescape(href)),settings['officialDomain'])
                    candidates.append(client.get(target,8*1024*1024))
                    if len(candidates)>=3: break
            for content,source in candidates:
                text=source_text(content,source)
                lines=[_normal(re.sub(r'^(?:课程名称|course\s+title)\s*[:：]?|(?:课程)?教学大纲|课程大纲|syllabus','',line,flags=re.I)) for line in text[:3000].splitlines() if line.strip()]
                if _normal(course.name) not in lines: continue
                is_pdf=content.startswith(b'%PDF-')
                return {'filename':course.name+('.pdf' if is_pdf else '.txt'),'raw':content if is_pdf else text.encode(),
                        'content':text,'summary':summarize_material(text),'sourceUrl':source,'sourceKind':'school',
                        'sourceUpdatedAt':'','courseCode':''}
    except (ValueError,OSError) as exc: raise SyllabusUnavailable(str(exc)) from None
    raise SyllabusUnavailable("官网目录中未找到名称一致的大纲。")
