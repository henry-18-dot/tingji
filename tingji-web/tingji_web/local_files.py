"""Audio stays on this computer; only explicitly imported inbox files get jobs."""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from .config import get_settings
from .models import Note


AUDIO_SUFFIXES = {".mp3", ".mp4", ".wav", ".m4a", ".webm", ".ogg", ".opus", ".flac", ".aac"}


def confined_path(root: Path, relative: str) -> Path:
    if (not relative or "\\" in relative or ":" in relative or
            any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise HTTPException(400, "文件路径无效。")
    root = root.resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or result.suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(400, "文件路径无效。")
    return result


def audio_path(note: Note, *, require_exists: bool = True) -> Path:
    settings = get_settings()
    if not settings.local_mode:
        raise HTTPException(404, "本机录音仅在本地应用中提供。")
    if note.object_key.startswith("local/"):
        path = confined_path(settings.local_data_dir, note.object_key.removeprefix("local/"))
    elif note.object_key.startswith("legacy/"):
        relative = note.object_key.removeprefix("legacy/")
        # Several old note versions may share one recording; the note prefix
        # gives each database object key a unique value without copying audio.
        if relative.startswith(note.id + "/"):
            relative = relative.removeprefix(note.id + "/")
        path = confined_path(settings.legacy_data_dir, relative)
    else:
        raise HTTPException(404, "这条原文没有附带录音。")
    if require_exists and not path.is_file():
        raise HTTPException(404, "本机录音文件不存在。")
    return path


async def store_upload(note: Note, request: Request) -> None:
    if not note.object_key.startswith("local/") or note.uploaded_at:
        raise HTTPException(409, "这条录音已经完成上传。")
    destination = audio_path(note, require_exists=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    count = 0
    try:
        with temporary.open("xb") as handle:
            async for chunk in request.stream():
                count += len(chunk)
                if count > note.expected_size or count > get_settings().max_upload_bytes:
                    raise HTTPException(413, "上传录音超过登记大小。")
                handle.write(chunk)
        if count != note.expected_size:
            raise HTTPException(409, "上传文件大小与登记值不一致，请重新上传。")
        # The original becomes immutable as soon as its complete bytes are present.
        if destination.exists():
            if _file_digest(destination) != _file_digest(temporary):
                raise HTTPException(409, "已保存的原始录音不能被另一份文件覆盖。")
            return
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if _file_digest(destination) != _file_digest(temporary):
                raise HTTPException(409, "已保存的原始录音不能被另一份文件覆盖。") from None
    finally:
        temporary.unlink(missing_ok=True)


def _file_digest(path: Path) -> bytes:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").digest()


def inbox_path(file_id: str) -> Path:
    path = confined_path(get_settings().legacy_data_dir / "inbox", file_id)
    if not path.is_file():
        raise HTTPException(404, "收件夹里已找不到这份录音。")
    if not 0 < path.stat().st_size <= get_settings().max_upload_bytes:
        raise HTTPException(400, "录音为空或超过上传大小限制。")
    return path


def inbox_note_id(path: Path) -> str:
    stat = path.stat()
    identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "tingji-local-inbox:" + identity))


def copy_inbox_original(source: Path, note: Note) -> None:
    destination = audio_path(note, require_exists=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    try:
        with temporary.open("xb") as output, source.open("rb") as original:
            shutil.copyfileobj(original, output)
        if inbox_note_id(source) != note.id or temporary.stat().st_size != note.expected_size:
            raise HTTPException(409, "录音还在传输，请等传输完成后再导入。")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if _file_digest(destination) != _file_digest(temporary):
                raise HTTPException(409, "已保存的原始录音与收件夹文件不一致。") from None
    finally:
        temporary.unlink(missing_ok=True)


def list_inbox(db) -> list[dict]:
    root = get_settings().legacy_data_dir / "inbox"
    if not root.is_dir():
        return []
    result = []
    for item in root.rglob("*"):
        if len(result) >= 2000:
            break
        try:
            relative = item.relative_to(root).as_posix()
            if item.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            path = inbox_path(relative)
            stat = path.stat()
            result.append({"id": relative, "name": item.name, "size": stat.st_size,
                           "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                           "imported": db.get(Note, inbox_note_id(path)) is not None})
        except (HTTPException, OSError, ValueError):
            continue
    return sorted(result, key=lambda item: item["modifiedAt"], reverse=True)
