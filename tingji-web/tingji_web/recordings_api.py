from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import tos
from .auth import require_csrf, require_user, require_verified
from .config import get_settings
from .database import get_db
from .logic import clean_text, enqueue, note_json, resolve_prompt
from .models import Course, Note, NoteVersion, User, uid
from .recording_models import RecordingBatch, RecordingInfo, RecordingMerge, RecordingSegment
from .recordings import ALLOWED_AUDIO, segment_path, segments_for
from .schemas import StrictModel

router = APIRouter(prefix="/api")


class AudioFile(StrictModel):
    filename: str
    size: int


class BatchBody(StrictModel):
    requestId: str
    files: list[AudioFile]
    mode: Literal["combine", "separate"] = "combine"
    courseId: str | None = None
    recordingDate: date
    title: str = ""
    prompt: str | None = None


class MergeBody(StrictModel):
    requestId: str
    noteIds: list[str]
    courseId: str | None = None
    recordingDate: date
    mode: Literal["transcript", "summary"] = "transcript"


@router.post("/notes/merge", dependencies=[Depends(require_csrf)])
def merge_notes(body: MergeBody, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    key = clean_text(body.requestId, field="合并标识", maximum=80, required=True)
    if not 2 <= len(body.noteIds) <= 24 or len(set(body.noteIds)) != len(body.noteIds):
        raise ValueError("请选择 2–24 份不同的课堂原文。")
    digest = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(RecordingMerge).where(RecordingMerge.user_id == user.id, RecordingMerge.request_key == key))
    if previous:
        if previous.input_hash != digest:
            raise HTTPException(409, "合并内容已变化，请重新选择。")
        saved = db.get(Note, previous.note_id)
        if saved.deleted_at:
            raise HTTPException(409, "合并后的笔记已被移除。")
        return {"note": note_json(db, saved)}
    sources = []
    for nid in body.noteIds:
        note = db.get(Note, nid)
        if not note or note.user_id != user.id or note.deleted_at:
            raise HTTPException(404, "找不到所选笔记。")
        content = note.summary if body.mode == "summary" else note.transcript
        if not content.strip() or note.status in {"uploading", "queued", "preparing", "transcribing", "summarizing"}:
            raise HTTPException(409, "请等所选录音转写完成后再合并。")
        sources.append(note)
    course_ids = {n.course_id for n in sources}
    cid = body.courseId or (next(iter(course_ids)) if len(course_ids) == 1 else None)
    if len(course_ids - {None}) > 1:
        raise ValueError("请选择同一门课程的录音。")
    prompt, version = resolve_prompt(db, user, cid)
    transcript = "\n\n".join(f"【第{i+1}段：{n.source_name}】\n{n.transcript}" for i, n in enumerate(sources))
    if len(transcript) > 800000 or sum(n.duration or 0 for n in sources) > 18000:
        raise ValueError("合并内容过长，请按一堂课分组。")
    course = db.get(Course, cid) if cid else None
    nid = uid()
    title = f"{course.name if course else '课堂记录'}—待整理 {body.recordingDate.isoformat()}"
    merged = Note(id=nid, user_id=user.id, course_id=cid, title=title, source_name="课堂录音合并原文.txt",
                  object_key=f"transcript-only/{nid}", expected_size=0, transcript=transcript,
                  duration=sum(n.duration or 0 for n in sources) or None, status="queued", stage="原文已合并，等待整理",
                  prompt_snapshot=prompt, prompt_version_id=version, model=get_settings().deepseek_model)
    db.add(merged); db.flush()
    db.add(RecordingInfo(note_id=nid, recording_date=body.recordingDate.isoformat(), generated_title=title))
    db.add(RecordingMerge(note_id=nid, user_id=user.id, request_key=key, input_hash=digest, source_ids=body.noteIds))
    if body.mode == "summary":
        merged.summary = "\n\n---\n\n".join(f"# {note.title}\n\n{note.summary}" for note in sources)
        if len(merged.summary) > 800000:
            raise ValueError("合并的整理稿过长，请分组。")
        merged.status, merged.stage = "ready", "整理稿已按顺序拼接"
        merged.title = f"{course.name if course else '课堂记录'}—合并笔记 {body.recordingDate.isoformat()}"
        db.add(NoteVersion(user_id=user.id, note_id=nid, kind="merge", summary=merged.summary, prompt_snapshot=prompt, model=""))
    else:
        enqueue(db, merged, "summarize", prompt=prompt, prompt_version_id=version)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.scalar(select(RecordingMerge).where(RecordingMerge.user_id == user.id, RecordingMerge.request_key == key))
        if not previous or previous.input_hash != digest:
            raise HTTPException(409, "合并信息冲突，请重新选择。")
        merged = db.get(Note, previous.note_id)
    return {"note": note_json(db, merged)}


def owned_batch(db, user, batch_id):
    batch = db.get(RecordingBatch, batch_id)
    if not batch or batch.user_id != user.id:
        raise HTTPException(404, "找不到这组录音。")
    return batch


def batch_result(db, user, batch, csrf=""):
    settings = get_settings()
    notes, files = [], []
    for nid in batch.note_ids:
        note = db.get(Note, nid)
        if not note or note.user_id != user.id or note.deleted_at:
            raise HTTPException(409, "这组录音中的笔记已被移除。")
        notes.append(note_json(db, note, include_body=False))
        for part in segments_for(db, note):
            upload = {"url": f"/api/recording-segments/{part.id}/content", "method": "PUT", "headers": {"X-CSRF-Token": csrf}} if settings.local_mode else {
                "url": tos.presign(part.object_key, "PUT", 3600, user_id=user.id), "method": "PUT", "headers": {}}
            files.append({"id": part.id, "name": part.filename, "noteId": nid, "size": part.expected_size,
                          "confirmed": bool(note.uploaded_at), "upload": upload})
    return {"id": batch.id, "notes": notes, "files": files}


@router.post("/recordings/batches", dependencies=[Depends(require_csrf)])
def create_batch(body: BatchBody, request: Request, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    settings = get_settings()
    key = clean_text(body.requestId, field="上传标识", maximum=80, required=True)
    if not 1 <= len(body.files) <= 24:
        raise ValueError("一次可选择 1–24 段录音。")
    if not settings.local_mode and not settings.tos_configured:
        raise HTTPException(503, "录音存储尚未配置。")
    files = []
    for item in body.files:
        name = Path(item.filename.replace("\\", "/")).name[:240]
        if not name or Path(name).suffix.lower() not in ALLOWED_AUDIO:
            raise ValueError("请选择 MP3、M4A、WAV 等音频文件。")
        if not 0 < item.size <= settings.max_upload_bytes:
            raise ValueError("录音为空或超过单个文件大小限制。")
        files.append((name, item.size))
    if sum(size for _, size in files) > 4 * settings.max_upload_bytes:
        raise ValueError("这组文件过大，请分批上传。")
    course = db.get(Course, body.courseId) if body.courseId else None
    if body.courseId and (not course or course.user_id != user.id):
        raise HTTPException(404, "找不到这门课程。")
    digest = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    batch = db.scalar(select(RecordingBatch).where(RecordingBatch.user_id == user.id, RecordingBatch.request_key == key))
    if batch:
        if batch.input_hash != digest:
            raise HTTPException(409, "上传信息已变化，请重新选择文件。")
        return batch_result(db, user, batch, request.headers.get("X-CSRF-Token", ""))
    prompt, version = resolve_prompt(db, user, body.courseId, body.prompt)
    groups = [files] if body.mode == "combine" else [[item] for item in files]
    note_ids = []
    for group in groups:
        nid = uid()
        title = clean_text(body.title, field="标题", maximum=160) or f"{course.name if course else '课堂记录'}—待整理 {body.recordingDate.isoformat()}"
        parts = []
        for index, (name, size) in enumerate(group):
            sid = uid()
            suffix = Path(name).suffix.lower()
            object_key = f"local/recordings/{nid}/{sid}{suffix}" if settings.local_mode else f"tingji/users/{user.id}/recordings/{nid}/{sid}{suffix}"
            parts.append(RecordingSegment(id=sid, note_id=nid, position=index, filename=name, expected_size=size, object_key=object_key))
        note = Note(id=nid, user_id=user.id, course_id=body.courseId, title=title, source_name=group[0][0],
                    expected_size=sum(x[1] for x in group), object_key=parts[0].object_key,
                    prompt_snapshot=prompt, prompt_version_id=version, model=settings.deepseek_model,
                    status="uploading", stage=f"等待上传 {len(group)} 段录音")
        db.add(note)
        db.flush()
        db.add_all(parts)
        db.add(RecordingInfo(note_id=nid, recording_date=body.recordingDate.isoformat(), auto_title=not bool(body.title.strip()), generated_title=title))
        note_ids.append(nid)
    batch = RecordingBatch(user_id=user.id, request_key=key, input_hash=digest, note_ids=note_ids)
    db.add(batch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        batch = db.scalar(select(RecordingBatch).where(RecordingBatch.user_id == user.id, RecordingBatch.request_key == key))
        if not batch or batch.input_hash != digest:
            raise HTTPException(409, "上传信息已变化，请重新选择文件。")
    return batch_result(db, user, batch, request.headers.get("X-CSRF-Token", ""))


@router.put("/recording-segments/{segment_id}/content", dependencies=[Depends(require_csrf)])
async def upload_segment(segment_id: str, request: Request, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    if not get_settings().local_mode:
        raise HTTPException(404)
    segment = db.get(RecordingSegment, segment_id)
    note = db.get(Note, segment.note_id) if segment else None
    if not note or note.user_id != user.id or note.deleted_at:
        raise HTTPException(404)
    if note.uploaded_at:
        raise HTTPException(409, "录音已进入处理，原文件不再修改。")
    target = segment_path(segment)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + "." + uid() + ".partial")
    size = 0
    try:
        with partial.open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > segment.expected_size:
                    raise HTTPException(413, "文件大小超过登记值。")
                output.write(chunk)
        if size != segment.expected_size:
            raise HTTPException(409, "文件没有上传完整，请重试。")
        # A completed source is immutable, even before all other parts arrive.
        if target.exists():
            with target.open("rb") as old, partial.open("rb") as new:
                if hashlib.file_digest(old, "sha256").digest() != hashlib.file_digest(new, "sha256").digest():
                    raise HTTPException(409, "已保存的原录音不能被另一份文件覆盖。")
        else:
            try:
                os.link(partial, target)
            except FileExistsError:
                raise HTTPException(409, "文件正在保存，请重试确认。") from None
    finally:
        partial.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/recordings/batches/{batch_id}/uploaded", dependencies=[Depends(require_csrf)])
def complete_batch(batch_id: str, user: User = Depends(require_verified), db: Session = Depends(get_db)):
    settings = get_settings()
    batch = owned_batch(db, user, batch_id)
    if not settings.providers_configured:
        raise HTTPException(503, "转写或整理服务尚未配置。")
    notes = []
    # Verify all sources before adding any paid work.
    for nid in batch.note_ids:
        note = db.get(Note, nid)
        if not note or note.deleted_at or note.user_id != user.id:
            raise HTTPException(409, "笔记已被移除。")
        notes.append(note)
        if note.uploaded_at:
            continue
        for part in segments_for(db, note):
            path = segment_path(part) if settings.local_mode else None
            size = (path.stat().st_size if path.is_file() else -1) if path else tos.head_object(part.object_key, user_id=user.id)["size"]
            if size != part.expected_size:
                raise HTTPException(409, f"{part.filename} 尚未上传完整，请重试。")
    for note in notes:
        if not note.uploaded_at:
            note.uploaded_at = datetime.now(timezone.utc)
            note.status, note.stage = "queued", "录音已上传，等待处理"
            enqueue(db, note, "transcribe", prompt=note.prompt_snapshot, prompt_version_id=note.prompt_version_id)
    db.commit()
    return {"notes": [note_json(db, n) for n in notes]}
