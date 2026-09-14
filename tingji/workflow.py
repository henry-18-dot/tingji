"""Durable, bounded user-authorized batches and independent audio deletion scopes."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import date

from . import actions, jobs, lifecycle, storage, sync

LOCK = threading.RLock()
STOP = threading.Event()
SCHEDULER = None
LIMIT = 2


def initialize():
    with storage.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS workflow_batches (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL)')


def ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise ValueError('请选择 1–200 条记录。')
    checked = [sync.identifier(value) for value in values]
    if len(set(checked)) != len(checked):
        raise ValueError('同一记录不能重复选择。')
    return checked


def get_batch(batch_id):
    initialize()
    with storage.connect() as db:
        row = db.execute('SELECT body FROM workflow_batches WHERE id=?', (sync.identifier(batch_id),)).fetchone()
    if not row:
        raise KeyError(batch_id)
    return json.loads(row[0])


def save_batch(batch):
    batch['updatedAt'] = storage.now()
    with storage.connect() as db:
        db.execute('UPDATE workflow_batches SET body=? WHERE id=?', (json.dumps(batch, ensure_ascii=False), batch['id']))
    return batch


def list_batches():
    initialize()
    with storage.connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT body FROM workflow_batches ORDER BY rowid DESC LIMIT 30')]


def create_batch(note_ids, options, operation_id):
    initialize()
    note_ids = ids(note_ids)
    operation_id = sync.identifier(operation_id)
    if options.get('autoSummarize') is False:
        raise ValueError('听记在转录后统一生成笔记，不提供只转文字。')
    if options.get('audioUploadConsent') is not True:
        raise ValueError('请确认整理所选录音；语音与笔记服务按用量计费。')
    options = {k: options[k] for k in ('template', 'language', 'audioUploadConsent', 'cloudUploadConsent', 'asrMode', 'explicitRetry', 'retryFailed') if k in options}
    options['autoSummarize'] = True
    fingerprint = hashlib.sha256(json.dumps([note_ids, options], sort_keys=True).encode()).hexdigest()
    with LOCK, storage.LOCK, storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT fingerprint,body FROM workflow_batches WHERE id=?', (operation_id,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise ValueError('这次批处理编号已用于不同内容。')
            return json.loads(old[1])
        items = []
        for ident in note_ids:
            note = storage.get_note(ident)
            reason = ''
            if note.get('trashedAt'):
                reason = '在回收站中'
            elif note.get('groupParentId'):
                reason = '已归入其他笔记，请选择对应的整课笔记'
            elif note.get('summary') and not note.get('summaryStale'):
                reason = '笔记已经完成'
            elif note.get('transcriptPurgedAt'):
                reason = '原文此前已清理'
            elif not (note.get('sourceNoteIds') or note.get('audioFile') or note.get('transcript')):
                reason = '没有可处理的音频'
            items.append({'noteId': ident, 'title': note['title'], 'state': 'skipped' if reason else 'waiting',
                          'reason': reason, 'actionId': str(uuid.uuid4())})
        batch = {'id': operation_id, 'createdAt': storage.now(), 'updatedAt': storage.now(), 'state': 'queued',
                 'options': options, 'items': items}
        db.execute('INSERT INTO workflow_batches VALUES (?,?,?)', (operation_id, fingerprint, json.dumps(batch, ensure_ascii=False)))
    return batch


def pause_batch(batch_id, paused=True):
    with LOCK:
        batch = get_batch(batch_id)
        if batch['state'] == 'complete':
            return batch
        batch['state'] = 'paused' if paused else 'queued'
        return save_batch(batch)


def _start_note(note, options, action_id):
    if note.get('sourceNoteIds'):
        from . import group_jobs
        return group_jobs.start_group(note['id'], options)
    task = note.get('asrTask') or {}
    if task.get('requestId') and task.get('state') not in ('completed', 'failed', 'rejected'):
        return jobs.start(note['id'], 'query', options)
    if note.get('status') in ('transcribing', 'summarizing') or note['id'] in jobs.ACTIVE:
        return note
    action = 'summarize' if note.get('transcript') and note.get('asrComplete') is not False else 'transcribe'
    params = dict(options)
    if action == 'transcribe' and task.get('requestId') and task.get('state') in ('failed', 'rejected'):
        params.update(resubmit=True, replaceRequestId=task['requestId'])
    return actions.once(action_id, action, note['id'], params, lambda: jobs.start(note['id'], action, params))


def tick():
    with LOCK:
        batches = list_batches()
        # Preserve authorizations older than the recent-history UI window too.
        with storage.connect() as db:
            batches = [json.loads(row[0]) for row in db.execute('SELECT body FROM workflow_batches')]
        active_count = sum(item['state'] in ('running', 'dispatching') for batch in batches for item in batch['items'])
        for batch in batches:
            if batch['state'] == 'complete':
                continue
            for item in batch['items']:
                if item['state'] not in ('running', 'dispatching'):
                    continue
                try:
                    note = storage.get_note(item['noteId'])
                    if note.get('summary') and not note.get('summaryStale'):
                        item.update(state='completed', reason='')
                    elif note.get('status') == 'error':
                        item.update(state='failed', reason=note.get('error') or '处理未完成')
                    elif item['state'] == 'dispatching':
                        # A crashed dispatch is not permission to repeat a paid request.
                        if note.get('asrTask', {}).get('requestId') or note.get('groupTask') or note.get('status') in ('transcribing', 'summarizing'):
                            item['state'] = 'running'
                        else:
                            item.update(state='failed', reason='上次提交结果待核对，请先查看记录状态')
                except KeyError:
                    item.update(state='failed', reason='记录已不可用')
                if item['state'] not in ('running', 'dispatching'):
                    active_count = max(0, active_count - 1)
            if batch['state'] != 'paused':
                for item in batch['items']:
                    if item['state'] != 'waiting' or active_count >= LIMIT:
                        continue
                    item['state'] = 'dispatching'
                    save_batch(batch)
                    try:
                        note = storage.get_note(item['noteId'])
                        if note.get('trashedAt'):
                            raise ValueError('记录已放入回收站')
                        _start_note(note, batch['options'], item['actionId'])
                        item['state'] = 'running'
                        active_count += 1
                    except Exception as exc:
                        item.update(state='failed', reason=str(exc) if isinstance(exc, ValueError) else '未能提交，请查看记录状态')
            if all(item['state'] in ('completed', 'failed', 'skipped') for item in batch['items']):
                batch['state'] = 'complete'
            elif batch['state'] != 'paused':
                batch['state'] = 'running'
            save_batch(batch)


def start_scheduler():
    global SCHEDULER
    if SCHEDULER and SCHEDULER.is_alive():
        return
    STOP.clear()
    def loop():
        while not STOP.wait(1):
            try:
                tick()
            except Exception:
                continue
    SCHEDULER = threading.Thread(target=loop, name='tingji-batch-scheduler', daemon=True)
    SCHEDULER.start()


def stop_scheduler():
    STOP.set()


def delete_one(note_id, scope):
    if scope not in ('computer', 'cloud', 'cache'):
        raise ValueError('请选择此电脑原件、云端副本或此电脑转换缓存。')
    note = storage.get_note(note_id)
    if note.get('sourceNoteIds'):
        raise ValueError('请展开录音文件，选择具体文件及存放位置。')
    with jobs.LOCK:
        parent_id = note.get('groupParentId')
        parent = storage.get_note(parent_id) if parent_id else None
        if parent and not parent.get('summary'):
            raise ValueError('整课笔记尚未完成，请保留来源录音。')
        if scope != 'cache' and (not note.get('asrComplete') or not note.get('audioArchivedAt')):
            raise ValueError('请先完成笔记，再删除原始录音。')
        if scope == 'computer':
            return lifecycle.delete_audio(note_id)
        if scope == 'cloud':
            return lifecycle.delete_cloud_audio(note_id)
        return lifecycle.clean_audio_cache(note_id)


def delete_batch(note_ids, scope):
    result = []
    for ident in ids(note_ids):
        try:
            note = delete_one(ident, scope)
            result.append({'noteId': ident, 'state': 'completed', 'note': storage.public_note(note)})
        except (ValueError, KeyError) as exc:
            result.append({'noteId': ident, 'state': 'failed', 'reason': str(exc) if isinstance(exc, ValueError) else '录音记录不可用'})
        except Exception:
            result.append({'noteId': ident, 'state': 'failed', 'reason': '此位置删除未完成，其他位置保持独立'})
    return {'scope': scope, 'items': result}


def archive_batch(note_ids, archived=True):
    if not isinstance(archived, bool):
        raise ValueError('请选择归档或移回。')
    result = []
    for ident in ids(note_ids):
        try:
            note = storage.get_note(ident)
            if note.get('sourceNoteIds') or not lifecycle.audio_available(note):
                raise ValueError('请选择仍有原文件的具体录音。')
            note = storage.update_note(ident, audioArchivedAt=(note.get('audioArchivedAt') or storage.now()) if archived else None,
                                       audioDeleteDueAt=note.get('audioDeleteDueAt') if archived else None)
            result.append({'noteId': ident, 'state': 'completed', 'note': storage.public_note(note)})
        except (KeyError, ValueError) as exc:
            result.append({'noteId': ident, 'state': 'failed', 'reason': str(exc)})
    return {'items': result}


def assign_lesson(note_id, lesson):
    from . import timetable
    with storage.LOCK:
        note = storage.get_note(note_id)
        if note.get('trashedAt'):
            raise ValueError('请先恢复笔记。')
        if not lesson:
            return storage.update_note(note_id, lessonId='', courseId='', classDate='', courseName='')
        if not isinstance(lesson, dict):
            raise ValueError('课次格式无效。')
        class_date = date.fromisoformat(str(lesson.get('classDate', '')))
        if not lesson.get('courseId') and not lesson.get('lessonId'):
            return storage.update_note(note_id, classDate=class_date.isoformat(), lessonId='',
                                       courseId='', courseName='', courseColor='', lessonConfirmed=True)
        schedule = timetable.load()
        course = next((c for c in schedule['courses'] if c['id'] == lesson.get('courseId')), None)
        if not course:
            raise ValueError('课程已改变，请刷新课表。')
        lesson_id = str(lesson.get('lessonId', ''))
        if not lesson_id or not any(rule['id'] == lesson_id for rule in course['lessons']):
            raise ValueError('请选择具体课次。')
        return storage.update_note(note_id, lessonId=lesson_id, courseId=course['id'], classDate=class_date.isoformat(),
                                   courseName=course['name'], courseColor=course.get('color', ''), lessonConfirmed=True)
