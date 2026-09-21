from __future__ import annotations

import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import mailer, tos
from .auth import COOKIE_NAME, optional_user, require_csrf, require_user, require_verified, session_record
from .config import get_settings
from .database import create_all, get_db
from . import local_files, local_runtime
from .logic import clean_text, course_json, enqueue, note_json, resolve_prompt, usage_total, user_json
from .models import Course, EmailToken, Job, LoginSession, Note, NoteVersion, RateLimitBucket, User
from .schemas import (AccountPatch, CourseBody, CoursePatch, ForgotBody, LoginBody, NotePatch,
                      PasswordBody, RegisterBody, ResetBody, SummarizeBody, TokenBody, UploadBody)
from .security import (aware, expires_in, hash_password, normalize_email, random_token,
                       signed_csrf_token, token_digest, validate_password, verify_password)


settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_all()
    if settings.local_mode:
        from .database import SessionLocal
        with SessionLocal() as db:
            local_runtime.ensure_local_owner(db)
    yield


app = FastAPI(title="听记云端版", version="3.0.1", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(x) for x in first.get("loc", [])[1:])
    message = first.get("msg", "请求格式不正确")
    return JSONResponse({"detail": f"{location + '：' if location else ''}{message}"}, status_code=422)


@app.exception_handler(ValueError)
async def _value_error(_request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc) or "请求内容无效。"}, status_code=400)


@app.exception_handler(mailer.MailConfigurationError)
async def _mail_configuration_error(_request: Request, exc: mailer.MailConfigurationError):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(tos.TosError)
async def _tos_error(_request: Request, exc: tos.TosError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(Exception)
async def _unexpected_error(_request: Request, _exc: Exception):
    return JSONResponse({"detail": "服务器暂时无法完成请求。"}, status_code=500)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if settings.local_mode:
        try:
            local_runtime.allowed_request(request.headers, method=request.method, path=request.url.path)
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        if request.url.path.startswith("/api/auth/"):
            return JSONResponse({"detail": "本地应用使用这台电脑的独立资料。"}, status_code=404)
        return await call_next(request)
    if request.url.path.startswith("/api/local/"):
        return JSONResponse({"detail": "此入口仅供本地应用使用。"}, status_code=404)
    if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin", "")
        origin_path = ""
        try:
            parsed = urlsplit(origin)
            normalized = f"{parsed.scheme}://{parsed.netloc}"
            origin_path = parsed.path
        except ValueError:
            normalized = ""
        if not origin or normalized != settings.allowed_origin or origin_path not in {"", "/"}:
            return JSONResponse({"detail": "不接受其他网站发起的写入请求。"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    material_view = request.url.path.startswith("/api/course-materials/") and request.url.path.endswith("/view")
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if material_view else "DENY"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://upload.wikimedia.org https://thumb.wikimedia.org; media-src 'self' blob: https:; connect-src 'self' https://upload.wikimedia.org https://thumb.wikimedia.org; "
        "font-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'self'; "
        + ("frame-ancestors 'self'" if material_view else "frame-ancestors 'none'")
    )
    return response


def _csrf_dependency(_csrf: None = Depends(require_csrf)) -> None:
    return None


def _token(db: Session, raw: str, kind: str) -> EmailToken:
    record = db.scalar(select(EmailToken).where(
        EmailToken.token_hash == token_digest(raw), EmailToken.kind == kind, EmailToken.used_at.is_(None)
    ))
    if record is None or aware(record.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(400, "链接无效或已经过期。")
    return record


def _issue_email_token(db: Session, user: User, kind: str) -> str:
    db.execute(delete(EmailToken).where(EmailToken.user_id == user.id, EmailToken.kind == kind,
                                        EmailToken.used_at.is_(None)))
    raw = random_token()
    minutes = settings.email_token_ttl_minutes if kind == "verify" else settings.reset_token_ttl_minutes
    db.add(EmailToken(user_id=user.id, kind=kind, token_hash=token_digest(raw),
                      expires_at=expires_in(minutes=minutes)))
    db.flush()
    return raw


def _rate_limit(db: Session, key: str, limit: int, window_seconds: int) -> None:
    digest = token_digest("rate:" + key)
    now = datetime.now(timezone.utc)
    row = db.scalar(select(RateLimitBucket).where(RateLimitBucket.key == digest).with_for_update())
    if row is None:
        db.add(RateLimitBucket(key=digest, window_start=now, count=1))
        db.commit()
        return
    if (now - aware(row.window_start)).total_seconds() >= window_seconds:
        row.window_start, row.count = now, 1
        db.commit()
        return
    row.count += 1
    db.commit()
    if row.count > limit:
        raise HTTPException(429, "请求过于频繁，请稍后再试。")


def _course(db: Session, user: User, course_id: str) -> Course:
    value = db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id))
    if value is None:
        raise HTTPException(404, "找不到这门课程。")
    return value


def _note(db: Session, user: User, note_id: str, *, deleted: bool = False) -> Note:
    query = select(Note).where(Note.id == note_id, Note.user_id == user.id)
    if not deleted:
        query = query.where(Note.deleted_at.is_(None))
    value = db.scalar(query)
    if value is None:
        raise HTTPException(404, "找不到这条笔记。")
    return value


def _clean_course(data: dict, *, partial: bool = False) -> dict:
    limits = {"name": 120, "term": 80, "teacher": 80, "hotwords": 5000, "prompt": 30000}
    clean: dict[str, str] = {}
    for key, limit in limits.items():
        if key in data and data[key] is not None:
            clean[key] = clean_text(data[key], field=key, maximum=limit, required=key == "name")
    if not partial and "name" not in clean:
        raise ValueError("课程名称不能为空。")
    return clean


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"ok": True, "app": "tingji", "runtime": "unified", "version": app.version, "localMode": settings.local_mode,
            "database": True, "mailConfigured": settings.mail_configured,
            "providersConfigured": settings.providers_configured}


@app.get("/api/session")
def session_info(request: Request, db: Session = Depends(get_db)):
    user = optional_user(request, db)
    record = session_record(request, db)
    return {"user": user_json(db, user) if user else None,
            "csrfToken": record.csrf_token if record else signed_csrf_token(),
            "config": {"maxUploadBytes": settings.max_upload_bytes, "displayAllowanceYuan": None,
                       "localMode": settings.local_mode,
                       "localTransferUrl": local_runtime.transfer_url() if settings.local_mode else "",
                       "free": True, "mailConfigured": settings.mail_configured,
                       "providersConfigured": settings.providers_configured}}


@app.post("/api/auth/register", dependencies=[Depends(_csrf_dependency)])
def register(body: RegisterBody, request: Request, db: Session = Depends(get_db)):
    email = normalize_email(body.email)
    name = clean_text(body.name, field="姓名", maximum=80, required=True)
    validate_password(body.password)
    client = request.client.host if request.client else "unknown"
    _rate_limit(db, f"register-ip:{client}", 20, 3600)
    _rate_limit(db, f"register-mail:{client}:{email}", 5, 3600)
    user = db.scalar(select(User).where(User.email == email))
    if user and user.verified_at:
        return {"message": "如果邮箱可用，验证邮件已经发送。"}
    if user is None:
        user = User(email=email, name=name, password_hash=hash_password(body.password),
                    default_prompt="", language="zh", is_admin=email in settings.admin_emails)
        db.add(user)
        db.flush()
    raw = _issue_email_token(db, user, "verify")
    mailer.send_verification(user.email, raw)
    db.commit()
    return {"message": "验证邮件已经发送，请打开邮箱继续。"}


@app.post("/api/auth/verify", dependencies=[Depends(_csrf_dependency)])
def verify_email(body: TokenBody, db: Session = Depends(get_db)):
    record = _token(db, body.token, "verify")
    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(400, "链接无效或已经过期。")
    user.verified_at = datetime.now(timezone.utc)
    record.used_at = datetime.now(timezone.utc)
    db.commit()
    return {"message": "邮箱验证完成，可以登录了。"}


@app.post("/api/auth/login", dependencies=[Depends(_csrf_dependency)])
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    try:
        email = normalize_email(body.email)
    except ValueError:
        raise HTTPException(401, "邮箱或密码不正确。") from None
    client = request.client.host if request.client else "unknown"
    _rate_limit(db, f"login:{client}:{email}", 10, 900)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(401, "邮箱或密码不正确。")
    raw, csrf = random_token(), signed_csrf_token()
    db.add(LoginSession(user_id=user.id, token_hash=token_digest(raw), csrf_token=csrf,
                        expires_at=expires_in(days=settings.session_ttl_days)))
    db.commit()
    response.set_cookie(COOKIE_NAME, raw, max_age=settings.session_ttl_days * 86400,
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    return {"user": user_json(db, user), "csrfToken": csrf}


@app.post("/api/auth/logout", dependencies=[Depends(_csrf_dependency)])
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    record = session_record(request, db)
    if record:
        db.delete(record)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
    return {"message": "已退出。"}


@app.post("/api/auth/forgot", dependencies=[Depends(_csrf_dependency)])
def forgot(body: ForgotBody, request: Request, db: Session = Depends(get_db)):
    if not settings.mail_configured:
        raise HTTPException(503, "邮件服务尚未配置，请联系站长。")
    try:
        email = normalize_email(body.email)
    except ValueError:
        email = ""
    client = request.client.host if request.client else "unknown"
    _rate_limit(db, f"forgot-ip:{client}", 20, 3600)
    _rate_limit(db, f"forgot-mail:{client}:{email or 'invalid'}", 5, 3600)
    user = db.scalar(select(User).where(User.email == email)) if email else None
    if user:
        raw = _issue_email_token(db, user, "reset")
        mailer.send_reset(user.email, raw)
        db.commit()
    return {"message": "如果邮箱已注册，重置邮件将会发送。"}


@app.post("/api/auth/reset", dependencies=[Depends(_csrf_dependency)])
def reset_password(body: ResetBody, db: Session = Depends(get_db)):
    validate_password(body.password)
    record = _token(db, body.token, "reset")
    user = db.get(User, record.user_id)
    if user is None:
        raise HTTPException(400, "链接无效或已经过期。")
    user.password_hash = hash_password(body.password)
    record.used_at = datetime.now(timezone.utc)
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    db.commit()
    return {"message": "密码已更新，请重新登录。"}


@app.patch("/api/account", dependencies=[Depends(_csrf_dependency)])
def patch_account(body: AccountPatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        user.name = clean_text(data["name"], field="姓名", maximum=80, required=True)
    if "defaultPrompt" in data:
        prompt = clean_text(data["defaultPrompt"], field="默认提示词", maximum=30000)
        user.default_prompt = prompt
    if "language" in data:
        if data["language"] not in {"zh", "en", "auto"}:
            raise ValueError("语言设置无效。")
        user.language = data["language"]
    db.commit()
    return {"user": user_json(db, user)}


@app.post("/api/account/password", dependencies=[Depends(_csrf_dependency)])
def change_password(body: PasswordBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not verify_password(user.password_hash, body.currentPassword):
        raise HTTPException(400, "当前密码不正确。")
    user.password_hash = hash_password(body.password)
    db.commit()
    return {"message": "密码已更新。"}


@app.get("/api/courses")
def courses(user: User = Depends(require_user), db: Session = Depends(get_db)):
    values = db.scalars(select(Course).where(Course.user_id == user.id).order_by(Course.created_at.desc())).all()
    return {"courses": [{**course_json(x), **appearance_json(db, x)} for x in values]}


@app.post("/api/courses", dependencies=[Depends(_csrf_dependency)])
def create_course(body: CourseBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    course = Course(user_id=user.id, **_clean_course(body.model_dump()))
    if not course.hotwords:
        course.hotwords = course_hotwords(course.name)
    db.add(course)
    try:
        db.flush()
        ensure_appearance(db, course)
        from .syllabus import enqueue_syllabus
        enqueue_syllabus(db, course)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "同一学期已经有同名课程。") from None
    return {"course": course_json(course)}


@app.patch("/api/courses/{course_id}", dependencies=[Depends(_csrf_dependency)])
def patch_course(course_id: str, body: CoursePatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    course = _course(db, user, course_id)
    for key, value in _clean_course(body.model_dump(exclude_unset=True), partial=True).items():
        setattr(course, key, value)
    try:
        from .syllabus import enqueue_syllabus
        from .slide_matching import enqueue_user_matching
        enqueue_syllabus(db, course)
        enqueue_user_matching(db, user.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "同一学期已经有同名课程。") from None
    return {"course": course_json(course)}


@app.delete("/api/courses/{course_id}", dependencies=[Depends(_csrf_dependency)])
def delete_course(course_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    course = _course(db, user, course_id)
    for note in db.scalars(select(Note).where(Note.course_id == course.id, Note.user_id == user.id)):
        note.course_id = None
    db.delete(course)
    db.commit()
    return {"message": "课程已删除，原有笔记已保留并取消课程关联。"}


@app.get("/api/notes")
def notes(q: str = Query("", max_length=200), user: User = Depends(require_user), db: Session = Depends(get_db)):
    from .library_models import NoteLibrary
    query = select(Note).outerjoin(NoteLibrary, NoteLibrary.note_id == Note.id).where(
        Note.user_id == user.id, Note.deleted_at.is_(None), NoteLibrary.archived_at.is_(None))
    if q.strip():
        query = query.where(or_(*(getattr(Note, key).icontains(q.strip(), autoescape=True)
                                 for key in ("title", "source_name", "summary", "transcript"))))
    values = db.scalars(query.order_by(Note.updated_at.desc())).all()
    return {"notes": [note_json(db, x, include_body=False) for x in values]}


@app.get("/api/courses/{course_id}/knowledge")
def course_knowledge(course_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from .note_output import extract_course_info
    _course(db, user, course_id)
    items = []
    for summary in db.scalars(select(Note.summary).where(
            Note.course_id == course_id, Note.user_id == user.id, Note.deleted_at.is_(None)
    ).order_by(Note.updated_at.desc()).limit(30)):
        for item in extract_course_info(summary):
            if item not in items:
                items.append(item)
    return {"items": items[:12]}


@app.get("/api/notes/{note_id}")
def get_note(note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return {"note": note_json(db, _note(db, user, note_id))}


@app.patch("/api/notes/{note_id}", dependencies=[Depends(_csrf_dependency)])
def patch_note(note_id: str, body: NotePatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    data = body.model_dump(exclude_unset=True)
    if "title" in data:
        note.title = clean_text(data["title"], field="标题", maximum=160, required=True)
    if "courseId" in data:
        note.course_id = _course(db, user, data["courseId"]).id if data["courseId"] else None
    if "recordingDate" in data:
        from .recording_models import RecordingInfo
        value = data["recordingDate"] or ""
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("上课日期格式不正确。")
            date.fromisoformat(value)
        info = db.get(RecordingInfo, note.id)
        if info is None:
            info = RecordingInfo(note_id=note.id, recording_date=value, auto_title=False)
            db.add(info)
        else:
            info.recording_date = value
    if "title" in data:
        from .recording_models import RecordingInfo
        info = db.get(RecordingInfo, note.id)
        if info:
            info.auto_title = False
    if "summary" in data:
        summary = clean_text(data["summary"], field="笔记", maximum=1_000_000)
        note.summary = summary
        if summary:
            db.add(NoteVersion(user_id=user.id, note_id=note.id, kind="manual", summary=summary,
                               prompt_snapshot=note.prompt_snapshot, model=note.model))
    db.commit()
    return {"note": note_json(db, note)}


@app.delete("/api/notes/{note_id}", dependencies=[Depends(_csrf_dependency)])
def delete_note(note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    note.deleted_at = datetime.now(timezone.utc)
    note.status, note.stage = "error", "已移入回收状态"
    for job in db.scalars(select(Job).where(Job.note_id == note.id, Job.status.in_(["queued", "running", "waiting"]))):
        job.status, job.error = "cancelled", "笔记已删除"
    db.commit()
    return {"message": "笔记已删除；原始录音仍保留。"}


ALLOWED_UPLOADS = {".mp3", ".mp4", ".wav", ".m4a", ".webm", ".ogg", ".opus", ".flac", ".aac"}


@app.post("/api/uploads", dependencies=[Depends(_csrf_dependency)])
def create_upload(body: UploadBody, request: Request, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    if not settings.local_mode and not settings.tos_configured:
        raise HTTPException(503, "对象存储尚未配置，请联系站长。")
    if isinstance(body.size, bool) or not 0 < body.size <= settings.max_upload_bytes:
        raise ValueError(f"录音必须非空且不超过 {settings.max_upload_bytes} 字节。")
    filename = Path(body.filename.replace("\\", "/")).name[:240]
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_UPLOADS:
        raise ValueError("不支持这类音频文件。")
    course_id = _course(db, user, body.courseId).id if body.courseId else None
    prompt, prompt_version_id = resolve_prompt(db, user, course_id, body.prompt)
    note_id, object_id = str(uuid.uuid4()), secrets.token_hex(16)
    object_key = f"tingji/users/{user.id}/recordings/{note_id}/source-{object_id}{suffix}"
    if settings.local_mode:
        object_key = f"local/recordings/{note_id}/original{suffix}"
    title = clean_text(body.title or Path(filename).stem, field="标题", maximum=160, required=True)
    note = Note(id=note_id, user_id=user.id, course_id=course_id, title=title, source_name=filename,
                expected_size=body.size, object_key=object_key, status="uploading", stage="等待录音上传",
                prompt_snapshot=prompt, prompt_version_id=prompt_version_id, model=settings.deepseek_model)
    db.add(note)
    db.commit()
    upload = {"url": f"/api/local/uploads/{note.id}", "method": "PUT",
              "headers": {"X-CSRF-Token": request.headers.get("X-CSRF-Token", "")}} if settings.local_mode else {
                  "url": tos.presign(object_key, "PUT", 3600, user_id=user.id), "method": "PUT", "headers": {}}
    return {"note": note_json(db, note), "upload": upload}


@app.post("/api/notes/{note_id}/uploaded", dependencies=[Depends(_csrf_dependency)])
def upload_complete(note_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    existing = db.scalar(select(Job).where(Job.dedupe_key == f"transcribe:{note.id}"))
    if note.uploaded_at and existing:
        return {"note": note_json(db, note)}
    if not settings.providers_configured:
        raise HTTPException(503, "语音、AI 或对象存储服务尚未配置，请联系站长。")
    head = {"size": local_files.audio_path(note).stat().st_size} if settings.local_mode else tos.head_object(note.object_key, user_id=user.id)
    if head["size"] != note.expected_size:
        raise HTTPException(409, "上传文件大小与登记值不一致，请重新上传。")
    note.uploaded_at = datetime.now(timezone.utc)
    note.status, note.stage, note.error = "queued", "已上传，等待处理", ""
    enqueue(db, note, "transcribe", prompt=note.prompt_snapshot, prompt_version_id=note.prompt_version_id)
    db.commit()
    return {"note": note_json(db, note)}


@app.post("/api/notes/{note_id}/summarize", dependencies=[Depends(_csrf_dependency)])
def summarize(note_id: str, body: SummarizeBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    if db.scalar(select(Job.id).where(Job.note_id == note.id, Job.status.in_(["queued", "running", "waiting"]))):
        raise HTTPException(409, "正在处理，已有任务会继续完成。")
    if not note.transcript.strip():
        raise HTTPException(409, "这条笔记还没有完整转写。")
    if not settings.deepseek_api_key:
        raise HTTPException(503, "DeepSeek 尚未配置，请联系站长。")
    prompt, prompt_version_id = resolve_prompt(db, user, note.course_id, body.prompt)
    note.prompt_snapshot, note.prompt_version_id = prompt, prompt_version_id
    note.status, note.stage, note.error = "queued", "等待重新整理", ""
    enqueue(db, note, "summarize", prompt=prompt, prompt_version_id=prompt_version_id)
    db.commit()
    return {"note": note_json(db, note)}


@app.post("/api/notes/{note_id}/resume", dependencies=[Depends(_csrf_dependency)])
def resume(note_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    db.scalar(select(Note).where(Note.id == note.id).with_for_update())
    jobs = list(db.scalars(select(Job).where(Job.note_id == note.id, Job.kind != "reinforce").order_by(Job.created_at.desc())))
    if any(x.status in {"running", "waiting", "queued"} for x in jobs):
        return {"note": note_json(db, note)}
    # Only the latest task may be resumed. Older failures belong to earlier
    # versions and must not overwrite a newer completed or running result.
    job = jobs[0] if jobs and jobs[0].status in {"error", "uncertain"} else None
    if job is None:
        if note.status in {"uploaded", "ready"} and not note.transcript.strip() and not jobs:
            from .recordings import queue_imported_audio
            queue_imported_audio(db, note)
            db.commit()
        elif note.status == "uncertain":
            raise HTTPException(409, "旧任务的受理状态需要先核对，不能直接重新提交录音。")
        return {"note": note_json(db, note)}
    from .models import ProviderRequest
    requests = list(db.scalars(select(ProviderRequest).where(ProviderRequest.job_id == job.id)))
    request = next((x for x in requests if x.provider == "deepseek" and x.state in {"dispatched", "uncertain"}), None)
    if request:
        raise HTTPException(409, "上次 DeepSeek 请求结果未知，不能自动重发；请新建一次重新整理。")
    request = next((x for x in requests if x.provider == "volcengine-asr"), None)
    if request and request.provider == "volcengine-asr" and request.state in {"not_found", "rejected", "failed"}:
        raise HTTPException(409, "原语音任务不能安全恢复；不会自动重新提交录音。")
    job.status, job.stage, job.error = "queued", "resume", ""
    job.available_at = datetime.now(timezone.utc)
    note.status, note.stage, note.error = "queued", "恢复原任务", ""
    db.commit()
    return {"note": note_json(db, note)}


@app.get("/api/notes/{note_id}/download")
def download(note_id: str, format: str = Query("md", pattern="^(txt|md)$"),
             user: User = Depends(require_user), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    content = note.transcript if format == "txt" else note.summary
    if not content:
        raise HTTPException(409, "对应内容尚未生成。")
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", note.title).strip(" .")[:160] or "tingji"
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe + '.' + format, safe='')}"})


@app.get("/api/notes/{note_id}/audio")
def audio(note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    from .audio_storage import available_audio_key
    from .recordings import segments_for
    key = available_audio_key(db, note)
    if not key:
        if len(segments_for(db, note)) > 1 and note.status in {"uploading", "queued", "preparing", "transcribing"}:
            raise HTTPException(409, "多段录音还在合并，请稍后下载。")
        raise HTTPException(404, "这条笔记没有可播放的录音。")
    if key.startswith(("local/", "legacy/")) and settings.local_mode:
        local_files.audio_path(note)
        return {"url": f"/api/local/notes/{note.id}/audio"}
    return {"url": tos.presign(key, "GET", 900, user_id=user.id)}


@app.get("/api/local/status")
def local_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    files = local_files.list_inbox(db)
    url = local_runtime.transfer_url()
    return {"localMode": True, "transferUrl": url, "tailscaleConfigured": bool(url),
            "inboxCount": sum(not item["imported"] for item in files)}


@app.get("/api/local/inbox")
def local_inbox(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return {"files": local_files.list_inbox(db)}


@app.put("/api/local/uploads/{note_id}", dependencies=[Depends(_csrf_dependency)])
async def local_upload(note_id: str, request: Request, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    await local_files.store_upload(note, request)
    return {"ok": True}


@app.get("/api/local/notes/{note_id}/audio")
def local_audio(note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    note = _note(db, user, note_id)
    from .audio_storage import audio_removed
    if audio_removed(db, note, note.object_key):
        raise HTTPException(404, "这份本机原录音已清理。")
    return FileResponse(local_files.audio_path(note), filename=note.source_name, content_disposition_type="inline")


from .schemas import StrictModel


class InboxImportBody(StrictModel):
    fileId: str
    courseId: str | None = None
    title: str = ""
    prompt: str | None = None


@app.post("/api/local/inbox/import", dependencies=[Depends(_csrf_dependency)])
def import_local_inbox(body: InboxImportBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    source = local_files.inbox_path(body.fileId)
    note_id = local_files.inbox_note_id(source)
    existing = db.get(Note, note_id)
    if existing:
        return {"note": note_json(db, _note(db, user, note_id))}
    if not settings.providers_configured:
        raise HTTPException(503, "语音、AI 或对象存储服务尚未配置。")
    course_id = _course(db, user, body.courseId).id if body.courseId else None
    prompt, prompt_version_id = resolve_prompt(db, user, course_id, body.prompt)
    note = Note(id=note_id, user_id=user.id, course_id=course_id,
                title=clean_text(body.title or source.stem, field="标题", maximum=160, required=True),
                source_name=source.name[:240], expected_size=source.stat().st_size,
                object_key=f"local/recordings/{note_id}/original{source.suffix.lower()}",
                status="queued", stage="已导入，等待处理", error="",
                uploaded_at=datetime.now(timezone.utc), prompt_snapshot=prompt,
                prompt_version_id=prompt_version_id, model=settings.deepseek_model)
    local_files.copy_inbox_original(source, note)
    db.add(note)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"note": note_json(db, _note(db, user, note_id))}
    enqueue(db, note, "transcribe", prompt=prompt, prompt_version_id=prompt_version_id)
    db.commit()
    return {"note": note_json(db, note)}


@app.get("/api/admin/overview")
def admin_overview(user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not user.is_admin:
        raise HTTPException(403, "没有管理员权限。")
    users = db.scalars(select(User).order_by(User.created_at)).all()
    rows = [{"id": x.id, "name": x.name, "email": x.email, "verified": bool(x.verified_at),
             "usageYuan": usage_total(db, user_id=x.id), "usageEstimated": True,
             "noteCount": db.scalar(select(func.count()).select_from(Note).where(Note.user_id == x.id)) or 0}
            for x in users]
    return {"users": rows, "totals": {"users": len(rows),
            "notes": db.scalar(select(func.count()).select_from(Note)) or 0,
            "usageYuan": usage_total(db), "usageEstimated": True}}


from .preferences_api import router as preferences_router
from .timetable_api import router as timetable_router, appearance_json, ensure_appearance
from .timetable import course_hotwords

app.include_router(preferences_router)
app.include_router(timetable_router)
from .recordings_api import router as recordings_router
app.include_router(recordings_router)
from .slides_api import router as slides_router
app.include_router(slides_router)
from .library_api import router as library_router
app.include_router(library_router)
from .audio_storage_api import router as audio_storage_router
app.include_router(audio_storage_router)
from .course_materials import router as course_materials_router
app.include_router(course_materials_router)
from .school_api import router as school_router
from .assignment_api import router as assignment_router
app.include_router(school_router)
app.include_router(assignment_router)

app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True, check_dir=False), name="static")
