"""Account-scoped archive, trash recovery, and safe bulk processing."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_csrf, require_user
from .config import get_settings
from .database import get_db
from .library_models import LibraryOperation, NoteLibrary
from .logic import iso, note_json, resolve_prompt
from .models import Job, Note, ProviderRequest, User, utcnow
from .schemas import StrictModel

router = APIRouter(prefix="/api/library", tags=["library"])
ACTIVE = {"queued", "running", "waiting"}


class LibraryAction(StrictModel):
    requestId: str = Field(min_length=1, max_length=100)
    action: Literal["archive", "unarchive", "trash", "restore", "summarize", "resume"]
    noteIds: list[str] = Field(min_length=1, max_length=100)


def library_json(db: Session, note: Note, item: NoteLibrary | None = None) -> dict:
    item = item or db.get(NoteLibrary, note.id)
    return {**note_json(db, note, include_body=False), "archivedAt": iso(item.archived_at) if item else None,
            "deletedAt": iso(note.deleted_at), "hasTranscript": bool(note.transcript.strip()),
            "hasSummary": bool(note.summary.strip())}


@router.get("")
def get_library(view: Literal["active", "archived", "trash"] = Query("active"),
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    query = select(Note, NoteLibrary).outerjoin(NoteLibrary, NoteLibrary.note_id == Note.id).where(Note.user_id == user.id)
    if view == "trash":
        query = query.where(Note.deleted_at.is_not(None))
    else:
        query = query.where(Note.deleted_at.is_(None))
        query = query.where(NoteLibrary.archived_at.is_not(None) if view == "archived" else NoteLibrary.archived_at.is_(None))
    rows = db.execute(query.order_by(Note.updated_at.desc())).all()
    return {"notes": [library_json(db, note, item) for note, item in rows]}


def _item(db: Session, note: Note) -> NoteLibrary:
    item = db.get(NoteLibrary, note.id)
    if item is None:
        item = NoteLibrary(note_id=note.id, user_id=note.user_id, before_trash={})
        db.add(item)
        db.flush()
    return item


def _jobs(db: Session, note: Note):
    return list(db.scalars(select(Job).where(Job.note_id == note.id, Job.user_id == note.user_id).order_by(Job.created_at.desc())))


def _unknown_request(db: Session, note: Note):
    return db.scalar(select(ProviderRequest.id).where(ProviderRequest.note_id == note.id,
        ProviderRequest.user_id == note.user_id,
        ProviderRequest.state.in_(["dispatched", "uncertain", "submit_unknown", "accepted", "querying", "queued", "processing"]))) is not None


def _restore(db: Session, note: Note, item: NoteLibrary):
    if note.deleted_at is None:
        return
    note.deleted_at = None
    # Library deletion keeps task state intact. Legacy deletion changed it to
    # 'error'; reconstruct only that documented legacy state, never schedule work.
    if item.before_trash:
        note.status = item.before_trash.get("status", note.status)
        note.stage = item.before_trash.get("stage", note.stage)
        note.error = item.before_trash.get("error", note.error)
    elif note.stage == "已移入回收状态":
        note.error = ""
        if _unknown_request(db, note):
            note.status, note.stage = "uncertain", "原任务结果待核对"
        elif note.summary.strip():
            note.status, note.stage = "ready", "笔记已恢复"
        elif note.transcript.strip():
            note.status, note.stage = "transcribed", "原文已恢复，可重新整理"
        else:
            note.status, note.stage = ("uploaded", "录音已恢复") if note.uploaded_at else ("uploading", "等待上传")
    item.before_trash = {}


def _queue_summary(db: Session, user: User, note: Note, operation: LibraryAction):
    if not note.transcript.strip():
        raise ValueError("尚无转写原文，不能重新整理。")
    if any(job.status in ACTIVE for job in _jobs(db, note)):
        raise ValueError("正在处理，已有任务会继续完成。")
    if _unknown_request(db, note):
        raise ValueError("原任务结果未知，请先核对；没有重新发送。")
    if not get_settings().deepseek_api_key:
        raise ValueError("DeepSeek 尚未配置。")
    prompt, version_id = resolve_prompt(db, user, note.course_id, None)
    key = hashlib.sha256((user.id + ":" + operation.requestId + ":" + note.id).encode()).hexdigest()
    db.add(Job(user_id=user.id, note_id=note.id, kind="summarize", dedupe_key="library:" + key,
               status="queued", stage="queued", prompt_snapshot=prompt, prompt_version_id=version_id,
               model=get_settings().deepseek_model))
    note.prompt_snapshot, note.prompt_version_id = prompt, version_id
    note.status, note.stage, note.error = "queued", "等待重新整理", ""


def _resume(db: Session, note: Note):
    jobs = _jobs(db, note)
    if not jobs and note.status in {"uploaded", "ready"} and not note.transcript.strip():
        from .recordings import queue_imported_audio
        queue_imported_audio(db, note)
        return
    if any(job.status in ACTIVE for job in jobs):
        raise ValueError("正在处理，已有任务会继续完成。")
    job = next((j for j in jobs if j.kind != "reinforce"), None)
    if job is None or job.status not in {"error", "uncertain", "cancelled"}:
        raise ValueError("没有可恢复的任务；已有原文可选择重新整理。")
    requests = list(db.scalars(select(ProviderRequest).where(ProviderRequest.job_id == job.id, ProviderRequest.user_id == note.user_id)))
    if any(r.provider == "deepseek" and r.state in {"dispatched", "uncertain"} for r in requests):
        raise ValueError("整理结果未知，不能自动重发。")
    if any(r.provider == "volcengine-asr" and r.state in {"not_found", "rejected", "failed"} for r in requests):
        raise ValueError("原语音任务不能安全恢复，没有重新提交录音。")
    if job.kind == "transcribe" and not requests:
        raise ValueError("原语音任务尚无受理记录，请单独打开这条录音处理。")
    if not requests and job.attempts:
        raise ValueError("原任务缺少请求记录，请单独核对。")
    job.status, job.stage, job.error = "queued", "resume", ""
    job.available_at = utcnow()
    note.status, note.stage, note.error = "queued", "恢复原任务", ""


@router.post("/actions", dependencies=[Depends(require_csrf)])
def apply_library_action(body: LibraryAction, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if body.action in {"summarize", "resume"} and not user.verified_at:
        raise HTTPException(403, "请先完成邮箱验证。")
    ids = list(dict.fromkeys(body.noteIds))
    # Account lock makes action receipts and job creation one durable operation.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    digest = hashlib.sha256(json.dumps({"action": body.action, "noteIds": ids}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(LibraryOperation).where(LibraryOperation.user_id == user.id, LibraryOperation.request_id == body.requestId))
    if previous:
        if previous.payload_hash != digest:
            raise HTTPException(409, "这次操作的内容已改变，请重新选择。")
        return previous.result
    notes = {n.id: n for n in db.scalars(select(Note).where(Note.id.in_(ids), Note.user_id == user.id).with_for_update())}
    if len(notes) != len(ids):
        raise HTTPException(404, "有笔记不存在或不属于当前账号。")
    results = []
    for note_id in ids:
        note = notes[note_id]
        try:
            if body.action != "restore" and note.deleted_at is not None:
                raise ValueError("请先从回收站恢复。")
            if body.action in {"archive", "unarchive"}:
                _item(db, note).archived_at = utcnow() if body.action == "archive" else None
            elif body.action == "trash":
                if any(job.status in ACTIVE for job in _jobs(db, note)):
                    raise ValueError("正在处理，完成后可移入回收站。")
                _item(db, note).before_trash = {"status": note.status, "stage": note.stage, "error": note.error}
                note.deleted_at = utcnow()
            elif body.action == "restore":
                _restore(db, note, _item(db, note))
            elif body.action == "summarize":
                _queue_summary(db, user, note, body)
            elif body.action == "resume":
                _resume(db, note)
            results.append({"noteId": note.id, "title": note.title, "ok": True})
        except ValueError as exc:
            results.append({"noteId": note.id, "title": note.title, "ok": False, "message": str(exc)})
    result = {"items": results, "completed": sum(item["ok"] for item in results), "total": len(ids)}
    db.add(LibraryOperation(user_id=user.id, request_id=body.requestId, payload_hash=digest, result=result))
    db.commit()
    return result
