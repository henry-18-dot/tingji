"""Preview exact owned files, then delete only explicitly confirmed targets."""
from __future__ import annotations
from datetime import timedelta, timezone
from typing import Literal
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from . import audio_storage as storage
from .audio_storage_models import AudioCleanupPlan, AudioRemoval
from .audio_storage_tos import cloud_delete, cloud_stat
from .auth import require_csrf, require_user
from .config import get_settings
from .database import get_db
from .logic import iso
from .models import Note, User, utcnow
from .schemas import StrictModel

router = APIRouter(prefix="/api/audio-storage", tags=["audio-storage"])


class PreviewBody(StrictModel):
    scope: Literal["cache", "local", "cloud_source", "cloud_prepared"]
    noteIds: list[str] = Field(min_length=1, max_length=20)


class ExecuteBody(StrictModel):
    targetId: str = Field(min_length=1, max_length=36)
    confirm: Literal[True]


def plan_json(plan):
    return {"planId": plan.id, "scope": plan.scope, "scopeLabel": storage.SCOPES[plan.scope],
            "noteCount": len(set(t["noteId"] for t in plan.targets)), "createdAt": iso(plan.created_at),
            "targets": [{k: v for k, v in target.items() if k != "identity"} for target in plan.targets], "skipped": plan.skipped}


@router.get("/scopes")
def scopes(user=Depends(require_user)):
    return {"scopes": [{"id": key, "label": value} for key, value in storage.SCOPES.items()
                       if key != "local" or get_settings().local_mode]}


@router.post("/preview", dependencies=[Depends(require_csrf)])
def preview(body: PreviewBody, user=Depends(require_user), db=Depends(get_db)):
    if body.scope == "local" and not get_settings().local_mode:
        raise HTTPException(400, "本机原录音只能在电脑应用中清理。")
    ids = list(dict.fromkeys(body.noteIds))
    notes = list(db.scalars(select(Note).where(Note.user_id == user.id, Note.id.in_(ids))))
    if len(notes) != len(ids):
        raise HTTPException(404, "找不到所选录音。")
    targets, skipped = [], []
    for note in notes:
        reason = storage.blocked_reason(db, note) if body.scope != "cache" else "当前没有登记在这些笔记下的持久转换缓存。"
        if reason:
            skipped.append({"noteId": note.id, "title": note.title, "message": reason})
            continue
        refs = storage.references(db, note, body.scope)
        if not refs:
            skipped.append({"noteId": note.id, "title": note.title, "message": "这个位置没有登记的录音。"})
        for key, name in refs:
            try:
                if storage.audio_removed(db, note, key):
                    continue
                path = storage.confined_local(note, key) if body.scope == "local" else storage.cloud_key(note, key)
                if storage.shared(db, note, key, body.scope):
                    raise ValueError("其他笔记或文件链接仍在使用这份录音，已保留。")
                identity = storage.local_stat(path) if body.scope == "local" else cloud_stat(key, user_id=user.id)
                if identity is None:
                    raise ValueError("此位置已经没有这份录音。")
                if identity["size"] < 0 or (body.scope != "local" and not identity.get("etag")):
                    raise ValueError("录音大小或对象标记没有读清，请稍后重新预览。")
                if len(targets) >= 80:
                    raise ValueError("本次文件较多，请减少所选笔记后清理。")
                targets.append({"id": str(uuid.uuid4()), "noteId": note.id, "title": note.title,
                    "filename": name, "key": key, "location": str(path), "size": identity["size"],
                    "versionId": identity.get("versionId", ""), "identity": identity, "state": "pending", "message": ""})
            except (ValueError, OSError) as exc:
                skipped.append({"noteId": note.id, "title": note.title, "filename": name, "message": str(exc)})
    plan = AudioCleanupPlan(user_id=user.id, scope=body.scope, note_ids=ids, targets=targets, skipped=skipped)
    db.add(plan)
    db.commit()
    return plan_json(plan)


@router.post("/{plan_id}/execute", dependencies=[Depends(require_csrf)])
def execute(plan_id: str, body: ExecuteBody, user=Depends(require_user), db=Depends(get_db)):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    plan = db.scalar(select(AudioCleanupPlan).where(AudioCleanupPlan.id == plan_id, AudioCleanupPlan.user_id == user.id).with_for_update())
    if plan is None:
        raise HTTPException(404, "找不到这次清理预览。")
    created = plan.created_at if plan.created_at.tzinfo else plan.created_at.replace(tzinfo=timezone.utc)
    if utcnow() - created > timedelta(minutes=15):
        raise HTTPException(409, "清理预览已过期，请重新查看要删除的文件。")
    targets = [dict(t) for t in plan.targets]
    target = next((t for t in targets if t["id"] == body.targetId), None)
    if target is None:
        raise HTTPException(404, "这份文件不在已预览的清理范围内。")
    if target["state"] == "removed":
        return plan_json(plan)
    note = db.scalar(select(Note).where(Note.id == target["noteId"], Note.user_id == user.id).with_for_update())
    try:
        if note is None:
            raise ValueError("录音记录已变化，请重新预览。")
        reason = storage.blocked_reason(db, note)
        if reason:
            raise ValueError(reason)
        key = target["key"]
        if not any(k == key for k, _ in storage.references(db, note, plan.scope)):
            raise ValueError("录音引用已变化，请重新预览。")
        if storage.shared(db, note, key, plan.scope):
            raise ValueError("这份录音正在被其他笔记使用，已保留。")
        if plan.scope == "local":
            path = storage.confined_local(note, key)
            identity = storage.local_stat(path)
            if identity is not None:
                if identity != target["identity"]:
                    raise ValueError("录音文件已变化，请重新预览。")
                path.unlink()
                if path.exists():
                    raise ValueError("录音文件仍然存在，请重试。")
        else:
            storage.cloud_key(note, key)
            cloud_delete(key, user_id=user.id, identity=target["identity"])
        if not storage.audio_removed(db, note, key):
            db.add(AudioRemoval(user_id=user.id, note_id=note.id, scope=plan.scope, locator_hash=storage.locator_hash(key)))
        if plan.scope == "cloud_prepared" and note.prepared_object_key == key:
            note.prepared_object_key = None
        target["state"], target["message"] = "removed", "此位置的录音已删除。"
    except (ValueError, OSError) as exc:
        target["state"], target["message"] = "failed", str(exc)
    plan.targets = targets
    db.commit()
    return plan_json(plan)
