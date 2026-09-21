from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Course, Job, Note, NoteVersion, PromptVersion, UsageRecord, User
from .preferences import apply_preferences
from .preferences_models import UserSettings


DEFAULT_PROMPT = (Path(__file__).with_name("default_prompt.md").read_text(encoding="utf-8-sig").strip())
OUTPUT_GUIDANCE = Path(__file__).with_name("output_guidance.md").read_text(encoding="utf-8-sig").strip()
_BUILTIN_PROMPT_HASHES = {
    "289b378f8b35c77c37704350fc4a60fb45d5570a3d17891cb82f757afa03c5aa",
    "86b447776afa6260b6531f0e39320689414789e8d3c9dd35bc36ddeb234283ba",
    "ee3b7975220ec8e692bf188abcc4f9f71867051de5d78b7ddec7affde2de84ee",
    "d34d2b58d73e999a121460aa3e116bd95bd02fb8962e5b80f7b96a0c56a7cf0f",
    "e7319797719edd7b0ad13d707eb64e359228747c0c6df7f8b368a72078dc7b91",
    "ebd4565f140848f220b0e2557c5c8463b891ccd2de0f0f0c329d238e024670cb",
    "cd57ab567efeced35a88f8b1ea6a8d25b2cdbf1d0a1dc82cdcaf37f4ca7ccf03",
    "3a6029b10203d0cae7d5e180f53b5e7e84290c7aca520aa6f0b408bdeb40342f",
}


def current_builtin_prompt(value: str) -> str:
    """Recognize exact shipped drafts; never rewrite a user's custom text."""
    normalized = value.replace("\r\n", "\n").strip()
    return DEFAULT_PROMPT if hashlib.sha256(normalized.encode()).hexdigest() in _BUILTIN_PROMPT_HASHES else value


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def usage_total(db: Session, *, user_id: str | None = None, note_id: str | None = None) -> float:
    """Return an estimated cost, never a provider invoice or account balance."""
    effective = func.coalesce(UsageRecord.actual_yuan, UsageRecord.estimated_yuan)
    query = select(func.coalesce(func.sum(effective), 0))
    if user_id:
        query = query.where(UsageRecord.user_id == user_id)
    if note_id:
        query = query.where(UsageRecord.note_id == note_id)
    amount = float(db.scalar(query) or 0)
    if not note_id:
        from .slide_models import SlideJob
        slide_query = select(func.coalesce(func.sum(func.coalesce(SlideJob.actual_yuan, SlideJob.estimated_yuan)), 0))
        if user_id:
            slide_query = slide_query.where(SlideJob.user_id == user_id)
        amount += float(db.scalar(slide_query) or 0)
        from .matching_models import SlideMatchJob
        match_query = select(func.coalesce(func.sum(func.coalesce(SlideMatchJob.actual_yuan, SlideMatchJob.estimated_yuan)), 0))
        if user_id:
            match_query = match_query.where(SlideMatchJob.user_id == user_id)
        amount += float(db.scalar(match_query) or 0)
    return round(amount, 6)


def user_json(db: Session, user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name,
            "verified": bool(user.verified_at), "defaultPrompt": user.default_prompt,
            "language": user.language, "usageYuan": usage_total(db, user_id=user.id),
            "usageEstimated": True, "isAdmin": user.is_admin}


def course_json(course: Course) -> dict:
    return {"id": course.id, "name": course.name, "term": course.term,
            "teacher": course.teacher, "hotwords": course.hotwords,
            "prompt": course.prompt, "createdAt": iso(course.created_at)}


def version_json(version: NoteVersion) -> dict:
    return {"id": version.id, "kind": version.kind, "summary": version.summary,
            "promptSnapshot": version.prompt_snapshot, "model": version.model,
            "createdAt": iso(version.created_at)}


def note_json(db: Session, note: Note, *, include_body: bool = True) -> dict:
    from .audio_storage import available_audio_key
    versions = list(db.scalars(select(NoteVersion).where(
        NoteVersion.note_id == note.id, NoteVersion.user_id == note.user_id
    ).order_by(NoteVersion.created_at.desc()))) if include_body else []
    result = {"id": note.id, "title": note.title, "courseId": note.course_id,
              "importedAt": iso(note.uploaded_at or note.created_at),
              "hasTranscript": bool(note.transcript.strip()), "hasSummary": bool(note.summary.strip()),
              "hasAudio": bool(available_audio_key(db, note)),
              "sourceName": note.source_name, "status": note.status, "stage": note.stage,
              "error": note.error, "createdAt": iso(note.created_at), "updatedAt": iso(note.updated_at),
              "duration": note.duration, "usageYuan": usage_total(db, note_id=note.id),
              "usageEstimated": True, "model": note.model, "versions": [version_json(x) for x in versions]}
    from .recordings import recording_json
    result.update(recording_json(db, note))
    if include_body:
        result.update(transcript=note.transcript, summary=note.summary,
                      promptSnapshot=note.prompt_snapshot)
        from .models import ProviderRequest
        request = db.scalar(select(ProviderRequest).where(
            ProviderRequest.note_id == note.id, ProviderRequest.user_id == note.user_id,
            ProviderRequest.provider == "deepseek", ProviderRequest.state == "completed",
            ProviderRequest.operation_key.endswith(":final")
        ).order_by(ProviderRequest.created_at.desc()))
        messages = (((request.metadata_json or {}).get("input") or {}).get("messages") or []) if request else []
        result["effectivePrompt"] = "\n\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
    return result


def resolve_prompt(db: Session, user: User, course_id: str | None, override: str | None = None) -> tuple[str, str]:
    course = None
    if course_id:
        course = db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id))
        if course is None:
            raise ValueError("找不到这门课程。")
    saved_settings = db.get(UserSettings, user.id)
    base = ((course.prompt or "").strip() if course else "") or (user.default_prompt.strip() if saved_settings is None else "") or DEFAULT_PROMPT
    prompt = apply_preferences(db, user, current_builtin_prompt(base), override=override or "")
    prompt += "\n\n" + OUTPUT_GUIDANCE
    if not prompt or len(prompt) > 45000:
        raise ValueError("合并后的整理要求最多 45000 个字符。")
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    version = db.scalar(select(PromptVersion).where(
        PromptVersion.user_id == user.id, PromptVersion.sha256 == digest
    ))
    if version is None:
        version = PromptVersion(user_id=user.id, course_id=course.id if course else None,
                                prompt=prompt, sha256=digest)
        db.add(version)
        db.flush()
    return prompt, version.id


def enqueue(db: Session, note: Note, kind: str, *, prompt: str = "", prompt_version_id: str | None = None) -> Job:
    if kind == "transcribe":
        dedupe = f"transcribe:{note.id}"
    else:
        dedupe = f"summarize:{note.id}:{hashlib.sha256((prompt + str(note.updated_at)).encode()).hexdigest()[:24]}"
    existing = db.scalar(select(Job).where(Job.dedupe_key == dedupe))
    if existing:
        return existing
    job = Job(user_id=note.user_id, note_id=note.id, kind=kind, dedupe_key=dedupe,
              status="queued", stage="queued", prompt_snapshot=prompt,
              prompt_version_id=prompt_version_id, model=get_settings().deepseek_model if kind == "summarize" else "")
    db.add(job)
    db.flush()
    return job


def clean_text(value: str, *, field: str, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if (required and not text) or len(text) > maximum:
        raise ValueError(f"{field}长度不正确。")
    return text
