"""Chunked slide uploads stay below hosted frontend request limits."""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import require_csrf, require_user, require_verified
from .database import get_db
from .models import Course, Note, User, utcnow
from .slide_models import SlideChunk, SlideDeck, SlideJob, SlidePage, SlideLocalization
from .slide_translation import VERSION
from .slides import CHUNK_BYTES, MAX_FILE_BYTES, MAX_PAGES, transcript_context, validate_file
from .slide_matching import active_notes, linked_notes, matching_info, reject_link
from .matching_models import SlideNoteLink, SlideLinkDecision
from .recording_models import RecordingInfo

router = APIRouter(prefix="/api/slides", tags=["slides"])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UploadBody(Body):
    filename: str = Field(min_length=1, max_length=240)
    size: StrictInt = Field(ge=1, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    courseId: str | None = Field(default=None, max_length=36)
    noteId: str | None = Field(default=None, max_length=36)


class ChunkBody(Body):
    contentBase64: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


class GenerateBody(Body):
    mode: Literal["explain", "translate"] = "explain"
    language: Literal["zh", "en"] = "zh"


def _deck(db, user, deck_id):
    deck = db.scalar(select(SlideDeck).where(SlideDeck.id == deck_id, SlideDeck.user_id == user.id))
    if not deck:
        raise HTTPException(404, "找不到这份课件。")
    return deck


def _page(db, deck, number):
    page = db.scalar(select(SlidePage).where(SlidePage.deck_id == deck.id, SlidePage.number == number))
    if not page:
        raise HTTPException(404, "找不到这一页。")
    return page


def _lock_user(db, user):
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())


def deck_json(deck, db=None):
    result = {"id": deck.id, "filename": deck.filename, "courseId": deck.course_id, "noteId": deck.note_id,
            "status": deck.status, "error": deck.error, "pageCount": deck.page_count,
            "createdAt": deck.created_at.isoformat(), "size": deck.expected_size}
    if db is not None:
        result.update(matching_info(db, deck))
    return result


def job_json(job):
    current = job.dedupe_key.startswith(VERSION + ":")
    image_revision = job.completed_at.strftime("%Y%m%d%H%M%S%f") if job.completed_at else "0"
    return {"id": job.id, "mode": job.kind, "language": job.language, "status": job.status,
            "content": job.content, "error": job.error, "contextSource": job.context_source,
            "version": VERSION if current else "legacy",
            "imageUrl": (f"/api/slides/{job.deck_id}/results/{job.id}/image?v={image_revision}"
                         if current and job.kind == "translate" and job.status in {"completed", "partial"} else None),
            "canRetry": job.status in {"error", "partial"} and job.request_state != "uncertain"}


def translation_progress(db, deck):
    counts = dict(db.execute(select(SlideJob.status, func.count()).where(
        SlideJob.deck_id == deck.id, SlideJob.dedupe_key.like(VERSION + ":translate:%"),
        SlideJob.language == "zh").group_by(SlideJob.status)).all())
    return {"completed": counts.get("completed", 0), "pending": counts.get("queued", 0) + counts.get("running", 0),
            "failed": counts.get("error", 0) + counts.get("uncertain", 0) + counts.get("partial", 0), "total": deck.page_count}


def _upload_json(db, deck):
    return {"deck": deck_json(deck, db), "chunkBytes": CHUNK_BYTES,
            "receivedChunks": list(db.scalars(select(SlideChunk.chunk_index).where(SlideChunk.deck_id == deck.id))),
            "maxPages": MAX_PAGES}


@router.get("")
def list_decks(user: User = Depends(require_user), db: Session = Depends(get_db)):
    decks = db.scalars(select(SlideDeck).where(SlideDeck.user_id == user.id).order_by(SlideDeck.created_at.desc()).limit(150))
    return {"decks": [deck_json(deck, db) for deck in decks], "maxFileBytes": MAX_FILE_BYTES, "maxPages": MAX_PAGES}


@router.get("/coverage")
def match_coverage(user: User = Depends(require_user), db: Session = Depends(get_db)):
    decks = list(db.scalars(select(SlideDeck).where(SlideDeck.user_id == user.id)
                           .order_by(SlideDeck.created_at.desc())))
    serialized = [deck_json(deck, db) for deck in decks]
    linked = {note_id for deck in serialized for note_id in deck["noteIds"]}
    pending = any(deck["matchingStatus"] in {"pending", "matching"} for deck in serialized)
    notes = [] if pending else list(db.scalars(active_notes(user.id).where(Note.status == "ready")
                                               .order_by(Note.created_at.desc())))
    dates = {row.note_id: row.recording_date for row in db.scalars(select(RecordingInfo).where(
             RecordingInfo.note_id.in_([note.id for note in notes])))}
    return {"recordingWithoutSlides": [{"id": note.id, "title": note.title, "courseId": note.course_id,
                                        "recordingDate": dates.get(note.id)}
                                       for note in notes if note.id not in linked],
            "slidesWithoutRecording": [deck for deck in serialized if deck["status"] == "ready"
                                       and not deck["noteIds"] and deck["matchingStatus"] not in {"pending", "matching"}],
            "matchingPending": pending}


@router.get("/notes/{note_id}")
def note_decks(note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not db.scalar(active_notes(user.id).where(Note.id == note_id)):
        raise HTTPException(404, "找不到这节课堂录音。")
    ids = select(SlideNoteLink.deck_id).where(SlideNoteLink.user_id == user.id, SlideNoteLink.note_id == note_id)
    rejected = select(SlideLinkDecision.deck_id).where(SlideLinkDecision.user_id == user.id,
                  SlideLinkDecision.note_id == note_id, SlideLinkDecision.action == "reject")
    from sqlalchemy import or_
    decks = db.scalars(select(SlideDeck).where(SlideDeck.user_id == user.id,
                      or_(SlideDeck.note_id == note_id, SlideDeck.id.in_(ids)),
                      ~SlideDeck.id.in_(rejected)).order_by(SlideDeck.created_at))
    return {"decks": [deck_json(deck, db) for deck in decks]}


@router.delete("/{deck_id}/notes/{note_id}", dependencies=[Depends(require_csrf)])
def remove_note_link(deck_id: str, note_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    note = db.scalar(select(Note).where(Note.id == note_id, Note.user_id == user.id, Note.deleted_at.is_(None)))
    if note is None:
        raise HTTPException(404, "找不到这节课堂录音。")
    reject_link(db, deck, note)
    db.commit()
    return {"deck": deck_json(deck, db)}


@router.post("/uploads", dependencies=[Depends(require_csrf)])
def start_upload(body: UploadBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    filename = Path(body.filename.replace("\\", "/")).name
    if Path(filename).suffix.lower() not in {".pdf", ".pptx", ".ppt"}:
        raise ValueError("请选择 PPTX、PPT 或 PDF 课件。")
    note = None
    if body.noteId:
        note = db.scalar(select(Note).where(Note.id == body.noteId, Note.user_id == user.id, Note.deleted_at.is_(None)))
        if not note:
            raise HTTPException(404, "找不到关联的课堂录音。")
    course_id = body.courseId or (note.course_id if note else None)
    if course_id and not db.scalar(select(Course.id).where(Course.id == course_id, Course.user_id == user.id)):
        raise HTTPException(404, "找不到这门课程。")
    if note and body.courseId and note.course_id and body.courseId != note.course_id:
        raise ValueError("请选择这门课程对应的课堂录音。")
    _lock_user(db, user)
    existing = db.scalar(select(SlideDeck).where(SlideDeck.user_id == user.id, SlideDeck.sha256 == body.sha256)
                         .order_by(SlideDeck.created_at.desc()))
    if existing:
        if existing.expected_size != body.size:
            raise ValueError("文件信息不一致，请重新选择文件。")
        return _upload_json(db, existing)
    recent = db.scalar(select(func.count()).select_from(SlideDeck).where(SlideDeck.user_id == user.id,
                       SlideDeck.created_at > utcnow() - timedelta(hours=1)))
    if recent >= 60:
        raise HTTPException(429, "本小时上传的课件较多，请稍后再试。")
    deck = SlideDeck(user_id=user.id, course_id=course_id, note_id=body.noteId, filename=filename,
                     sha256=body.sha256, expected_size=body.size)
    db.add(deck)
    db.commit()
    return _upload_json(db, deck)


@router.put("/{deck_id}/chunks/{index}", dependencies=[Depends(require_csrf)])
def put_chunk(deck_id: str, index: int, body: ChunkBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    if deck.status != "uploading":
        return {"deck": deck_json(deck, db)}
    count = math.ceil(deck.expected_size / CHUNK_BYTES)
    if not 0 <= index < count:
        raise ValueError("课件分段编号不正确。")
    try:
        data = base64.b64decode(body.contentBase64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("课件分段不完整，请重新选择原文件继续上传。") from None
    expected = min(CHUNK_BYTES, deck.expected_size - index * CHUNK_BYTES)
    if len(data) != expected:
        raise ValueError("课件分段大小不正确，请重新选择原文件继续上传。")
    digest = hashlib.sha256(data).hexdigest()
    chunk = db.scalar(select(SlideChunk).where(SlideChunk.deck_id == deck.id, SlideChunk.chunk_index == index))
    if chunk and chunk.sha256 != digest:
        raise HTTPException(409, "这段课件与已上传的内容不同，请重新选择原文件。")
    if not chunk:
        db.add(SlideChunk(deck_id=deck.id, chunk_index=index, sha256=digest, size=len(data), content=data))
        db.commit()
    return {"received": index}


@router.post("/{deck_id}/uploaded", dependencies=[Depends(require_csrf)])
def finish_upload(deck_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    if deck.status != "uploading":
        return {"deck": deck_json(deck, db)}
    chunks = list(db.scalars(select(SlideChunk).where(SlideChunk.deck_id == deck.id).order_by(SlideChunk.chunk_index)))
    if [x.chunk_index for x in chunks] != list(range(math.ceil(deck.expected_size / CHUNK_BYTES))):
        raise ValueError("课件还没传完，请重新选择原文件继续上传。")
    content = b"".join(chunk.content for chunk in chunks)
    if len(content) != deck.expected_size or hashlib.sha256(content).hexdigest() != deck.sha256:
        raise ValueError("课件校验不一致，请重新选择原文件。")
    validate_file(deck.filename, content)
    deck.original, deck.status = content, "queued"
    db.add(SlideJob(user_id=user.id, deck_id=deck.id, kind="render", dedupe_key=f"render:{deck.id}"))
    db.execute(delete(SlideChunk).where(SlideChunk.deck_id == deck.id))
    db.commit()
    return {"deck": deck_json(deck, db)}


@router.get("/{deck_id}")
def read_deck(deck_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deck = _deck(db, user, deck_id)
    pages = db.execute(select(SlidePage.number, SlidePage.title).where(SlidePage.deck_id == deck.id).order_by(SlidePage.number))
    return {"deck": {**deck_json(deck, db), "translation": translation_progress(db, deck)},
            "pages": [{"number": row.number, "title": row.title} for row in pages]}


@router.get("/{deck_id}/original")
def original_file(deck_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deck = _deck(db, user, deck_id)
    if not deck.original:
        raise HTTPException(409, "课件还没传完。")
    return Response(deck.original, media_type="application/pdf" if deck.original.startswith(b"%PDF-") else "application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(deck.filename)}"})


@router.get("/{deck_id}/pages/{number}/image")
def page_image(deck_id: str, number: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    page = _page(db, _deck(db, user, deck_id), number)
    return Response(page.image, media_type="image/jpeg")


@router.get("/{deck_id}/results/{job_id}/image")
def translated_image(deck_id: str, job_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deck = _deck(db, user, deck_id)
    job = db.scalar(select(SlideJob).where(SlideJob.id == job_id, SlideJob.deck_id == deck.id,
                                         SlideJob.user_id == user.id, SlideJob.status.in_(["completed", "partial"])))
    saved = db.get(SlideLocalization, job.id) if job else None
    if not saved or not saved.image:
        raise HTTPException(404, "这页中文页图尚未生成。")
    return Response(saved.image, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{deck_id}/pages/{number}")
def read_page(deck_id: str, number: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    deck = _deck(db, user, deck_id)
    page = _page(db, deck, number)
    jobs = db.scalars(select(SlideJob).where(SlideJob.page_id == page.id).order_by(SlideJob.created_at))
    return {"page": {"number": page.number, "title": page.title, "text": page.text, "textMethod": page.text_method,
                     "width": page.width, "height": page.height,
                     "imageUrl": f"/api/slides/{deck.id}/pages/{number}/image",
                     "results": [job_json(job) for job in jobs]}}


@router.post("/{deck_id}/pages/{number}/generate", dependencies=[Depends(require_csrf)])
def generate_page(deck_id: str, number: int, body: GenerateBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    if deck.status != "ready":
        raise HTTPException(409, "课件页图还在准备，请稍后查看。")
    page = _page(db, deck, number)
    key = _generation_key(body, page)
    existing = db.scalar(select(SlideJob).where(SlideJob.dedupe_key == key))
    if existing:
        return {"result": job_json(existing)}
    queued = db.scalar(select(func.count()).select_from(SlideJob).where(SlideJob.user_id == user.id,
                       SlideJob.status.in_(["queued", "running"])))
    if queued >= 12:
        raise HTTPException(429, "有几页正在整理，稍后再翻到下一页。")
    excerpts = [(note, transcript_context(note, page.text)) for note in linked_notes(db, deck)] if body.mode == "explain" else []
    excerpts = [(note, text) for note, text in excerpts if text]
    context = "\n\n".join(f"[{note.title}]\n{text}" for note, text in excerpts)[:7000]
    job = SlideJob(user_id=user.id, deck_id=deck.id, page_id=page.id, kind=body.mode,
                   language=body.language, dedupe_key=key, context_snapshot=context,
                   context_source="、".join(note.title for note, _ in excerpts)[:240])
    db.add(job)
    db.commit()
    return {"result": job_json(job)}


def _generation_key(body, page):
    prefix = VERSION + ":" if body.mode == "translate" and body.language == "zh" else ""
    return f"{prefix}{body.mode}:{page.id}:{body.language}"


@router.post("/{deck_id}/generate", dependencies=[Depends(require_csrf)])
def generate_deck(deck_id: str, body: GenerateBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    if deck.status != "ready":
        raise HTTPException(409, "课件页图还在准备，请稍后查看。")
    if body.mode != "translate" or body.language != "zh":
        raise HTTPException(400, "整份课件支持中文原位翻译。")
    pages = list(db.scalars(select(SlidePage).where(SlidePage.deck_id == deck.id).order_by(SlidePage.number)))
    existing = set(db.scalars(select(SlideJob.dedupe_key).where(SlideJob.deck_id == deck.id)))
    missing = [p for p in pages if _generation_key(body, p) not in existing]
    queued = db.scalar(select(func.count()).select_from(SlideJob).where(
        SlideJob.user_id == user.id, SlideJob.status.in_(["queued", "running"])))
    if queued + len(missing) > 2 * MAX_PAGES:
        raise HTTPException(429, "已有两份课件正在翻译，请等处理完成。")
    for page in missing:
        db.add(SlideJob(user_id=user.id, deck_id=deck.id, page_id=page.id, kind="translate", language="zh",
                       dedupe_key=_generation_key(body, page)))
    db.commit()
    progress = translation_progress(db, deck)
    return {"queued": progress["pending"], "completed": progress["completed"], "total": progress["total"]}


@router.post("/{deck_id}/retry", dependencies=[Depends(require_csrf)])
def retry_render(deck_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    job = db.scalar(select(SlideJob).where(SlideJob.deck_id == deck.id, SlideJob.kind == "render"))
    if deck.status == "error" and job and job.status == "error":
        job.status, job.request_state, job.error = "queued", "prepared", ""
        deck.status, deck.error = "queued", ""
        db.commit()
    return {"deck": deck_json(deck, db)}


@router.post("/{deck_id}/results/{job_id}/retry", dependencies=[Depends(require_csrf)])
def retry_result(deck_id: str, job_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    _lock_user(db, user)
    deck = _deck(db, user, deck_id)
    job = db.scalar(select(SlideJob).where(SlideJob.id == job_id, SlideJob.deck_id == deck.id, SlideJob.user_id == user.id))
    if not job:
        raise HTTPException(404, "找不到这页的讲解。")
    if job.status == "uncertain" or job.request_state == "uncertain":
        raise HTTPException(409, "原请求的结果仍然未知，暂时不能重复提交。")
    if job.status in {"error", "partial"}:
        job.status, job.request_state, job.error, job.locked_at = "queued", "prepared", "", None
        db.commit()
    return {"result": job_json(job)}
