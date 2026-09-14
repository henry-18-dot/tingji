"""Explicit device synchronization with conditional revisions and durable receipts."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import uuid
import threading
import shutil
import weakref
from datetime import datetime

from . import jobs, storage

CHUNK = 8 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024
EXTENSIONS = {'.mp3', '.mp4', '.wav', '.m4a', '.webm', '.ogg', '.opus', '.flac', '.mov', '.avi', '.mkv', '.aac', '.wma'}
_UPLOAD_LOCKS = weakref.WeakValueDictionary()
_LOCK_REGISTRY = threading.Lock()


def upload_lock(upload_id):
    identifier(upload_id)
    with _LOCK_REGISTRY:
        lock = _UPLOAD_LOCKS.get(upload_id)
        if lock is None:
            lock = threading.RLock()
            _UPLOAD_LOCKS[upload_id] = lock
        return lock


def identifier(value):
    value = str(value or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', value):
        raise ValueError('同步编号格式无效。')
    return value


def editable(note):
    task = note.get('asrTask') or {}
    return (note['id'] not in jobs.ACTIVE and note.get('status') not in ('transcribing', 'summarizing')
            and not (task.get('requestId') and task.get('state') not in ('completed', 'failed', 'rejected')))


def clean_note(raw):
    if not isinstance(raw, dict):
        raise ValueError('笔记内容格式无效。')
    clean = {}
    for key, limit in [('title', 160), ('transcript', 800000), ('summary', 800000), ('sourceName', 200)]:
        value = raw.get(key, '')
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('笔记内容过长或格式无效。')
        clean[key] = value
    clean['language'] = raw.get('language') if raw.get('language') in ('zh', 'en', 'auto') else 'auto'
    clean['template'] = raw.get('template') if raw.get('template') in ('general', 'meeting', 'lecture', 'interview') else 'general'
    # Client-controlled paths, task states, credentials and identities never enter storage.
    clean.update(transcriptEdited=True, summaryStale=bool(raw.get('summaryStale')))
    # The client may preserve an incomplete source, never assert cloud completion.
    if raw.get('asrComplete') is False:
        clean['asrComplete'] = False
    if raw.get('nameSource') in ('auto', 'manual'):
        clean['nameSource'] = raw['nameSource']
    if raw.get('recordedAt'):
        try:
            instant = datetime.fromisoformat(str(raw['recordedAt']).replace('Z', '+00:00'))
            if instant.tzinfo is None:
                raise ValueError()
            clean['recordedAt'] = instant.isoformat()
        except (ValueError, TypeError):
            raise ValueError('录音时间格式无效。') from None
    if raw.get('duration') is not None:
        duration = raw['duration']
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 5 * 3600:
            raise ValueError('录音时长无效。')
        clean['duration'] = duration
    for key in ('archivedAt', 'trashedAt'):
        if key in raw:
            clean[key] = clean_metadata({key: raw[key]})[key]
    return clean


METADATA_KEYS = {'title', 'nameSource', 'archivedAt', 'trashedAt'}


def clean_metadata(raw):
    if not isinstance(raw, dict) or not raw or not set(raw) <= METADATA_KEYS:
        raise ValueError('笔记操作无效。')
    clean = dict(raw)
    if 'title' in clean:
        if not isinstance(clean['title'], str) or not clean['title'].strip() or len(clean['title']) > 160:
            raise ValueError('请输入名称，最多 160 字。')
        clean['title'] = clean['title'].strip()
        clean['nameSource'] = 'manual'
    if 'nameSource' in clean and clean['nameSource'] not in ('auto', 'manual'):
        raise ValueError('命名方式无效。')
    for key in ('archivedAt', 'trashedAt'):
        if key in clean and clean[key] is not None:
            try:
                date = datetime.fromisoformat(str(clean[key]).replace('Z', '+00:00'))
                if date.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('笔记操作时间无效。') from None
    return clean


def push(data):
    operation_id = identifier(data.get('operationId'))
    raw = data.get('note')
    clean = clean_note(raw)
    note_id = identifier(raw.get('id'))
    base = data.get('baseRevision', 0)
    if not isinstance(base, int) or base < 0:
        raise ValueError('同步版本格式无效。')
    signature_body = {'noteId': note_id, 'base': base, 'content': clean}
    if data.get('acceptUploadedAudio') is True:
        signature_body['acceptUploadedAudio'] = True
    metadata = clean_metadata(data['metadataPatch']) if 'metadataPatch' in data and data['metadataPatch'] is not None else None
    if metadata:
        if not isinstance(data.get('metadataBase', {}), dict):
            raise ValueError('笔记原版本格式无效。')
        signature_body.update(metadataPatch=metadata, metadataBase=data.get('metadataBase', {}))
    canonical = json.dumps(signature_body, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    with jobs.LOCK, storage.LOCK, storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        receipt = db.execute('SELECT payload_hash,response FROM sync_receipts WHERE operation_id=?', (operation_id,)).fetchone()
        if receipt:
            if receipt[0] != fingerprint:
                raise ValueError('这次同步编号已用于不同内容，请保存修改后重新同步。')
            result = json.loads(receipt[1])
            for key in ('note', 'remote'):
                cached = result.get(key) or {}
                latest = db.execute('SELECT body FROM notes WHERE id=?', (cached.get('id'),)).fetchone()
                latest = json.loads(latest[0]) if latest else {}
                if latest.get('transcriptPurgedAt') or latest.get('audioDeletedAt') or latest.get('cloudAudioDeletedAt') or 'trashedAt' in latest or 'archivedAt' in latest:
                    result[key] = storage.public_note(latest)
            return result
        row = db.execute('SELECT body FROM notes WHERE id=?', (note_id,)).fetchone()
        current = json.loads(row[0]) if row else None
        if metadata and not current:
            raise ValueError('请先上传这条录音。')
        if current and metadata:
            before = data.get('metadataBase') or {}
            for key in metadata:
                if key == 'nameSource':
                    continue
                if base != int(current.get('revision', 0)) and before.get(key) != current.get(key):
                    # A manual rename may replace the automatic name produced while processing.
                    if key == 'title' and current.get('nameSource') == 'auto':
                        continue
                    raise ValueError('这条笔记已在另一设备修改，请刷新后重试。')
            note = {**current, **metadata, 'revision': int(current.get('revision', 0)) + 1, 'updatedAt': storage.now()}
            if 'title' in metadata:
                note['titleUpdatedAt'] = storage.now()
            result = {'note': storage.public_note(note), 'conflict': False}
            db.execute('UPDATE notes SET body=? WHERE id=?', (json.dumps(note, ensure_ascii=False), note_id))
            db.execute('INSERT INTO sync_receipts VALUES (?,?,?)', (operation_id, fingerprint, json.dumps(result, ensure_ascii=False)))
            return result
        if current and current.get('trashedAt'):
            raise ValueError('笔记在回收站，请先恢复后再修改。')
        if current and data.get('acceptUploadedAudio') is True:
            # The cloud worker may already have advanced the revision between
            # audio completion and this acknowledgement. It is not an edit conflict.
            uploaded = db.execute('SELECT body FROM sync_uploads WHERE local_id=?', (str(raw.get('localId', '')),)).fetchone()
            if not uploaded or json.loads(uploaded[0]).get('noteId') != note_id:
                raise ValueError('未找到这份录音的完整上传记录，请继续上传。')
            result = {'note': storage.public_note(current), 'conflict': False}
            db.execute('INSERT INTO sync_receipts VALUES (?,?,?)', (operation_id, fingerprint, json.dumps(result, ensure_ascii=False)))
            return result
        if current and current.get('transcriptPurgedAt'):
            if int(current.get('revision', 0)) != base or not editable(current):
                raise ValueError('电脑笔记已有更新。本机修改已保留，请先打开电脑上的笔记。')
            clean.update(transcript='', transcriptEdited=False, summaryStale=False)
        conflict = bool(current and (int(current.get('revision', 0)) != base or not editable(current)))
        if current and not conflict:
            note = dict(current)
        else:
            note = dict(id=str(uuid.uuid4()), title='', createdAt=storage.now(), updatedAt=storage.now(), status='idle',
                        stage='', error='', transcript='', summary='', segments=[], language='auto', template='general',
                        sourceName='', audioUrl='', chat=[], isDemo=False, summaryStale=False, revision=0)
            if conflict:
                # Audio is immutable and may be referenced only from the already-authorized source note.
                for key in ('audioFile', 'audioUrl', 'fileSize', 'duration', 'chat', 'asrComplete'):
                    if key in current:
                        note[key] = current[key]
                if note.get('audioFile'):
                    note['audioUrl'] = '/api/audio/' + note['id']
                clean['title'] = (clean['title'] or '未命名记录')[:145] + ' · 同步副本'
        old_transcript = note.get('transcript', '')
        if current and not conflict:
            for key in ('duration', 'recordedAt'):
                if current.get(key):
                    clean[key] = current[key]
            # Existing lifecycle state is changed only by an explicit metadata operation.
            initial_upload = not current.get('summary') and not current.get('transcript') and current.get('status') == 'idle' and base <= 1
            for key in ('archivedAt', 'trashedAt'):
                if key in current or not initial_upload:
                    clean.pop(key, None)
        note.update(clean, revision=int(note.get('revision', 0)) + 1, updatedAt=storage.now())
        if note.get('transcriptPurgedAt'):
            note.update(transcript='', segments=[], chat=[], transcriptEdited=False, summaryStale=False)
        if old_transcript != clean['transcript']:
            note['segments'] = []
        if clean['transcript'].strip():
            if raw.get('asrComplete') is False or (current and current.get('asrComplete') is False):
                note['asrComplete'] = False
            elif old_transcript != clean['transcript']:
                note['asrComplete'] = True
            note.update(status='ready', stage='已从设备同步', error='')
            if note.get('asrComplete') is False:
                note['stage'] = '部分原文已同步，尚未完成转写'
        result = {'note': storage.public_note(note), 'conflict': conflict}
        if conflict:
            result['remote'] = storage.public_note(current)
        db.execute('INSERT OR REPLACE INTO notes VALUES (?,?)', (note['id'], json.dumps(note, ensure_ascii=False)))
        db.execute('INSERT INTO sync_receipts VALUES (?,?,?)', (operation_id, fingerprint, json.dumps(result, ensure_ascii=False)))
        return result


def load_upload(upload_id, db=None):
    identifier(upload_id)
    if db is None:
        with storage.connect() as connection:
            return load_upload(upload_id, connection)
    row = db.execute('SELECT body FROM sync_uploads WHERE id=?', (upload_id,)).fetchone()
    if not row:
        raise KeyError('找不到上传记录。')
    return json.loads(row[0])


def folder_for(upload_id):
    return storage.DATA / 'sync' / identifier(upload_id)


def upload_status(info):
    return {'uploadId': info['id'], 'chunkSize': CHUNK, 'parts': info['parts'], 'noteId': info.get('noteId'),
            'audioId': info.get('audioId'), 'audioDeletedAt': info.get('audioDeletedAt')}


def start_upload(data):
    local_id = identifier(data.get('localId'))
    name = str(data.get('name', '')).replace('\\', '/').rsplit('/', 1)[-1][:200]
    size = data.get('size')
    audio_id = identifier(data['audioId']) if data.get('audioId') else None
    if not isinstance(size, int) or not 0 < size <= MAX_FILE or Path(name).suffix.lower() not in EXTENSIONS:
        raise ValueError('请选择不超过 1 GB 的支持格式音视频。')
    checked = clean_note(data)
    recording_metadata = {key: checked[key] for key in ('recordedAt', 'duration', 'nameSource', 'title', 'template', 'language') if key in checked and key in data}
    with storage.LOCK, storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT body FROM sync_uploads WHERE local_id=?', (local_id,)).fetchone()
        if previous:
            info = json.loads(previous[0])
            if info['size'] != size or info['name'] != name:
                raise ValueError('本地录音已改变，请另存为新记录后上传。')
            if info.get('audioId') and audio_id != info['audioId']:
                raise ValueError('本地录音已改变，请另存为新记录后上传。')
            if audio_id and not info.get('audioId') and not info.get('noteId'):
                info['audioId'] = audio_id
                db.execute('UPDATE sync_uploads SET body=? WHERE id=?', (json.dumps(info), info['id']))
            if not info.get('noteId') and recording_metadata:
                info['recordingMetadata'] = {**info.get('recordingMetadata', {}), **recording_metadata}
                db.execute('UPDATE sync_uploads SET body=? WHERE id=?', (json.dumps(info), info['id']))
            return upload_status(info)
        info = {'id': str(uuid.uuid4()), 'localId': local_id, 'name': name, 'size': size, 'parts': [],
                'audioId': audio_id, 'createdAt': storage.now(), 'recordingMetadata': recording_metadata}
        db.execute('INSERT INTO sync_uploads VALUES (?,?,?)', (info['id'], local_id, json.dumps(info)))
    folder_for(info['id']).mkdir(parents=True, exist_ok=True)
    return upload_status(info)


def put_part(upload_id, index, source, length, expected_hash):
    if not re.fullmatch(r'[a-f0-9]{64}', str(expected_hash or '')):
        raise ValueError('缺少录音分块校验值。')
    with upload_lock(upload_id):
        info = load_upload(upload_id)
        if info.get('noteId'):
            raise ValueError('这份录音已完成上传。')
        count = math.ceil(info['size'] / CHUNK)
        if not isinstance(index, int) or not 0 <= index < count:
            raise ValueError('录音分块编号超出范围。')
        expected_size = min(CHUNK, info['size'] - index * CHUNK)
        if length != expected_size:
            raise ValueError('录音分块长度不匹配。')
        folder = folder_for(upload_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f'{index:06}.part'
        temporary = folder / f'{index:06}.{uuid.uuid4()}.tmp'
        sha = hashlib.sha256()
        try:
            with temporary.open('xb') as output:
                remain = length
                while remain:
                    block = source.read(min(remain, 1024 * 1024))
                    if not block:
                        raise ValueError('这块录音尚未传完，已保留其他完成的部分。')
                    sha.update(block)
                    output.write(block)
                    remain -= len(block)
            if sha.hexdigest() != expected_hash:
                raise ValueError('录音分块校验失败，请重试这块数据。')
            existing = next((part for part in info['parts'] if part['index'] == index), None)
            if existing and existing['sha256'] != expected_hash:
                raise ValueError('同一分块出现不同内容，已保留先前版本。')
            temporary.replace(target)
            if not existing:
                info['parts'].append({'index': index, 'sha256': expected_hash})
            info['parts'].sort(key=lambda part: part['index'])
            with storage.LOCK, storage.connect() as db:
                db.execute('UPDATE sync_uploads SET body=? WHERE id=?', (json.dumps(info), upload_id))
            return upload_status(info)
        finally:
            temporary.unlink(missing_ok=True)


def complete_upload(upload_id):
    with upload_lock(upload_id):
        info = load_upload(upload_id)
        if info.get('noteId'):
            return {'note': {**storage.public_note(storage.get_note(info['noteId'])), **({'duplicateUpload': True} if info.get('duplicateUpload') else {})}, 'localId': info['localId']}
        count = math.ceil(info['size'] / CHUNK)
        parts = {part['index']: part for part in info['parts']}
        if set(parts) != set(range(count)):
            raise ValueError('录音还有未传完的分块，请继续上传。')
        file_id = info['id']
        target = storage.DATA / 'audio' / (file_id + Path(info['name']).suffix.lower())
        if shutil.disk_usage(storage.DATA).free < info['size'] + 32 * 1024 * 1024:
            raise ValueError('电脑剩余空间不足以合并录音，已传部分仍在，请腾出空间后继续。')
        temporary = target.with_suffix('.syncing')
        try:
            whole_hash = hashlib.sha256()
            with temporary.open('wb') as output:
                for index in range(count):
                    sha = hashlib.sha256()
                    size = 0
                    with (folder_for(upload_id) / f'{index:06}.part').open('rb') as part:
                        while block := part.read(1024 * 1024):
                            sha.update(block)
                            size += len(block)
                            output.write(block)
                            whole_hash.update(block)
                    if sha.hexdigest() != parts[index]['sha256'] or size != min(CHUNK, info['size'] - index * CHUNK):
                        raise ValueError('录音文件校验未通过，未发布为完整录音。')
            if temporary.stat().st_size != info['size']:
                raise ValueError('录音长度不完整。')
            temporary.replace(target)
            # Note and completed receipt are one transaction, avoiding duplicate notes after lost acknowledgements.
            with storage.LOCK, storage.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                from . import lifecycle
                duplicate = next((json.loads(row[0]) for row in db.execute('SELECT body FROM notes')
                                  if json.loads(row[0]).get('sourceHash') == whole_hash.hexdigest()
                                  and json.loads(row[0]).get('fileSize') == info['size']
                                  and not json.loads(row[0]).get('trashedAt')), None)
                if duplicate:
                    info.update(noteId=duplicate['id'], duplicateUpload=True)
                    db.execute('UPDATE sync_uploads SET body=? WHERE id=?', (json.dumps(info), upload_id))
                    target.unlink(missing_ok=True)
                    return {'note': {**storage.public_note(duplicate), 'duplicateUpload': True}, 'localId': info['localId']}
                note = dict(id=str(uuid.uuid4()), sourceHash=whole_hash.hexdigest(), title=Path(info['name']).stem, createdAt=storage.now(), updatedAt=storage.now(),
                            status='idle', stage='已收到，等待自动转录', error='', transcript='', summary='', segments=[],
                            autoProcessState='waiting', autoQueuedAt=storage.now(),
                            language='auto', template='lecture', sourceName=info['name'], audioFile=target.name, fileSize=info['size'],
                            chat=[], isDemo=False, summaryStale=False, revision=1)
                note.update(info.get('recordingMetadata') or {})
                note['uploadedAt'] = storage.now()
                note['audioUrl'] = '/api/audio/' + note['id']
                db.execute('INSERT INTO notes VALUES (?,?)', (note['id'], json.dumps(note, ensure_ascii=False)))
                info['noteId'] = note['id']
                db.execute('UPDATE sync_uploads SET body=? WHERE id=?', (json.dumps(info), upload_id))
            return {'note': storage.public_note(note), 'localId': info['localId']}
        finally:
            temporary.unlink(missing_ok=True)
