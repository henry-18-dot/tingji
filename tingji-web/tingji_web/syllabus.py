"""Fetch exact SUSTech syllabuses once, retaining PDFs and compact memories.

The public teaching catalogue identifies a course. Its visitor page currently
requires login for the full syllabus, so published PDFs on the school's mirror
are the fallback. No search snippets, prerequisites, or approximate titles are
used as course identity. Network jobs never use a paid model.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
import unicodedata
from datetime import timedelta, timezone
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from sqlalchemy import or_, select, update

from .database import SessionLocal
from .models import Course, CourseMaterial, User, utcnow
from .syllabus_models import CourseSyllabusJob, MaterialMemory, SyllabusCache

CATALOG_URL = "https://course-tao.sustech.edu.cn/kcxxweb/KcxxwebYxkcChineseAPP"
DETAIL_URL = "https://course-tao.sustech.edu.cn/kcxxweb/queryKcxxwebDetailVisitorChinesePC?pkcdm="
MIRROR_URL = "https://mirrors.sustech.edu.cn/courses/syllabus/"
ALLOWED_HOSTS = {"course-tao.sustech.edu.cn", "mirrors.sustech.edu.cn"}
MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 3
MEMORY_VERSION = "extractive-v1"


class SyllabusUnavailable(Exception):
    pass


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _normal(value: str) -> str:
    return re.sub(r"[\s\-—_·:：]+", "", unicodedata.normalize("NFKC", value)).casefold()


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or
            parts.username or parts.password or parts.port not in (None, 443)):
        raise SyllabusUnavailable("大纲来源不在南科大公开课程网站内。")
    return url


class SchoolClient:
    def __init__(self):
        self.client = httpx.Client(timeout=httpx.Timeout(15, connect=8), follow_redirects=False,
                                   headers={"User-Agent": "Tingji/3 course-syllabus reader"})
        self.deadline = time.monotonic() + 70

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def get(self, url: str, limit: int) -> tuple[bytes, str]:
        for _ in range(4):
            safe_url(url)
            if time.monotonic() > self.deadline:
                raise httpx.TimeoutException("syllabus lookup budget exceeded")
            with self.client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location:
                        raise SyllabusUnavailable("官网大纲地址暂不可用。")
                    url = safe_url(urljoin(url, location))
                    continue
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > limit:
                    raise SyllabusUnavailable("官网大纲文件超过下载上限。")
                chunks, size = [], 0
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit or time.monotonic() > self.deadline:
                        raise SyllabusUnavailable("官网大纲下载超过大小或时间上限。")
                    chunks.append(chunk)
                return b"".join(chunks), url
        raise SyllabusUnavailable("官网大纲跳转次数过多。")


def parse_catalog(value: str) -> list[dict]:
    entries = {}
    for code, name in re.findall(r'<p\b[^>]*>\s*<span\b[^>]*class=["\'][^"\']*kc-code[^"\']*["\'][^>]*>(.*?)</span>(.*?)</p>', value, re.I | re.S):
        code, name = _plain(code), _plain(name)
        if code and name and len(code) <= 80 and len(name) <= 200:
            entries[code] = {"code": code, "name": name}
    if not entries:
        raise SyllabusUnavailable("官网课程目录暂时无法读取。")
    return list(entries.values())


def choose_course(name: str, entries: list[dict]) -> dict:
    wanted = _normal(name)
    matches = [item for item in entries if wanted in {
        _normal(item["name"]), _normal(item["code"]), _normal(item["code"] + item["name"])}]
    if not matches:
        raise SyllabusUnavailable("官网未找到名称一致的课程大纲。")
    if len(matches) > 1:
        raise SyllabusUnavailable("官网存在同名课程，暂未取得可确认的大纲。")
    return matches[0]


def parse_pdf_links(value: str, base_url: str) -> list[dict]:
    result = []
    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', value, re.I | re.S):
        href, label = html.unescape(match.group(1)), _plain(match.group(2))
        if not urlsplit(href).path.lower().endswith(".pdf"):
            continue
        url = urljoin(base_url, href)
        try:
            safe_url(url)
        except (SyllabusUnavailable, ValueError):
            continue
        row_end = value.find("</tr>", match.end())
        tail = value[match.end():min(row_end if row_end >= 0 else match.end(), match.end() + 500)]
        date = re.search(r'class=["\']date["\'][^>]*>([^<]*)', tail)
        result.append({"url": url, "label": label, "sourceUpdatedAt": date.group(1).strip() if date else ""})
    return result


def identity_matches(content: str, entry: dict) -> bool:
    title_area = content[:3000].replace("\r", "\n")
    title = re.search(r"课程(?:中文)?名称\s*(?:Course\s*(?:Title|Name))?\s*[:：]?\s*(.{1,350}?)\s*(?:\n\s*2\s*[.．]|授课院系|Originating Department)", title_area, re.I | re.S)
    if not title:
        title = re.search(r"Course\s*Title\s*[:：]?\s*(.{1,250}?)\s*(?:\n\s*2\s*[.．]|Originating Department)", title_area, re.I | re.S)
    if not title:
        return False
    target, actual = _normal(entry["name"]), _normal(title.group(1))
    # Chinese and bilingual title fields: all Chinese title characters must
    # agree, and A/B/I/II qualifiers still have to occur in the full title.
    chinese = lambda text: "".join(re.findall(r"[\u3400-\u9fff]", text))
    if chinese(target):
        title_ok = chinese(target) == chinese(actual) and target in actual
    else:
        title_ok = actual == target
    code = re.search(r"(?:Course\s*Code|课程(?:编号|代码))\s*[:：]?\s*(?:Course\s*Code\s*)?([A-Za-z][A-Za-z0-9 .\-]{1,50})", title_area, re.I)
    return bool(title_ok and code and _normal(code.group(1).strip()) == _normal(entry["code"]))


def summarize_material(content: str) -> str:
    """Bounded extractive memory: preserve source wording and numerical rules."""
    text = content.replace("\r", "\n")
    text = re.sub(r"\n[ \t]*\n+", "\n", text).strip()
    if len(text) <= 6500:
        return text
    sections = [
        ("课程信息", r"课程(?:中文)?名称|Course\s*Title", r"(?:11\s*[.．]\s*授课方式|Delivery Method)", 650),
        ("教学目标", r"15\s*[.．]\s*教学目标|Course Objectives", r"16\s*[.．]\s*预达学习成果|Learning Outcomes", 950),
        ("学习成果", r"16\s*[.．]\s*预达学习成果|Learning Outcomes", r"17\s*[.．]\s*课程内容|Course Contents", 900),
        ("教学内容", r"17\s*[.．]\s*课程内容|Course Contents", r"18\s*[.．]\s*教材|Textbooks?\s*(?:and|&)|19\s*[.．]\s*评价", 2600),
        ("教材", r"18\s*[.．]\s*教材|Textbooks?\s*(?:and|&)", r"19\s*[.．]\s*评|课程评估|Course Assessment", 500),
        ("考核", r"19\s*[.．]\s*评|课程评估|Course Assessment|考核方式|成绩评定", r"20\s*[.．]\s*记分|Record of the Course", 950),
    ]
    parts = []
    for label, start, end, budget in sections:
        found = re.search(start, text, re.I)
        if not found:
            continue
        stop = re.search(end, text[found.end():], re.I)
        block = text[found.start():found.end() + stop.start() if stop else len(text)]
        lines = [line.strip() for line in block.splitlines() if line.strip() and
                 (label == "考核" or not re.fullmatch(r"\d{1,2}", line.strip()))]
        if label in {"教学目标", "学习成果", "教学内容"} and len("".join(re.findall(r"[\u3400-\u9fff]", block))) > 150:
            lines = [line for line in lines if re.search(r"[\u3400-\u9fff]", line)]
        if label == "教学内容":
            # Prefer all chapter/lab bullet topics over prose duplicated beside
            # them in the bilingual teaching-requirement column.
            topics = [line for line in lines if re.match(r"[•●\-]|第.+[章节]|\d+[.、]", line) or "/" in line or "实验教学" in line]
            if len(topics) >= 8:
                lines = topics
        selected, used = [], 0
        for line in lines:
            if used + len(line) + 1 > budget:
                continue
            selected.append(line)
            used += len(line) + 1
        if selected:
            parts.append(label + "：\n" + "\n".join(selected))
    if not parts:
        # Unstructured uploads: retain informative sentences spread across the
        # original instead of only the first page.
        lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 8]
        priority = [line for line in lines if re.search(r"课程|目标|章节|考核|成绩|期末|实验|教材|%|％", line)]
        parts = ["\n".join(dict.fromkeys(priority + lines))[:6300]]
    return "\n\n".join(parts)[:6500]


def ensure_material_memory(db, material: CourseMaterial, *, summary: str | None = None, **metadata) -> MaterialMemory:
    memory = db.get(MaterialMemory, material.id)
    if memory is None:
        memory = MaterialMemory(material_id=material.id, course_id=material.course_id,
                                user_id=material.user_id, summary=summary or summarize_material(material.content),
                                extractor_version=MEMORY_VERSION, **metadata)
        db.add(memory)
        db.flush()
    elif metadata.get("source_url") and not memory.source_url:
        for key, value in metadata.items():
            setattr(memory, key, value)
    return memory


def backfill_material_memories(db, limit: int = 100) -> int:
    rows = list(db.scalars(select(CourseMaterial).outerjoin(MaterialMemory,
                CourseMaterial.id == MaterialMemory.material_id).where(MaterialMemory.material_id.is_(None)).limit(limit)))
    for material in rows:
        ensure_material_memory(db, material)
    return len(rows)


def enqueue_syllabus(db, course: Course) -> CourseSyllabusJob:
    job = db.get(CourseSyllabusJob, course.id)
    if job is None:
        job = CourseSyllabusJob(course_id=course.id, user_id=course.user_id)
        db.add(job)
        db.flush()
    elif job.status in {"not_found", "error"} and (not job.checked_at or _aware(job.checked_at) < utcnow() - timedelta(days=7)):
        job.status, job.error, job.attempts, job.available_at = "queued", "", 0, utcnow()
    return job


def enqueue_existing_syllabuses(db, user_id: str | None = None) -> int:
    query = select(Course)
    if user_id is not None:
        query = query.where(Course.user_id == user_id)
    courses = list(db.scalars(query))
    for course in courses:
        enqueue_syllabus(db, course)
    backfill_material_memories(db)
    return len(courses)


def syllabus_status(db, course_id: str) -> dict:
    job = db.get(CourseSyllabusJob, course_id)
    return {"status": job.status if job else "queued", "error": job.error if job else "",
            "sourceUrl": job.source_url if job else "", "courseCode": job.course_code if job else "",
            "checkedAt": job.checked_at.isoformat() if job and job.checked_at else None}


def _cache_get(db, key: str):
    row = db.get(SyllabusCache, key)
    return row if row and _aware(row.expires_at) > utcnow() else None


def _cache_put(db, key: str, payload: dict, days: int = 1, raw: bytes | None = None):
    row = db.get(SyllabusCache, key)
    if row is None:
        row = SyllabusCache(key=key)
        db.add(row)
    row.payload, row.original_content, row.expires_at = payload, raw, utcnow() + timedelta(days=days)
    return row


def discover_syllabus(db, course_name: str, school=None) -> dict:
    """The injected school client makes discovery testable without networking."""
    if school is None:
        with SchoolClient() as client:
            return discover_syllabus(db, course_name, client)
    cached = _cache_get(db, "catalog-v1")
    if cached:
        entries = cached.payload["entries"]
    else:
        raw, _ = school.get(CATALOG_URL, MAX_HTML_BYTES)
        entries = parse_catalog(raw.decode("utf-8-sig"))
        _cache_put(db, "catalog-v1", {"entries": entries})
    entry = choose_course(course_name, entries)
    detail_url = DETAIL_URL + quote(entry["code"], safe="")
    pdf_key = "pdf:" + hashlib.sha256((entry["code"] + "\0" + entry["name"]).encode()).hexdigest()
    cached = _cache_get(db, pdf_key)
    if cached:
        return {**cached.payload, "raw": cached.original_content}
    from .course_materials import extract_material

    def read_candidate(item):
        try:
            raw, url = school.get(item["url"], MAX_PDF_BYTES)
            if not raw.startswith(b"%PDF-"):
                return None
            content = extract_material(entry["code"] + ".pdf", raw)
            if not identity_matches(content, entry):
                return None
        except (ValueError, SyllabusUnavailable):
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {403, 404, 410}:
                raise
            return None
        result = {"filename": entry["code"] + "-" + entry["name"] + ".pdf", "content": content,
                  "summary": summarize_material(content), "sourceUrl": url,
                  "sourceKind": item["sourceKind"], "sourceUpdatedAt": item["sourceUpdatedAt"],
                  "courseCode": entry["code"]}
        _cache_put(db, pdf_key, result, days=30, raw=raw)
        return {**result, "raw": raw}

    try:
        raw, detail_url = school.get(detail_url, MAX_HTML_BYTES)
        direct_links = parse_pdf_links(raw.decode("utf-8-sig"), detail_url)
    except httpx.HTTPError:
        direct_links = []
    for item in direct_links[:3]:
        result = read_candidate({**item, "sourceKind": "school"})
        if result:
            return result
    cached = _cache_get(db, "mirror-index-v1")
    if cached:
        mirror_links = cached.payload["links"]
    else:
        raw, url = school.get(MIRROR_URL, MAX_HTML_BYTES)
        mirror_links = parse_pdf_links(raw.decode("utf-8-sig"), url)
        if not mirror_links:
            raise SyllabusUnavailable("南科大公开大纲目录暂时无法读取。")
        _cache_put(db, "mirror-index-v1", {"links": mirror_links})
    for item in mirror_links:
        filename = unquote(urlsplit(item["url"]).path.rsplit("/", 1)[-1])
        if _normal(filename.removesuffix(".pdf")) == _normal(entry["code"]):
            result = read_candidate({**item, "sourceKind": "school_mirror"})
            if result:
                return result
            break
    raise SyllabusUnavailable("官网暂未公开可确认的课程大纲 PDF。")


def run_syllabus_job() -> bool:
    """Claim at most one durable job; bounded retries recover network failures."""
    recover_syllabus_jobs()
    with SessionLocal() as db:
        if backfill_material_memories(db):
            db.commit()
        now = utcnow()
        eligible = or_((CourseSyllabusJob.status == "queued") & (CourseSyllabusJob.available_at <= now),
                       (CourseSyllabusJob.status == "running") & (CourseSyllabusJob.locked_at < now - timedelta(minutes=5)))
        job = db.scalar(select(CourseSyllabusJob).where(eligible).order_by(CourseSyllabusJob.available_at).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return False
        # Conditional update also protects the single-process SQLite worker.
        claimed = db.execute(update(CourseSyllabusJob).where(CourseSyllabusJob.course_id == job.course_id, eligible)
                             .values(status="running", locked_at=now, attempts=CourseSyllabusJob.attempts + 1)
                             .execution_options(synchronize_session=False))
        if not claimed.rowcount:
            db.rollback()
            return False
        course_id = job.course_id
        db.commit()
        db.expire_all()
        job = db.get(CourseSyllabusJob, course_id)
        course = db.get(Course, course_id)
        if course is None:
            return True
        try:
            from .schools import is_sustech, school_json, discover_school_syllabus
            school = school_json(db, course.user_id)
            default_source = is_sustech(db, course.user_id) and school['syllabusUrl'] in {MIRROR_URL, CATALOG_URL}
            result = discover_syllabus(db, course.name) if default_source else discover_school_syllabus(course, school)
            db.scalar(select(User).where(User.id == course.user_id).with_for_update())
            db.expire_all()
            if school_json(db, course.user_id) != school:
                job = db.get(CourseSyllabusJob, course_id)
                job.status, job.locked_at, job.available_at = "queued", None, utcnow()
                db.commit()
                return True
            digest = hashlib.sha256(result["raw"]).hexdigest()
            material = db.scalar(select(CourseMaterial).where(CourseMaterial.user_id == course.user_id,
                                 CourseMaterial.course_id == course.id, CourseMaterial.sha256 == digest))
            if material is None:
                material = CourseMaterial(user_id=course.user_id, course_id=course.id, filename=result["filename"],
                                          content=result["content"], original_content=result["raw"], sha256=digest)
                db.add(material)
                db.flush()
            ensure_material_memory(db, material, summary=result["summary"], source_url=result["sourceUrl"], source_kind=result["sourceKind"],
                                   course_code=result["courseCode"], source_updated_at=result["sourceUpdatedAt"])
            job.status, job.error = "ready", ""
            job.source_url, job.course_code = result["sourceUrl"], result["courseCode"]
            from .slide_matching import enqueue_user_matching
            enqueue_user_matching(db, course.user_id)
        except SyllabusUnavailable as exc:
            job.status, job.error = "not_found", str(exc)[:240]
        except Exception as exc:
            # A failed document or concurrent public-cache insert must not kill
            # the shared audio worker or leave its transaction unusable.
            db.rollback()
            job = db.get(CourseSyllabusJob, course_id)
            if job is None:
                return True
            job.status = "queued" if job.attempts < MAX_ATTEMPTS else "error"
            job.error = "学校课程网站暂时无法访问。" if isinstance(exc, httpx.HTTPError) else "课程大纲暂时无法处理。"
            job.available_at = utcnow() + timedelta(minutes=job.attempts * 2)
        job.checked_at, job.locked_at = utcnow(), None
        db.commit()
        return True


def recover_syllabus_jobs() -> int:
    with SessionLocal() as db:
        rows = list(db.scalars(select(CourseSyllabusJob).where(CourseSyllabusJob.status == "running",
                    CourseSyllabusJob.locked_at < utcnow() - timedelta(minutes=5)).with_for_update(skip_locked=True)))
        for job in rows:
            job.status = "queued" if job.attempts < MAX_ATTEMPTS else "error"
            job.locked_at, job.available_at = None, utcnow()
            if job.status == "error":
                job.error, job.checked_at = "官网大纲下载多次中断，稍后会重新检查。", utcnow()
        retry = list(db.scalars(select(CourseSyllabusJob).where(CourseSyllabusJob.status.in_(["error", "not_found"]),
                     CourseSyllabusJob.checked_at < utcnow() - timedelta(days=7)).with_for_update(skip_locked=True)))
        for job in retry:
            job.status, job.error, job.attempts, job.available_at = "queued", "", 0, utcnow()
        db.commit()
        return len(rows) + len(retry)
