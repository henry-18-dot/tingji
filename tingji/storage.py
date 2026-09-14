from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('TINGJI_DATA_DIR', str(ROOT / 'data'))).resolve()
DB = DATA / 'notes.sqlite3'
LOCK = threading.RLock()
DEFAULTS = {
    'asrAppId': '', 'asrResourceId': 'volc.seedasr.auc',
    'deepseekModel': 'deepseek-flash', 'hotwords': '',
    'tosRegion': 'cn-beijing', 'tosBucket': '', 'tosPrivateConfirmed': False,
    'asrMode': 'standard',
    'autoProcess': True,
    'dailyBudgetYuan': 10.0, 'failureOnlyNotifications': True,
}
SECRET_KEYS = ('asrApiKey', 'asrAccessToken', 'deepseekApiKey',
               'tosAccessKeyId', 'tosSecretAccessKey', 'tosSessionToken')
RECOVERABLE_ASR = {'submitting', 'submit_unknown', 'accepted', 'queued', 'processing', 'waiting_query'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'audio').mkdir(exist_ok=True)
    (DATA / 'jobs').mkdir(exist_ok=True)
    with connect() as db:
        exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        version = db.execute("SELECT value FROM settings WHERE key='_schemaVersion'").fetchone() if exists else None
        if exists and (not version or version[0] != '2'):
            folder = DATA / 'backups'
            folder.mkdir(exist_ok=True)
            destination = folder / ('before-standard-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.sqlite3')
            backup = sqlite3.connect(destination)
            try:
                db.backup(backup)
            finally:
                backup.close()
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS sync_receipts (operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, response TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS sync_uploads (id TEXT PRIMARY KEY, local_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS action_receipts (operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, result TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO settings VALUES ('_vaultId',?)", (str(uuid.uuid4()),))
        db.execute("INSERT OR REPLACE INTO settings VALUES ('asrResourceId','volc.seedasr.auc')")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('_schemaVersion','2')")
        # A durable ID means resume querying only. Never repeat a paid submission.
        for row in db.execute('SELECT id,body FROM notes').fetchall():
            note = json.loads(row[1])
            task = note.get('asrTask') or {}
            fast_task = note.get('fastTask') or {}
            if any(chunk.get('state') in ('submitting', 'submit_unknown') for chunk in fast_task.get('chunks', [])):
                for chunk in fast_task['chunks']:
                    if chunk.get('state') == 'submitting':
                        chunk['state'] = 'submit_unknown'
                fast_task.update(state='submit_unknown', retryMayCharge=True)
                note.update(fastTask=fast_task, status='error', asrComplete=False,
                    stage='极速请求结果待确认', error='上次极速请求已发送但结果未确认。保留了原录音和已返回内容；继续重试可能再次计费。')
                db.execute('UPDATE notes SET body=? WHERE id=?', (json.dumps(note, ensure_ascii=False), row[0]))
            if note['status'] in ('transcribing', 'summarizing'):
                if note['status'] == 'transcribing' and task.get('requestId') and task.get('state') in RECOVERABLE_ASR:
                    if task['state'] == 'submitting':
                        task['state'] = 'submit_unknown'
                    task['nextCheckAt'] = now()
                    note.update(asrTask=task, stage='恢复查询已提交任务', error='')
                else:
                    note.update(status='error', stage='处理已中断', error='本地处理已中断，原录音与已有文字仍在。已提交任务不会自动重新提交；请查看任务状态后继续。')
                db.execute('UPDATE notes SET body=? WHERE id=?', (json.dumps(note, ensure_ascii=False), row[0]))


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=30)
    try:
        with db:
            yield db
    finally:
        db.close()


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(value: str, decrypt=False):
    if os.name != 'nt':
        raise ValueError('当前版本使用 Windows 账户加密密钥，请在 Windows 上配置。')
    raw = base64.b64decode(value) if decrypt else value.encode('utf-8')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    func = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('Windows 密钥加密失败；请使用保存密钥时的 Windows 账户。')
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode('utf-8') if decrypt else base64.b64encode(result).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def settings(secrets=False):
    values = dict(DEFAULTS)
    with connect() as db:
        stored = dict(db.execute('SELECT key,value FROM settings'))
    values.update({k: v for k, v in stored.items() if k not in SECRET_KEYS and not k.startswith('_')})
    if values.get('deepseekModel') == 'deepseek-v4-flash':
        values['deepseekModel'] = 'deepseek-flash'
    values['asrResourceId'] = 'volc.seedasr.auc'
    values['tosPrivateConfirmed'] = stored.get('tosPrivateConfirmed') == 'true'
    values['autoProcess'] = stored.get('autoProcess', 'true') == 'true'
    values['failureOnlyNotifications'] = stored.get('failureOnlyNotifications', 'true') == 'true'
    values['dailyBudgetYuan'] = float(stored.get('dailyBudgetYuan', 10))
    for key in SECRET_KEYS:
        values[key] = protect(stored[key], decrypt=True) if secrets and stored.get(key) else ''
    values['asrConfigured'] = bool(stored.get('asrApiKey') or (values['asrAppId'] and stored.get('asrAccessToken')))
    values['asrAuthMode'] = 'apiKey' if stored.get('asrApiKey') else 'legacy'
    values['deepseekConfigured'] = bool(stored.get('deepseekApiKey'))
    values['tosConfigured'] = bool(values.get('tosBucket') and values.get('tosRegion') and stored.get('tosAccessKeyId') and stored.get('tosSecretAccessKey'))
    if not secrets:
        for key in SECRET_KEYS:
            values.pop(key, None)
    return values


def save_settings(values):
    clean = {}
    for key in DEFAULTS:
        if key in values:
            clean[key] = str(values[key]).strip()[:5000 if key == 'hotwords' else 150]
    clean['asrResourceId'] = 'volc.seedasr.auc'
    if 'dailyBudgetYuan' in values:
        value = values['dailyBudgetYuan']
        if isinstance(value, bool):
            raise ValueError('每日额度需为 0–10000 元。')
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError('每日额度需为 0–10000 元。') from None
        if not math.isfinite(value) or not 0 <= value <= 10000:
            raise ValueError('每日额度需为 0–10000 元。')
        clean['dailyBudgetYuan'] = str(round(value, 2))
    if 'failureOnlyNotifications' in values:
        if not isinstance(values['failureOnlyNotifications'], bool):
            raise ValueError('提醒设置无效。')
        clean['failureOnlyNotifications'] = 'true' if values['failureOnlyNotifications'] else 'false'
    if 'autoProcess' in values:
        if not isinstance(values['autoProcess'], bool):
            raise ValueError('自动处理设置无效。')
        clean['autoProcess'] = 'true' if values['autoProcess'] else 'false'
    if 'tosPrivateConfirmed' in values:
        clean['tosPrivateConfirmed'] = 'true' if values['tosPrivateConfirmed'] is True else 'false'
    if any(key in values for key in ('tosBucket', 'tosRegion')) and 'tosPrivateConfirmed' not in values:
        current = settings()
        if any(key in clean and clean[key] != current.get(key) for key in ('tosBucket', 'tosRegion')):
            clean['tosPrivateConfirmed'] = 'false'
    if 'deepseekModel' in clean and not clean['deepseekModel']:
        clean['deepseekModel'] = DEFAULTS['deepseekModel']
    if 'asrMode' in clean and clean['asrMode'] not in ('fast', 'standard'):
        raise ValueError('请选择标准版或极速版。')
    if 'asrResourceId' in clean and not clean['asrResourceId']:
        clean['asrResourceId'] = DEFAULTS['asrResourceId']
    for key in SECRET_KEYS:
        if values.get(key):
            text = str(values[key]).strip()
            if len(text) > 4096 or '\n' in text or '\r' in text:
                raise ValueError('密钥格式不正确，请粘贴完整单行密钥。')
            clean[key] = protect(text)
    with LOCK, connect() as db:
        for key, value in clean.items():
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
        for key in values.get('clearKeys', []):
            if key in SECRET_KEYS:
                db.execute('DELETE FROM settings WHERE key=?', (key,))
    return settings()


def create_note(title='', text='', **extra):
    note = dict(id=str(uuid.uuid4()), title=title.strip()[:160] or '未命名记录',
                createdAt=now(), updatedAt=now(), status='idle', stage='', error='',
                transcript=text, summary='', segments=[], language='auto', template='general',
                sourceName='', audioUrl='', chat=[], isDemo=False, summaryStale=False)
    note.update(extra)
    with LOCK, connect() as db:
        db.execute('INSERT INTO notes VALUES (?,?)', (note['id'], json.dumps(note, ensure_ascii=False)))
    return note


def get_note(note_id):
    with connect() as db:
        row = db.execute('SELECT body FROM notes WHERE id=?', (note_id,)).fetchone()
    if not row:
        raise KeyError('找不到这条记录。')
    note = json.loads(row[0])
    note.setdefault('revision', 0)
    return note


def update_note(note_id, **values):
    with LOCK, connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT body FROM notes WHERE id=?', (note_id,)).fetchone()
        if not row:
            raise KeyError('找不到这条记录。')
        note = json.loads(row[0])
        revision = int(note.get('revision', 0)) + 1
        if note.get('transcriptPurgedAt'):
            values.update(transcript='', segments=[], chat=[], transcriptPurgedAt=note['transcriptPurgedAt'])
        if note.get('audioDeletedAt'):
            values.update(audioFile='', audioUrl='', audioDeletedAt=note['audioDeletedAt'])
        note.update(values, updatedAt=now(), revision=revision)
        db.execute('UPDATE notes SET body=? WHERE id=?', (json.dumps(note, ensure_ascii=False), note_id))
    return note


def list_notes():
    with connect() as db:
        notes = [json.loads(row[0]) for row in db.execute('SELECT body FROM notes')]
    result = []
    for note in sorted(notes, key=lambda n: n['updatedAt'], reverse=True):
        item = {k: v for k, v in public_note(note).items() if k not in ('segments', 'chat', 'transcript', 'summary')}
        item['preview'] = (note['summary'] or note['transcript'])[:140]
        item['hasTranscript'] = bool(note['transcript'])
        item['hasSummary'] = bool(note['summary'])
        result.append(item)
    return result


def all_notes():
    with connect() as db:
        return [json.loads(row[0]) for row in db.execute('SELECT body FROM notes')]


def public_note(note):
    from . import lifecycle
    result = dict(note)
    result.update(lifecycle.public_fields(note))
    if note.get('transcriptPurgedAt'):
        result.update(transcript='', segments=[], chat=[])
    if note.get('audioDeletedAt'):
        result.update(audioFile='', audioUrl='')
    result.setdefault('revision', 0)
    if isinstance(note.get('asrTask'), dict):
        allowed = {'requestId', 'queryId', 'state', 'submittedAt', 'lastCheckedAt', 'nextCheckAt',
                   'errorCount', 'code', 'logId', 'autoSummarize', 'uploadConsentedAt', 'uploadSize', 'completedAt'}
        result['asrTask'] = {k: v for k, v in note['asrTask'].items() if k in allowed}
    if isinstance(note.get('fastTask'), dict):
        allowed = {'state', 'completedChunks', 'totalChunks', 'currentChunk', 'autoSummarize',
                   'startedAt', 'completedAt', 'retryMayCharge'}
        result['fastTask'] = {k: v for k, v in note['fastTask'].items() if k in allowed}
    if isinstance(note.get('groupTask'), dict):
        from .group_jobs import _task_public
        result['groupTask'] = _task_public(note['groupTask'])
    result.pop('groupReceipt', None)
    result.pop('sourceHash', None)
    result.pop('asrTaskHistory', None)
    return result


def account_id():
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key='_vaultId'").fetchone()
    if not row:
        raise ValueError('请重新打开听记以准备手机同步。')
    return row[0]
