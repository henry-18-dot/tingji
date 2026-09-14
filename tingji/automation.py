"""One durable automatic queue; shared audio files are indexed in place."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

from . import jobs, lifecycle, storage, budget

STOP = threading.Event()
THREAD = None
LOCK = threading.RLock()
SEEN = {}
ERROR = ''
EXTENSIONS = {'.mp3', '.mp4', '.wav', '.m4a', '.webm', '.ogg', '.opus', '.flac', '.mov', '.aac', '.wma', '.mkv'}
STABLE_SECONDS = 15


def enqueue(note_id):
    with LOCK, storage.LOCK:
        note = storage.get_note(note_id)
        if (note.get('autoProcessState') or note.get('summary') or note.get('groupParentId')
                or note.get('sourceNoteIds') or note.get('trashedAt') or note.get('asrTask')
                or note.get('fastTask') or not lifecycle.audio_available(note)):
            return note
        return storage.update_note(note_id, autoProcessState='waiting', autoQueuedAt=storage.now(),
                                   stage='已收到，等待自动转录')


def blocked_reason():
    config = storage.settings()
    if not config.get('autoProcess'):
        return '自动处理已暂停；文件会继续保存。'
    if not config.get('asrConfigured') or not config.get('deepseekConfigured'):
        return '请在设置中配置语音与笔记服务，文件已保存。'
    if not config.get('tosConfigured') or not config.get('tosPrivateConfirmed'):
        return '请在设置中完成私有音频存储配置，文件已保存。'
    if not jobs.FFMPEG or not jobs.FFPROBE:
        return '电脑缺少 FFmpeg 音频工具，暂不能读取录音。'
    return ''


def status():
    from .standard_jobs import elapsed
    notes = storage.all_notes()
    waiting = sum(n.get('autoProcessState') == 'waiting' and not n.get('trashedAt') for n in notes)
    running = sum(n.get('status') in ('transcribing', 'summarizing') for n in notes)
    blocked = blocked_reason()
    allowance = budget.status()
    budget_wait = any((n.get('budgetWaitUntil') and elapsed(n['budgetWaitUntil']) < 0)
                      or ((n.get('groupTask') or {}).get('state') in ('waiting_budget', 'summary_waiting_budget')
                          and (n.get('groupTask') or {}).get('budgetWaitUntil')
                          and elapsed(n['groupTask']['budgetWaitUntil']) < 0)
                      for n in notes if not n.get('trashedAt'))
    if not blocked and budget_wait:
        blocked = '今日额度不足，文件已排队，次日自动继续；也可在设置中调整上限。'
    return {'enabled': storage.settings().get('autoProcess', True), 'queuedCount': waiting,
            'runningCount': running, 'blockedReason': blocked,
            'message': ERROR or blocked or (f'{running} 项处理中，{waiting} 项排队' if running or waiting else '收到录音后自动转录、整理'),
            'inboxPath': str(storage.DATA / 'inbox'), **allowance}


def register_file(path):
    """Move a complete inbound file into managed storage without making a copy."""
    base = (storage.DATA / 'inbox').resolve()
    path = Path(path)
    if path.parent.resolve() != base or path.resolve() != path.absolute() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError('只接收录音目录中的音视频文件。')
    before = path.stat()
    if not 0 < before.st_size <= 1024**3:
        raise ValueError('录音为空或超过 1 GB，请拆分后保存。')
    with path.open('rb') as source:
        fingerprint = hashlib.file_digest(source, 'sha256').hexdigest()
    if (path.stat().st_size, path.stat().st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return None
    with storage.LOCK:
        notes = storage.all_notes()
        duplicate = next((n for n in notes if n.get('sourceHash') == fingerprint and n.get('fileSize') == before.st_size
                          and not n.get('trashedAt')), None)
        if duplicate:
            if not lifecycle.audio_available(duplicate) and not duplicate.get('audioDeletedAt') and not duplicate.get('summary'):
                target = lifecycle._child(storage.DATA / 'audio', duplicate['audioFile'])
                path.replace(target)
                return storage.update_note(duplicate['id'], autoProcessState='waiting', status='idle', error='',
                                           audioUrl='/api/audio/' + duplicate['id'], stage='已收到，等待自动转录')
            # Only the newly received identical file is removed; existing data is untouched.
            path.unlink()
            return duplicate
        target = (storage.DATA / 'audio' / (str(uuid.uuid4()) + path.suffix.lower())).resolve()
        if target.parent != (storage.DATA / 'audio').resolve() or target.exists():
            raise ValueError('录音保存路径无效。')
        note = storage.create_note(path.stem, sourceName=path.name, audioFile=target.name,
                                   sourceHash=fingerprint, fileSize=before.st_size, uploadedAt=storage.now(),
                                   template='lecture', language='auto', nameSource='auto',
                                   autoProcessState='waiting', autoQueuedAt=storage.now(), stage='已收到，等待自动转录')
        try:
            path.replace(target)
        except OSError:
            storage.update_note(note['id'], autoProcessState='blocked', status='error', error='收件文件移动失败，请检查文件是否仍在写入。')
            raise
        return storage.update_note(note['id'], audioUrl='/api/audio/' + note['id'])


def scan():
    global ERROR
    folder = storage.DATA / 'inbox'
    folder.mkdir(exist_ok=True)
    now = time.monotonic()
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS or path.is_symlink():
            continue
        info = path.stat()
        signature = (info.st_size, info.st_mtime_ns)
        previous = SEEN.get(str(path))
        if not previous or previous[:2] != signature:
            SEEN[str(path)] = (*signature, now)
            continue
        if now - previous[2] < STABLE_SECONDS:
            continue
        try:
            register_file(path)
            ERROR = ''
        except (OSError, ValueError) as exc:
            ERROR = f'{path.name}：{exc}'
            SEEN[str(path)] = (*signature, now + 45)


def tick():
    with LOCK:
        scan()
        notes = storage.all_notes()
        for note in notes:
            state = note.get('autoProcessState')
            if state not in ('running', 'dispatching'):
                continue
            task = note.get('asrTask') or {}
            if note.get('summary') and not note.get('summaryStale'):
                storage.update_note(note['id'], autoProcessState='completed')
            elif note.get('status') == 'error':
                storage.update_note(note['id'], autoProcessState='blocked')
            elif state == 'dispatching':
                if task.get('requestId') or note.get('status') in ('transcribing', 'summarizing'):
                    storage.update_note(note['id'], autoProcessState='running')
                else:
                    storage.update_note(note['id'], autoProcessState='blocked', status='error',
                                        stage='需要继续处理', error='上次自动处理被中断，请打开录音查看并继续。')
        if blocked_reason():
            return
        # Include manual/group work in the limit, not just automatic requests.
        notes = storage.all_notes()
        if jobs.ACTIVE or any(n.get('status') in ('transcribing', 'summarizing') for n in notes):
            return
        from .standard_jobs import elapsed
        pending = sorted((n for n in notes if n.get('autoProcessState') == 'waiting' and not n.get('trashedAt')
                          and not n.get('groupParentId') and not n.get('asrComplete')
                          and (not n.get('budgetWaitUntil') or elapsed(n['budgetWaitUntil']) >= 0)),
                         key=lambda n: n.get('autoQueuedAt', n['createdAt']))
        if not pending:
            return
        note = pending[0]
        storage.update_note(note['id'], autoProcessState='dispatching', stage='检查录音文件')
        try:
            # Validate before any TOS/ASR side effect, not merely by file extension.
            source = lifecycle._child(storage.DATA / 'audio', note['audioFile'])
            probe = json.loads(jobs.run_media([jobs.FFPROBE, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)], 30))
            if not any(s.get('codec_type') == 'audio' for s in probe.get('streams', [])):
                raise ValueError('文件没有可读取的音轨，请在语音备忘录重新导出。')
            seconds = jobs.duration(source)
            values = {'duration': seconds}
            created = (probe.get('format', {}).get('tags') or {}).get('creation_time')
            if created and not note.get('recordedAt'):
                from .sync import clean_note
                try:
                    values['recordedAt'] = clean_note({'recordedAt': created})['recordedAt']
                except ValueError:
                    pass
            storage.update_note(note['id'], **values)
            from . import standard_jobs
            standard_jobs.start(note['id'], 'transcribe', {'autoSummarize': True, 'cloudUploadConsent': True,
                                'audioUploadConsent': True, 'asrMode': 'standard', 'template': note.get('template') or 'lecture'})
            with jobs.LOCK:
                current = storage.get_note(note['id'])
                if current.get('autoProcessState') != 'waiting' or not current.get('budgetWaitUntil'):
                    storage.update_note(note['id'], autoProcessState='running', budgetWaitUntil=None)
        except budget.BudgetExceeded as exc:
            storage.update_note(note['id'], autoProcessState='waiting', status='idle', error='',
                                stage=str(exc), budgetWaitUntil=exc.resetsAt)
        except Exception as exc:
            storage.update_note(note['id'], autoProcessState='blocked', status='error', stage='录音需要处理',
                                error=str(exc) if isinstance(exc, ValueError) else '电脑未能读取录音，请检查文件是否完整后继续。')


def wake_budget_waiters():
    for note in storage.all_notes():
        changes = {}
        if note.get('budgetWaitUntil'):
            changes['budgetWaitUntil'] = None
        task = note.get('groupTask') or {}
        if task.get('state') in ('waiting_budget', 'summary_waiting_budget') and task.get('budgetWaitUntil'):
            changes['groupTask'] = {**task, 'budgetWaitUntil': None}
        if changes:
            storage.update_note(note['id'], **changes)


def start_scheduler():
    global THREAD
    if THREAD and THREAD.is_alive():
        return
    STOP.clear()
    def loop():
        global ERROR
        while not STOP.wait(3):
            try:
                tick()
            except Exception:
                ERROR = '接收队列暂时无法读取文件，正在重连；请检查电脑磁盘。'
    THREAD = threading.Thread(target=loop, name='tingji-automatic-inbox', daemon=True)
    THREAD.start()


def stop_scheduler():
    STOP.set()
