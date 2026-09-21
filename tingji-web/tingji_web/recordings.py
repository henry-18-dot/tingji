from __future__ import annotations

import re
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .models import Course, Note
from .recording_models import RecordingInfo, RecordingSegment

ALLOWED_AUDIO = {".mp3", ".m4a", ".wav", ".webm", ".ogg", ".flac", ".mp4", ".aac", ".opus", ".amr", ".wma"}


def queue_imported_audio(db: Session, note: Note) -> None:
    from . import local_files, tos
    from .logic import enqueue, resolve_prompt
    from .models import Job, User
    settings = get_settings()
    if note.status not in {"uploaded", "ready"} or note.transcript.strip() or db.scalar(select(Job.id).where(Job.note_id == note.id)):
        raise ValueError("请继续原任务，或从现有原文重新整理。")
    if not settings.providers_configured:
        raise ValueError("语音、AI 或对象存储服务尚未配置。")
    if settings.local_mode:
        local_files.audio_path(note)
    else:
        head = tos.head_object(note.object_key, user_id=note.user_id)
        if head["size"] != note.expected_size:
            raise ValueError("录音上传尚未完成。")
    user = db.get(User, note.user_id)
    prompt, version = resolve_prompt(db, user, note.course_id, None)
    note.status, note.stage, note.error = "queued", "等待转录", ""
    note.prompt_snapshot, note.prompt_version_id = prompt, version
    enqueue(db, note, "transcribe", prompt=prompt, prompt_version_id=version)


def segments_for(db: Session, note: Note) -> list[RecordingSegment]:
    return list(db.scalars(select(RecordingSegment).where(RecordingSegment.note_id == note.id).order_by(RecordingSegment.position)))


def segment_path(segment: RecordingSegment) -> Path:
    root = Path(get_settings().local_data_dir).resolve()
    path = (root / segment.object_key.removeprefix("local/")).resolve()
    if not segment.object_key.startswith("local/recordings/") or not path.is_relative_to(root):
        raise ValueError("录音路径无效。")
    return path


def update_lecture_title(db: Session, note: Note, summary: str) -> None:
    info = db.get(RecordingInfo, note.id)
    if not info or not info.auto_title or (info.generated_title and note.title != info.generated_title):
        return
    course = db.get(Course, note.course_id) if note.course_id else None
    course_name = course.name if course and course.user_id == note.user_id else "课堂记录"
    headings = re.findall(r"^#{1,2}\s+(.+)$", summary, re.M)
    topic = next((x for x in headings if x.strip() not in {"课堂笔记", "整理稿", "课程笔记", "概述", "正文"}), "课堂内容")
    topic = re.sub(r"[\[\]*`_#]|https?://\S+", "", topic).strip()
    topic = topic.removeprefix(course_name).strip(" ·—-：: ")
    topic = re.split(r"[。；;\n]", topic)[0][:24].strip() or "课堂内容"
    title = "—".join(x for x in (course_name, topic, info.recording_date) if x)[:160]
    note.title = info.generated_title = title


def recording_json(db: Session, note: Note) -> dict:
    info = db.get(RecordingInfo, note.id)
    segments = segments_for(db, note)
    return {"recordingDate": info.recording_date if info else None,
            "recordingGaps": info.gaps if info else [],
            "segments": [{"id": s.id, "name": s.filename, "position": s.position, "duration": s.duration} for s in segments]}
