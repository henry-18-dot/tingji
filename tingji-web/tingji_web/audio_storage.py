"""Bounded targets and ownership checks for explicitly confirmed audio cleanup."""
from __future__ import annotations
import hashlib
from pathlib import Path
from sqlalchemy import or_, select
from .audio_storage_models import AudioRemoval
from .config import get_settings
from .models import Job, Note, ProviderRequest
from .recording_models import RecordingSegment

SCOPES = {"cache": "转换缓存", "local": "本机应用原录音", "cloud_source": "云端原录音", "cloud_prepared": "云端标准音频"}


def locator_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def audio_removed(db, note, key):
    return db.scalar(select(AudioRemoval.id).where(AudioRemoval.user_id == note.user_id, AudioRemoval.locator_hash == locator_hash(key))) is not None


def available_audio_key(db, note):
    segments = list(db.scalars(select(RecordingSegment).where(RecordingSegment.note_id == note.id)))
    # Multi-part playback uses its combined standard audio. Keep an original as
    # a fallback only for a single-file recording, not a misleading partial class.
    keys = [note.prepared_object_key] if len(segments) > 1 else [note.object_key, note.prepared_object_key]
    return next((key for key in keys if key and not key.startswith("transcript-only/") and not audio_removed(db, note, key)), None)


def blocked_reason(db, note):
    if note.status in {"uploading", "uploaded", "queued", "preparing", "transcribing", "summarizing", "uncertain"}:
        return "录音尚在处理或结果待核对，暂不清理。"
    if not note.transcript.strip() or not note.summary.strip():
        return "完整原文与整理稿尚未保存，暂不清理原录音。"
    if db.scalar(select(Job.id).where(Job.note_id == note.id, Job.status.in_(["queued", "running", "waiting", "uncertain"]))):
        return "仍有活动任务或待核对任务，暂不清理。"
    if db.scalar(select(ProviderRequest.id).where(ProviderRequest.note_id == note.id,
        ProviderRequest.state.not_in(["completed", "failed", "rejected"]))):
        return "供应商任务尚未确认结束，暂不清理。"
    return ""


def confined_local(note, key):
    cfg = get_settings()
    if not cfg.local_mode:
        raise ValueError("本机原录音只能在电脑应用中清理。")
    if key.startswith(f"local/recordings/{note.id}/"):
        root = cfg.local_data_dir.resolve()
        relative = key.removeprefix("local/")
    elif key.startswith(f"legacy/{note.id}/audio/"):
        root = cfg.legacy_data_dir.resolve()
        relative = key.removeprefix(f"legacy/{note.id}/")
    else:
        raise ValueError("这份本机文件不在当前笔记的录音目录内。")
    if "\\" in relative or ":" in relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("录音路径无效。")
    raw = root / relative
    path = raw.resolve()
    if not path.is_relative_to(root) or path.suffix.lower() not in {".mp3", ".mp4", ".m4a", ".wav", ".webm", ".ogg", ".opus", ".flac", ".aac", ".amr", ".wma"}:
        raise ValueError("录音路径超出允许范围。")
    for part in [raw, *raw.parents]:
        if part == root:
            break
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("链接文件或目录不能通过此入口清理。")
    return path


def cloud_key(note, key):
    prefix = f"tingji/users/{note.user_id}/recordings/{note.id}/"
    if not key.startswith(prefix) or any(c in key for c in ("\\", "%", "\x00")):
        raise ValueError("云端对象不属于当前账号的这条录音。")
    tail = key.removeprefix(prefix)
    if not tail or "/" in tail or tail in {".", ".."}:
        raise ValueError("云端对象路径无效。")
    return key


def references(db, note, scope):
    if scope == "cache":
        return []
    if scope == "cloud_prepared":
        return [(note.prepared_object_key, "整堂课标准音频.mp3")] if note.prepared_object_key else []
    segments = list(db.scalars(select(RecordingSegment).where(RecordingSegment.note_id == note.id).order_by(RecordingSegment.position)))
    values = [(s.object_key, s.filename) for s in segments] if segments else [(note.object_key, note.source_name)]
    return [(key, name) for key, name in values if key and (key.startswith(("local/", "legacy/")) if scope == "local" else key.startswith("tingji/"))]


def shared(db, note, key, scope):
    if db.scalar(select(Note.id).where(Note.id != note.id, or_(Note.object_key == key, Note.prepared_object_key == key))):
        return True
    if db.scalar(select(RecordingSegment.id).where(RecordingSegment.note_id != note.id, RecordingSegment.object_key == key)):
        return True
    if scope == "local":
        target = confined_local(note, key)
        if target.exists() and target.stat().st_nlink > 1:
            return True
        # Legacy note IDs can point at the same physical recording.
        others = db.scalars(select(Note).where(Note.id != note.id, Note.object_key.like("legacy/%")))
        for other in others:
            try:
                if confined_local(other, other.object_key) == target:
                    return True
            except ValueError:
                continue
    return False


def local_stat(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("目标不是普通录音文件。")
    value = path.stat()
    return {"size": value.st_size, "mtimeNs": value.st_mtime_ns, "device": value.st_dev, "inode": value.st_ino}
