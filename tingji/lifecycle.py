"""Apply retention and explicit, storage-scoped local cleanup policies."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import storage

TEXT_CACHE_SUFFIXES = {'.json', '.tmp', '.txt', '.md'}
AUDIO_SUFFIXES = {'.mp3', '.mp4', '.wav', '.m4a', '.mka', '.webm', '.ogg', '.opus',
                  '.flac', '.mov', '.avi', '.mkv', '.aac', '.wma', '.syncing'}


def _instant(value=None):
    value = value or storage.now()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value):
    return _instant(value).isoformat(timespec='seconds')


def _child(base, name):
    if not isinstance(name, str) or not name or name in ('.', '..') or Path(name).name != name:
        raise ValueError('录音文件路径无效。')
    base = base.resolve()
    candidate = base / name
    target = candidate.resolve()
    if (not base.is_relative_to(storage.DATA.resolve()) or target != candidate
            or target == base or not target.is_relative_to(base)):
        raise ValueError('录音文件路径无效。')
    return target


def _job_folder(note_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', str(note_id)):
        raise ValueError('记录编号无效。')
    return _child(storage.DATA / 'jobs', note_id)


def _files(folder, suffixes):
    if not folder.exists():
        return []
    selected = []
    for path in folder.rglob('*'):
        resolved = path.resolve()
        if resolved != path.absolute() or not resolved.is_relative_to(folder.resolve()):
            raise ValueError('录音缓存路径无效。')
        if resolved.is_file() and path.suffix.lower() in suffixes:
            selected.append(resolved)
    return selected


def _processing(note_id, note):
    """Return whether an ASR job can still read a local recording."""
    from . import jobs
    task = note.get('asrTask') or {}
    return (note_id in jobs.ACTIVE or note.get('status') in ('transcribing', 'summarizing')
            or (task.get('requestId') and task.get('state') not in ('completed', 'failed', 'rejected')))


def audio_available(note):
    if note.get('audioDeletedAt') or not note.get('audioFile'):
        return False
    try:
        return _child(storage.DATA / 'audio', note['audioFile']).is_file()
    except (ValueError, OSError):
        return False


def public_fields(note, at=None):
    available = audio_available(note)
    due = False
    if available and note.get('audioArchivedAt') and note.get('audioDeleteDueAt'):
        try:
            due = _instant(at) >= _instant(note['audioDeleteDueAt'])
        except (ValueError, TypeError):
            pass
    return {'audioAvailable': available, 'cleanupDue': due}


def archive_audio(note_id, at=None):
    with storage.LOCK:
        note = storage.get_note(note_id)
        if not note.get('asrComplete') or not note.get('audioFile') or note.get('audioDeletedAt'):
            return note
        if note.get('audioArchivedAt') and note.get('audioDeleteDueAt'):
            return note
        archived = _instant(note.get('audioArchivedAt') or at)
        return storage.update_note(note_id, audioArchivedAt=_iso(archived),
                                   audioDeleteDueAt=_iso(archived + timedelta(days=7)))


def _clear_receipt(value, note_id, purged_at):
    if isinstance(value, list):
        return [_clear_receipt(item, note_id, purged_at) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _clear_receipt(item, note_id, purged_at) for key, item in value.items()}
    if value.get('id') == note_id:
        result.update(transcript='', segments=[], chat=[], transcriptPurgedAt=purged_at)
    return result


def purge_transcript(note_id):
    """The note commit precedes cache deletion; repeating this finishes interrupted cleanup."""
    with storage.LOCK:
        note = storage.get_note(note_id)
        if (not str(note.get('summary') or '').strip() or note.get('summaryStale')
                or note.get('asrComplete') is False):
            return note
        if note.get('template') in ('general', 'lecture'):
            from .prompt_lab import preserve
            preserve(note)
        files = _files(_job_folder(note_id), TEXT_CACHE_SUFFIXES)
        stamp = note.get('transcriptPurgedAt') or storage.now()
        if not note.get('transcriptPurgedAt') or note.get('transcript') or note.get('segments') or note.get('chat'):
            note = storage.update_note(note_id, transcript='', segments=[], chat=[],
                                       transcriptEdited=False, transcriptPurgedAt=stamp)
        with storage.connect() as db:
            for operation_id, body in db.execute('SELECT operation_id,response FROM sync_receipts').fetchall():
                old = json.loads(body)
                clean = _clear_receipt(old, note_id, stamp)
                if clean != old:
                    db.execute('UPDATE sync_receipts SET response=? WHERE operation_id=?',
                               (json.dumps(clean, ensure_ascii=False), operation_id))
        for path in files:
            path.unlink(missing_ok=True)
        return note


def purge_group_sources(parent_id):
    """Purge raw text from a completed group parent's owned source notes.

    A group child is allowed to have no summary of its own.  The parent's
    durable summary is the commit point.  Only IDs in ``sourceNoteIds`` whose
    current ``groupParentId`` still points at this parent are touched; source
    audio files and the parent note remain intact.
    """
    with storage.LOCK:
        parent = storage.get_note(parent_id)
        if not str(parent.get('summary') or '').strip() or parent.get('summaryStale'):
            raise ValueError('统一成稿尚未持久化，暂不清理来源原文。')
        source_ids = parent.get('sourceNoteIds')
        if source_ids is None:
            return parent
        if not isinstance(source_ids, list):
            raise ValueError('来源清单格式无效，暂不清理来源原文。')

        # Resolve every target and validate its cache paths before changing
        # either SQLite rows or files, so a malformed source cannot cause a
        # partial purge.
        children = []
        files = []
        for source_id in source_ids:
            try:
                child = storage.get_note(source_id)
            except KeyError:
                continue
            if child.get('groupParentId') != parent_id:
                continue
            from .prompt_lab import preserve
            preserve(child)
            children.append(child)
            files.extend(_files(_job_folder(source_id), TEXT_CACHE_SUFFIXES))

        stamp = parent.get('groupSourcesPurgedAt') or storage.now()
        with storage.connect() as db:
            for child in children:
                tombstone = child.get('transcriptPurgedAt') or stamp
                if (not child.get('transcriptPurgedAt') or child.get('transcript')
                        or child.get('segments') or child.get('chat') or child.get('summaryStale')):
                    child.update(transcript='', segments=[], chat=[], transcriptEdited=False,
                                 summaryStale=False, transcriptPurgedAt=tombstone,
                                 updatedAt=storage.now(), revision=int(child.get('revision', 0)) + 1)
                    db.execute('UPDATE notes SET body=? WHERE id=?',
                               (json.dumps(child, ensure_ascii=False), child['id']))
            for operation_id, body in db.execute('SELECT operation_id,response FROM sync_receipts').fetchall():
                old = json.loads(body)
                clean = old
                for child in children:
                    clean = _clear_receipt(clean, child['id'], child.get('transcriptPurgedAt') or stamp)
                if clean != old:
                    db.execute('UPDATE sync_receipts SET response=? WHERE operation_id=?',
                               (json.dumps(clean, ensure_ascii=False), operation_id))
        for path in set(files):
            path.unlink(missing_ok=True)
        return storage.get_note(parent_id)


def apply_retention(note_id):
    note = storage.get_note(note_id)
    if note.get('asrComplete'):
        completed_at = (note.get('asrTask') or {}).get('completedAt') or (note.get('fastTask') or {}).get('completedAt')
        note = archive_audio(note_id, at=completed_at)
    return purge_transcript(note_id)


def migrate_completed_notes():
    """Explicit migration entry point; never deletes original audio."""
    from . import jobs
    changed = []
    with jobs.LOCK:
        for note in storage.all_notes():
            if note['id'] in jobs.ACTIVE or note.get('status') in ('transcribing', 'summarizing'):
                continue
            after = apply_retention(note['id'])
            if after != note:
                changed.append(after['id'])
    return changed


def delete_audio(note_id):
    """Delete only this computer's original audio file and mark that local source gone.

    Derived job/recovery caches are intentionally handled by ``clean_audio_cache``;
    cloud objects are handled by ``object_storage.delete_cloud_audio``.
    """
    from . import jobs
    with jobs.LOCK, storage.LOCK:
        note = storage.get_note(note_id)
        if _processing(note_id, note):
            raise ValueError('录音正在处理，完成后再删除。')
        if note.get('audioDeletedAt'):
            return note
        files = []
        name = note.get('audioFile')
        if name:
            source = _child(storage.DATA / 'audio', name)
            shared = any(other['id'] != note_id and other.get('audioFile') == name and not other.get('audioDeletedAt')
                         for other in storage.all_notes())
            if not shared:
                files.append(source)
        # Every target was resolved and checked before the first deletion.
        for path in set(files):
            path.unlink(missing_ok=True)
        stamp = note.get('audioDeletedAt') or storage.now()
        # Keep an opaque basename so a later cache-only cleanup can remove the
        # adjacent playable derivative without making the original available.
        return storage.update_note(note_id, audioFile='', audioUrl='', audioOriginalFile=name or note.get('audioOriginalFile', ''),
                                   audioDeletedAt=stamp)


def clean_audio_cache(note_id):
    """Remove recoverable derived audio caches while retaining the original source.

    This is deliberately independent from ``delete_audio`` and never touches
    ``DATA/audio`` or any cloud object.  It is safe to repeat after an interrupted
    cleanup because every target is resolved and validated before unlinking.
    """
    from . import jobs
    with jobs.LOCK, storage.LOCK:
        note = storage.get_note(note_id)
        if _processing(note_id, note):
            raise ValueError('录音正在处理，完成后再清理缓存。')
        files = _files(_job_folder(note_id), AUDIO_SUFFIXES)
        files.extend(_files(_child(storage.DATA / 'recovery', note_id), AUDIO_SUFFIXES))
        name = note.get('audioFile') or note.get('audioOriginalFile')
        if name:
            source = _child(storage.DATA / 'audio', name)
            files.append(_child(storage.DATA / 'audio', source.stem + '.playable.mka'))
        with storage.connect() as db:
            for upload_id, body in db.execute('SELECT id,body FROM sync_uploads').fetchall():
                info = json.loads(body)
                if info.get('noteId') == note_id:
                    files.extend(_files(_child(storage.DATA / 'sync', upload_id), {'.part', '.tmp'}))
        for path in set(files):
            path.unlink(missing_ok=True)
        return note


def delete_cloud_audio(note_id, config=None):
    """Compatibility entry point for workflow callers; cloud scope stays isolated."""
    from . import object_storage
    return object_storage.delete_cloud_audio(note_id, config)
