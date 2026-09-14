"""Durable multi-note grouping and batch transcription orchestration.

Groups deliberately keep the existing note IDs as their source assets.  A
group parent is a normal note whose ``sourceNoteIds`` is an ordered manifest;
each source note receives ``groupParentId``.  No audio is copied or re-uploaded
by this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import budget, jobs, lifecycle, storage


GROUP_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tingji-group')
GROUP_ACTIVE = set()
GROUP_LOCK = threading.RLock()
SCHEDULER_STOP = threading.Event()
SCHEDULER = None
TERMINAL = {'completed', 'failed', 'rejected'}
PENDING = {'submitting', 'submit_unknown', 'accepted', 'queued', 'processing', 'waiting_query', 'paused', 'not_found'}


def _ensure_schema():
    with storage.LOCK, storage.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS group_receipts ('
                   'operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, '
                   'note_id TEXT NOT NULL, result TEXT NOT NULL)')


def _id(value):
    value = str(value or '')
    if not value or not all(ch.isalnum() or ch in '-_' for ch in value) or not 8 <= len(value) <= 100:
        raise ValueError('分组编号格式无效。')
    return value


def _operation(value):
    return _id(value or str(uuid.uuid4()))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def _load(db, note_id):
    row = db.execute('SELECT body FROM notes WHERE id=?', (note_id,)).fetchone()
    if not row:
        raise KeyError('找不到这条记录。')
    note = json.loads(row[0])
    note.setdefault('revision', 0)
    return note


def _save_db(db, note):
    db.execute('UPDATE notes SET body=? WHERE id=?',
               (json.dumps(note, ensure_ascii=False), note['id']))


def _source_state(note):
    if (note.get('asrComplete') is True and
            (str(note.get('transcript') or '').strip() or str(note.get('summary') or '').strip()
             or note.get('transcriptPurgedAt'))):
        return 'completed'
    task = note.get('asrTask') or {}
    if task.get('state') == 'budget_wait':
        return 'waiting_budget'
    if task.get('requestId') and task.get('state') in ('paused', 'not_found'):
        return 'unknown'
    if task.get('requestId') and task.get('state') in PENDING:
        return 'waiting_query'
    if task.get('state') == 'completed':
        return 'failed'  # Completed with silence/empty output is not usable text.
    fast = note.get('fastTask') or {}
    if fast.get('state') in ('submitting', 'submit_unknown'):
        return 'unknown'
    if note.get('status') in ('transcribing', 'summarizing') or note['id'] in jobs.ACTIVE:
        return 'running'
    if task.get('state') in ('failed', 'rejected', 'local_error') or fast.get('state') in ('failed', 'rejected', 'local_error'):
        return 'failed'
    if note.get('transcript') and note.get('asrComplete') is False:
        return 'partial'
    return 'pending'


def _source_entry(note_id, note, state=None):
    return {'noteId': note_id, 'state': state or _source_state(note),
            'revision': int(note.get('revision', 0)), 'startedAt': None,
            'completedAt': None, 'error': ''}


def _task_public(task):
    task = task if isinstance(task, dict) else {}
    sources = []
    for item in task.get('sources', []):
        sources.append({k: item.get(k) for k in ('noteId', 'state', 'revision', 'startedAt', 'completedAt', 'error')})
    return {'version': task.get('version', 1), 'state': task.get('state', 'idle'),
            'total': len(sources),
            'completed': sum(item.get('state') == 'completed' for item in sources),
            'failed': sum(item.get('state') == 'failed' for item in sources),
            'unknown': sum(item.get('state') == 'unknown' for item in sources),
            'sources': sources,
            'autoSummarize': bool(task.get('autoSummarize', True)),
            'startedAt': task.get('startedAt'), 'completedAt': task.get('completedAt'),
            'lessonId': task.get('lessonId', '')}


def _require_summary(options):
    if (options or {}).get('autoSummarize') is False:
        raise ValueError('分组任务必须生成统一成稿；不提供只转文字模式。')


def _options_snapshot(options):
    """Keep a small JSON-safe recovery snapshot, never provider secrets."""
    allowed = {'autoSummarize', 'template', 'language', 'lessonId', 'courseId',
               'classDate', 'courseName', 'retryFailed', 'retrySummary',
               'audioUploadConsent', 'cloudUploadConsent', 'asrMode'}
    snapshot = {}
    for key in allowed:
        value = (options or {}).get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            snapshot[key] = value
    snapshot['autoSummarize'] = True
    return snapshot


def group_public(note):
    """Return a public group view without provider IDs, hashes, or raw task data."""
    _ensure_schema()
    result = storage.public_note(note)
    task = note.get('groupTask') or {}
    source_ids = list(note.get('sourceNoteIds') or note.get('mergedNoteIds') or
                      task.get('sourceNoteIds') or task.get('mergedNoteIds') or [])
    sources = []
    entries = {item.get('noteId'): item for item in task.get('sources', [])}
    for index, source_id in enumerate(source_ids):
        try:
            source = storage.get_note(source_id)
        except KeyError:
            sources.append({'noteId': source_id, 'index': index, 'state': 'missing'})
            continue
        item = {'noteId': source_id, 'index': index, 'title': source.get('title', ''),
                'state': _source_state(source), 'status': source.get('status', ''),
                'stage': source.get('stage', ''), 'error': source.get('error', ''),
                'asrComplete': source.get('asrComplete') is True,
                'hasTranscript': bool(source.get('transcript')),
                'audioAvailable': lifecycle.audio_available(source)}
        entry = entries.get(source_id) or {}
        if item['state'] != 'completed' and entry.get('state') in ('failed', 'unknown'):
            item['state'] = entry['state']
            item['error'] = item['error'] or entry.get('error', '')
        item['nextAction'] = _next_action(source, item['state'])
        item['sourceName'] = source.get('sourceName') or source.get('title', '')
        if item['audioAvailable']:
            item['sourceName'] = source.get('sourceName', '')
            item['audioUrl'] = source.get('audioUrl') or '/api/audio/' + source_id
        sources.append(item)
    result['sourceNoteIds'] = source_ids
    result['sources'] = sources
    result['groupTask'] = _task_public(task)
    if note.get('groupKind'):
        result['groupKind'] = note['groupKind']
    if note.get('mergedNoteIds'):
        result['mergedNoteIds'] = list(note['mergedNoteIds'])
    if note.get('missingSourceIds'):
        result['missingSourceIds'] = list(note['missingSourceIds'])
    result.pop('groupReceipt', None)
    return result


def _new_parent(source_ids, title, lesson_id, operation_id, *, course_id='', class_date='', course_name=''):
    now = storage.now()
    return {'id': str(uuid.uuid4()), 'title': title.strip()[:160] or '合并课堂笔记',
            'createdAt': now, 'updatedAt': now, 'status': 'idle', 'stage': '', 'error': '',
            'transcript': '', 'summary': '', 'segments': [], 'language': 'auto',
            'template': 'lecture', 'sourceName': '合并课堂记录', 'audioUrl': '',
            'audioFile': '', 'chat': [], 'isDemo': False, 'summaryStale': False,
            'asrComplete': False, 'revision': 0, 'sourceNoteIds': list(source_ids),
            'lessonId': lesson_id or '', 'courseId': course_id or '',
            'classDate': class_date or '', 'courseName': course_name or '',
            'groupTask': {'version': 1, 'state': 'idle', 'sourceNoteIds': list(source_ids),
                          'sources': [], 'lessonId': lesson_id or '', 'operationId': operation_id,
                          'autoSummarize': True, 'startedAt': None, 'completedAt': None}}


def create_group(source_ids, title='', lesson_id='', operation_id=None, *,
                 course_id='', class_date='', course_name=''):
    """Create or replay a group parent while preserving source note IDs/order."""
    _ensure_schema()
    if not isinstance(source_ids, (list, tuple)) or not source_ids:
        raise ValueError('请选择至少一条来源录音。')
    source_ids = [_id(value) for value in source_ids]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError('来源录音不能重复。')
    if not isinstance(title, str) or len(title) > 160:
        raise ValueError('分组名称最多 160 字。')
    operation_id = _operation(operation_id)
    metadata = {'lessonId': str(lesson_id or '')[:160], 'courseId': str(course_id or '')[:160],
                'classDate': str(class_date or '')[:80], 'courseName': str(course_name or '')[:160]}
    fingerprint = _hash({'sourceNoteIds': source_ids, 'title': title, **metadata})
    with storage.LOCK, storage.connect() as db:
        receipt = db.execute('SELECT payload_hash,note_id,result FROM group_receipts WHERE operation_id=?',
                             (operation_id,)).fetchone()
        if receipt:
            if receipt[0] != fingerprint:
                raise ValueError('这个分组操作编号已经用于不同内容。')
            return storage.get_note(receipt[1])
        sources = [_load(db, source_id) for source_id in source_ids]
        for source in sources:
            parent = source.get('groupParentId')
            if parent:
                raise ValueError('来源录音已经属于另一组合，请先解除原组合。')
            if source.get('sourceNoteIds'):
                raise ValueError('不能把已有合并稿再次作为来源，避免原稿套叠。')
        parent = _new_parent(source_ids, title, metadata['lessonId'], operation_id,
                             course_id=metadata['courseId'], class_date=metadata['classDate'],
                             course_name=metadata['courseName'])
        parent['groupTask']['sources'] = [_source_entry(source['id'], source) for source in sources]
        db.execute('INSERT INTO notes VALUES (?,?)',
                   (parent['id'], json.dumps(parent, ensure_ascii=False)))
        for source in sources:
            source['groupParentId'] = parent['id']
            source['revision'] = int(source.get('revision', 0)) + 1
            source['updatedAt'] = storage.now()
            _save_db(db, source)
        result = storage.public_note(parent)
        db.execute('INSERT INTO group_receipts VALUES (?,?,?,?)',
                   (operation_id, fingerprint, parent['id'], json.dumps({'noteId': parent['id']})))
    return parent


def _lineage(note_id, seen=None):
    """Return IDs represented by a note, recursively, for merge-cycle checks."""
    seen = set() if seen is None else seen
    if note_id in seen:
        raise ValueError('检测到合并谱系循环。')
    seen.add(note_id)
    note = storage.get_note(note_id)
    children = list(note.get('sourceNoteIds') or note.get('mergedNoteIds') or [])
    result = {note_id}
    for child_id in children:
        result.update(_lineage(child_id, seen.copy()))
    return result


def _create_manual_parent(source_ids, title, operation_id):
    """Create a manual merge parent without changing source visibility links."""
    # Manual merge can be the first group operation in a fresh/temporary DB.
    # Keep its receipt table compatible with create_group.
    _ensure_schema()
    operation_id = _operation(operation_id)
    fingerprint = _hash({'mergedNoteIds': source_ids, 'title': title, 'groupKind': 'manual_merge'})
    with storage.LOCK, storage.connect() as db:
        receipt = db.execute('SELECT payload_hash,note_id FROM group_receipts WHERE operation_id=?',
                             (operation_id,)).fetchone()
        if receipt:
            if receipt[0] != fingerprint:
                raise ValueError('这个合并操作编号已经用于不同内容。')
            return storage.get_note(receipt[1])
        notes = [_load(db, source_id) for source_id in source_ids]
        lineages = []
        for source_id in source_ids:
            lineages.append(_lineage(source_id))
        for index, lineage in enumerate(lineages):
            if any(lineage & other for other in lineages[index + 1:]):
                raise ValueError('选中的笔记存在重复来源或嵌套谱系。')
        parent = _new_parent([], title, '', operation_id)
        parent.pop('sourceNoteIds', None)
        parent['mergedNoteIds'] = list(source_ids)
        parent['groupKind'] = 'manual_merge'
        parent['groupTask'].update(state='idle', mergedNoteIds=list(source_ids),
                                   sources=[_source_entry(source['id'], source, 'completed') for source in notes])
        db.execute('INSERT INTO notes VALUES (?,?)',
                   (parent['id'], json.dumps(parent, ensure_ascii=False)))
        db.execute('INSERT INTO group_receipts VALUES (?,?,?,?)',
                   (operation_id, fingerprint, parent['id'], json.dumps({'noteId': parent['id']})))
    return parent


def _merge_sources(source_ids):
    """Deterministically concatenate source text and shift available segments."""
    texts, segments, offset = [], [], 0.0
    for index, source_id in enumerate(source_ids):
        note = storage.get_note(source_id)
        # A completed note's summary is its durable user-facing content.  It
        # takes precedence over a legacy retained transcript so manually
        # merging an automatic group never nests its intermediate raw draft.
        text = str(note.get('summary') or note.get('transcript') or '')
        if text:
            texts.append(f'【第 {index + 1} 段｜{note.get("title") or source_id}】\n{text}')
        for item in note.get('segments') or []:
            if not isinstance(item, dict) or not str(item.get('text') or '').strip():
                continue
            try:
                start = float(item.get('start') or 0) + offset
                end = float(item.get('end') or item.get('start') or 0) + offset
            except (TypeError, ValueError):
                continue
            segment = dict(item, start=round(start, 3), end=round(max(start, end), 3))
            segments.append(segment)
        duration = note.get('duration')
        try:
            offset += max(0.0, float(duration or 0))
        except (TypeError, ValueError):
            pass
    return '\n\n'.join(texts), sorted(segments, key=lambda item: (item['start'], item['end'], item['text']))


def _persist_parent_transcript(parent_id, source_ids):
    text, segments = _merge_sources(source_ids)
    if not text.strip():
        raise ValueError('来源录音尚无可用原文。')
    return storage.update_note(parent_id, transcript=text, segments=segments,
                               asrComplete=True, status='ready', stage='来源转写完成', error='',
                               summaryStale=False)


def merge_notes(note_ids, title='', operation_id=None):
    """Create a deterministic manual merge without hiding source notes.

    Manual merge is a local composition operation: the selected source
    summaries (or retained transcripts for legacy notes) become the new
    note's summary.  The transient assembled transcript is never exposed or
    retained on the resulting note.
    """
    if not isinstance(note_ids, (list, tuple)) or not note_ids:
        raise ValueError('请选择至少一条待合并笔记。')
    source_ids = [_id(value) for value in note_ids]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError('待合并笔记不能重复。')
    if not isinstance(title, str) or len(title) > 160:
        raise ValueError('合并名称最多 160 字。')
    for source_id in source_ids:
        source = storage.get_note(source_id)
        if source.get('trashedAt'):
            raise ValueError('回收站中的笔记不能合并。')
        if source_id in jobs.ACTIVE or source.get('status') in ('transcribing', 'summarizing'):
            raise ValueError('处理中笔记不能合并，请等待成稿完成。')
        if not str(source.get('summary') or '').strip() or source.get('summaryStale'):
            raise ValueError('手动合并仅接受已有成稿的笔记。')
    # Validate that this local composition has content before creating its
    # durable receipt, avoiding an orphan manual parent for empty sources.
    preview, _ = _merge_sources(source_ids)
    if not preview.strip():
        raise ValueError('来源笔记尚无可用原文或成稿。')
    parent = _create_manual_parent(source_ids, title, operation_id)
    if parent.get('groupTask', {}).get('state') == 'merged':
        return parent
    notes = [storage.get_note(source_id) for source_id in source_ids]
    # Carry course linkage only when every selected source explicitly agrees.
    common = {}
    for key in ('lessonId', 'courseId', 'classDate', 'courseName'):
        values = [str(note.get(key) or '').strip() for note in notes]
        if values and values[0] and all(value == values[0] for value in values):
            common[key] = values[0]
    if common:
        parent = storage.update_note(parent['id'], **common)
    text, _segments = _merge_sources(source_ids)
    if not text.strip():
        raise ValueError('来源笔记尚无可用原文或成稿。')
    # The summary is the durable result of a manual merge.  Source notes are
    # deliberately left untouched and remain independently openable.
    current = storage.update_note(parent['id'], summary=text, transcript='', segments=[],
                                  asrComplete=True, status='ready', stage='合并成稿已保存',
                                  summaryStale=False, error='')
    task = dict(current.get('groupTask') or {})
    task.update(state='merged', completedAt=storage.now(), sources=[
        dict(item, state='completed', completedAt=item.get('completedAt') or storage.now())
        for item in task.get('sources', [])])
    return storage.update_note(parent['id'], groupTask=task, status='ready', stage='已按指定顺序合并', error='')


def _query_existing(source, options):
    task = source.get('asrTask') or {}
    if task.get('requestId') and task.get('state') in PENDING:
        if task.get('state') in ('paused', 'not_found'):
            return 'unknown', source.get('error') or '原任务查询已暂停，请继续查询原任务；不要重新上传。'
        # Standard scheduler owns its backoff and query budget. Re-starting
        # query here every second would reset both and create a hot retry loop.
        if task.get('nextCheckAt') or source['id'] in jobs.ACTIVE:
            return 'waiting_query', ''
        try:
            jobs.start(source['id'], 'query', {})
        except Exception as exc:
            return 'failed', str(exc)
        return 'waiting_query', ''
    # Terminal failures are eligible for an explicit per-source retry in
    # _process_sources. They must not be treated as pending queries.
    fast = source.get('fastTask') or {}
    if fast.get('state') in ('submitting', 'submit_unknown'):
        return 'unknown', '已有状态不明的极速请求，保留原请求，禁止自动重发。'
    return None, ''


def _next_action(source, state):
    task = source.get('asrTask') or {}
    if task.get('state') == 'not_found':
        return 'check-task'
    if task.get('requestId') and task.get('state') in PENDING:
        return 'query'
    if state == 'unknown':
        return 'check-task'
    if state == 'failed':
        return 'retry' if task or source.get('fastTask') else 'configure'
    return ''


def _process_sources(parent_id, options):
    parent = storage.get_note(parent_id)
    task = dict(parent.get('groupTask') or {})
    source_ids = list(parent.get('sourceNoteIds') or task.get('sourceNoteIds') or [])
    entries = {item['noteId']: item for item in task.get('sources', []) if item.get('noteId') in source_ids}
    for source_id in source_ids:
        source = storage.get_note(source_id)
        entry = entries.setdefault(source_id, _source_entry(source_id, source))
        state = _source_state(source)
        if state == 'completed':
            entry.update(state='completed', completedAt=entry.get('completedAt') or storage.now(), error='')
            continue
        if state == 'waiting_budget':
            from .standard_jobs import elapsed
            if source.get('budgetWaitUntil') and elapsed(source['budgetWaitUntil']) < 0:
                entry.update(state='waiting_budget', error='', budgetWaitUntil=source['budgetWaitUntil'])
                continue
        queried, error = _query_existing(source, options)
        if queried:
            entry.update(state=queried, error=error)
            continue
        if entry.get('state') == 'unknown':
            # The prior submission has no locally queryable task identity;
            # never turn an unknown charge into an automatic resubmission.
            entry.update(state='unknown', error=entry.get('error') or '请求状态不明，请核对后处理。')
            continue
        if source_id in jobs.ACTIVE or source.get('status') in ('transcribing', 'summarizing'):
            entry.update(state='running', error='')
            continue
        prior = entry.get('state')
        failed_retry = prior == 'failed' or state == 'failed'
        if failed_retry and options.get('retryFailed') is not True:
            entry.update(state='failed', error=entry.get('error') or source.get('error') or '来源转写失败，请明确重试失败项。')
            continue
        child_options = dict(options or {})
        child_options['autoSummarize'] = False
        if failed_retry:
            # This counter records one explicit retry authorization.  It is
            # never consumed by tick() implicitly, so a failed retry cannot
            # turn into a per-tick paid resubmission loop.
            entry['retryCount'] = int(entry.get('retryCount', 0)) + 1
            entry['lastRetryAt'] = storage.now()
            task['lastRetryRequestedAt'] = entry['lastRetryAt']
            standard_task = source.get('asrTask') or {}
            if standard_task.get('requestId') and standard_task.get('state') in TERMINAL:
                child_options.update(resubmit=True, replaceRequestId=standard_task['requestId'])
            if (source.get('fastTask') or {}).get('retryMayCharge'):
                # Unknown flash submissions are handled above and never enter here.
                child_options['explicitRetry'] = True
        try:
            entry.update(state='running', startedAt=entry.get('startedAt') or storage.now(), error='')
            task['sources'] = list(entries.values())
            storage.update_note(parent_id, groupTask=task, status='transcribing',
                                stage=f'正在处理来源 {source_ids.index(source_id) + 1}/{len(source_ids)}', error='')
            jobs.start(source_id, 'transcribe', child_options)
            after = storage.get_note(source_id)
            if (_source_state(after) == 'pending' and after.get('status') not in ('transcribing', 'summarizing')
                    and not (after.get('asrTask') or {}).get('requestId')
                    and (after.get('fastTask') or {}).get('state') not in ('submitting', 'submit_unknown')):
                entry.update(state='unknown', error='请求已提交但本地状态无法确认，禁止自动重发。')
        except budget.BudgetExceeded as exc:
            entry.update(state='waiting_budget', error='', budgetWaitUntil=exc.resetsAt)
        except Exception as exc:
            entry.update(state='failed', error=str(exc) if isinstance(exc, ValueError) else '来源处理未启动。')
    task['sources'] = [entries[source_id] for source_id in source_ids]
    parent = storage.update_note(parent_id, groupTask=task)
    return parent


def _finalize(parent_id, options):
    parent = storage.get_note(parent_id)
    task = dict(parent.get('groupTask') or {})
    source_ids = list(parent.get('sourceNoteIds') or task.get('sourceNoteIds') or [])
    sources = [storage.get_note(source_id) for source_id in source_ids]
    # A local start/configuration failure can leave the child note idle while
    # its durable group entry is already failed.  Prefer that entry state for
    # failed/unknown outcomes so the parent reaches a terminal error instead
    # of staying pending forever.
    entries = {item.get('noteId'): item for item in task.get('sources', [])}
    states = []
    for source in sources:
        entry_state = (entries.get(source['id']) or {}).get('state')
        actual = _source_state(source)
        states.append('completed' if actual == 'completed' else
                      entry_state if entry_state in ('failed', 'unknown', 'waiting_budget') else actual)
    def durable_entries():
        result = []
        for item in task.get('sources', []):
            source = storage.get_note(item['noteId'])
            actual = _source_state(source)
            state = 'completed' if actual == 'completed' else item.get('state') if item.get('state') in ('failed', 'unknown', 'waiting_budget') else actual
            result.append(dict(item, state=state, error='' if state == 'completed' else item.get('error') or source.get('error', '')))
        return result
    def source_errors():
        details = []
        for index, (source, state) in enumerate(zip(sources, states)):
            if state not in ('failed', 'unknown'):
                continue
            entry = entries.get(source['id']) or {}
            reason = source.get('error') or entry.get('error') or '转写未完成，请检查录音与任务设置。'
            name = source.get('sourceName') or source.get('title') or f'第 {index + 1} 段'
            details.append(f'{name}：{reason}')
        return '\n'.join(details)
    # Summary persistence and source cleanup are separate durable steps.  If
    # the process died after summary commit, finish cleanup without rerunning
    # the paid/remote summarizer.
    if task.get('state') in ('summarizing', 'summary_failed', 'summary_cleanup_failed', 'recovering') and str(parent.get('summary') or '').strip():
        try:
            lifecycle.purge_group_sources(parent_id)
        except Exception as exc:
            task['state'] = 'summary_cleanup_failed'
            return storage.update_note(parent_id, groupTask=task, status='error', stage='等待清理来源原文',
                                       error=str(exc) if isinstance(exc, ValueError) else '成稿已保存，来源原文清理未完成。')
        task.update(state='completed', completedAt=task.get('completedAt') or storage.now(), error='')
        return storage.update_note(parent_id, groupTask=task, status='ready', stage='统一成稿完成', error='')
    if any(state in ('waiting_query', 'running', 'pending', 'partial') for state in states):
        return storage.update_note(parent_id, status='transcribing', stage='等待全部来源完成')
    if any(state == 'waiting_budget' for state in states):
        waits = [source.get('budgetWaitUntil') or (entries.get(source['id']) or {}).get('budgetWaitUntil')
                 for source, state in zip(sources, states) if state == 'waiting_budget']
        task.update(state='waiting_budget', sources=durable_entries(),
                    budgetWaitUntil=min((value for value in waits if value), default=budget.status()['resetsAt']))
        return storage.update_note(parent_id, groupTask=task, status='idle',
                                   stage='今日额度不足，次日自动继续', error='')
    if any(state == 'unknown' for state in states):
        task.update(state='error', sources=durable_entries())
        return storage.update_note(parent_id, groupTask=task, status='error',
                                   stage='存在状态不明来源，已暂停批量任务',
                                   error=source_errors())
    if any(state == 'failed' for state in states):
        task.update(state='error', sources=durable_entries())
        return storage.update_note(parent_id, groupTask=task, status='error',
                                   stage=f'{sum(state == "completed" for state in states)}/{len(states)} 段完成，需处理以下来源', error=source_errors())
    parent = _persist_parent_transcript(parent_id, source_ids)
    task = dict(parent.get('groupTask') or {})
    task.update(state='summarizing', sources=[dict(item, state='completed', completedAt=item.get('completedAt') or storage.now())
                                              for item in task.get('sources', [])])
    parent = storage.update_note(parent_id, groupTask=task, status='summarizing', stage='开始生成统一成稿', error='')
    try:
        config = storage.settings(secrets=True)
        result = jobs.summarize(parent_id, config, options.get('template', parent.get('template', 'lecture')),
                                 options.get('language', parent.get('language', 'auto')))
        summarized = storage.get_note(parent_id)
        if not summarized.get('summary') and not (isinstance(result, dict) and result.get('summary')):
            raise ValueError('统一成稿未生成。')
    except budget.BudgetExceeded as exc:
        task.update(state='summary_waiting_budget', budgetWaitUntil=exc.resetsAt)
        return storage.update_note(parent_id, groupTask=task, status='idle', stage=str(exc), error='')
    except Exception as exc:
        task.update(state='summary_failed', error=str(exc))
        return storage.update_note(parent_id, groupTask=task, status='error', stage='统一成稿未完成',
                                   error=str(exc) if isinstance(exc, ValueError) else '统一成稿中断，来源原文已保留。')
    # The lifecycle helper validates the committed parent summary before
    # clearing source transcripts, caches, and old sync receipts.  Persist a
    # cleanup state so a crash/failure here is recoverable without resummary.
    try:
        lifecycle.purge_group_sources(parent_id)
    except Exception as exc:
        task.update(state='summary_cleanup_failed', error=str(exc))
        return storage.update_note(parent_id, groupTask=task, status='error', stage='等待清理来源原文',
                                   error=str(exc) if isinstance(exc, ValueError) else '成稿已保存，来源原文清理未完成。')
    task.update(state='completed', completedAt=storage.now(), error='')
    return storage.update_note(parent_id, groupTask=task, status='ready', stage='统一成稿完成', error='')


def process_group(note_id, options=None, _claimed=False):
    """One bounded, resumable processing pass; safe to call from a scheduler."""
    note_id = _id(note_id)
    _ensure_schema()
    options = dict(options or {})
    _require_summary(options)
    if not _claimed:
        with GROUP_LOCK:
            if note_id in GROUP_ACTIVE:
                return storage.get_note(note_id)
            GROUP_ACTIVE.add(note_id)
    try:
        note = storage.get_note(note_id)
        task = note.get('groupTask') or {}
        if task.get('state') == 'completed':
            return note
        if options:
            task = dict(task, processOptions=_options_snapshot(options))
            storage.update_note(note_id, groupTask=task)
        # A committed summary makes cleanup the only remaining step.  Resume
        # that local cleanup directly, without touching child ASR jobs or
        # requiring another paid request.
        if task.get('state') in ('summary_cleanup_failed', 'recovering') and str(note.get('summary') or '').strip():
            return _finalize(note_id, options)
        if task.get('state') == 'summary_waiting_budget' or (task.get('state') == 'summary_failed' and options.get('retrySummary') is True):
            return _finalize(note_id, options)
        _process_sources(note_id, options)
        return _finalize(note_id, options)
    finally:
        with GROUP_LOCK:
            GROUP_ACTIVE.discard(note_id)


def start_group(note_id, options=None):
    """Persist a group start then schedule one pass without occupying jobs.POOL."""
    note_id = _id(note_id)
    _ensure_schema()
    options = dict(options or {})
    _require_summary(options)
    with GROUP_LOCK, storage.LOCK:
        note = storage.get_note(note_id)
        if not note.get('sourceNoteIds'):
            raise ValueError('这条记录不是多来源分组。')
        task = dict(note.get('groupTask') or {})
        if task.get('state') == 'completed':
            return note
        if note_id in GROUP_ACTIVE:
            return note
        metadata = {}
        for key, limit in (('lessonId', 160), ('courseId', 160), ('classDate', 80), ('courseName', 160)):
            if key in options:
                metadata[key] = str(options[key] or '')[:limit]
        if metadata:
            note = storage.update_note(note_id, **metadata)
        task['autoSummarize'] = options.get('autoSummarize', True) is True
        task['processOptions'] = _options_snapshot(options)
        if options.get('lessonId'):
            task['lessonId'] = str(options['lessonId'])[:160]
        task['state'] = 'transcribing'
        task['startedAt'] = task.get('startedAt') or storage.now()
        storage.update_note(note_id, groupTask=task, status='transcribing', stage='批量任务已排队', error='')
        GROUP_ACTIVE.add(note_id)
    def worker():
        try:
            process_group(note_id, options, _claimed=True)
        except Exception as exc:
            storage.update_note(note_id, status='error', stage='批量任务中断',
                                error=str(exc) if isinstance(exc, ValueError) else '批量任务中断，来源原文已保留。')
        finally:
            with GROUP_LOCK:
                GROUP_ACTIVE.discard(note_id)
    GROUP_POOL.submit(worker)
    return storage.get_note(note_id)


def tick(note_id=None, options=None):
    _ensure_schema()
    targets = [storage.get_note(note_id)] if note_id else storage.all_notes()
    results = []
    for note in targets:
        task = note.get('groupTask') or {}
        if not task or task.get('state') in ('completed', 'merged', 'partial'):
            continue
        # Creation is durable but inert.  Only start_group/process_group (or a
        # caller supplying explicit options) activates provider work.
        if task.get('state') == 'idle' and options is None:
            continue
        if task.get('state') in ('waiting_budget', 'summary_waiting_budget') and task.get('budgetWaitUntil'):
            from .standard_jobs import elapsed
            if elapsed(task['budgetWaitUntil']) < 0:
                continue
        effective_options = dict(options) if options is not None else dict(task.get('processOptions') or {})
        # A retry flag is an authorization for one explicit call, never a
        # standing scheduler permission that could resubmit every tick.
        if options is None:
            effective_options.pop('retryFailed', None)
            effective_options.pop('retrySummary', None)
        if task.get('state') == 'error' and not effective_options.get('retryFailed'):
            # An individual source can complete through its own ASR queue.
            # Refresh a stale parent error only when every source is usable;
            # never turn an error into another paid transcription attempt.
            source_ids = note.get('sourceNoteIds') or []
            if not source_ids or not all(_source_state(storage.get_note(source_id)) == 'completed' for source_id in source_ids):
                continue
        if task.get('state') == 'summary_failed' and not effective_options.get('retrySummary'):
            continue
        if note['id'] in GROUP_ACTIVE:
            continue
        results.append(process_group(note['id'], effective_options))
    return results


def recover():
    """Mark interrupted groups resumable; never starts a provider call itself."""
    _ensure_schema()
    recovered = []
    with GROUP_LOCK:
        for note in storage.all_notes():
            task = note.get('groupTask') or {}
            if not task or task.get('state') in ('completed', 'merged'):
                continue
            state = task.get('state')
            has_summary = bool(str(note.get('summary') or '').strip()) and not note.get('summaryStale')
            is_summary_phase = state in ('summarizing', 'summary_failed', 'summary_cleanup_failed') \
                or note.get('status') == 'summarizing'
            if is_summary_phase:
                # With a committed summary only local source cleanup remains;
                # without one, automatic recovery must never re-run a paid
                # summarization request.  The user must explicitly retry it.
                next_state = 'summary_cleanup_failed' if has_summary else 'summary_failed'
                stage = '成稿已保存，等待清理来源原文' if has_summary else '整理中断，等待明确重试'
                error = ('程序曾中断；恢复时只继续本机来源清理。' if has_summary else
                         '程序在整理阶段中断；未自动重发，请明确重试整理。')
                task = dict(task, state=next_state, error=error)
                recovered.append(storage.update_note(note['id'], groupTask=task, status='error',
                    stage=stage, error=error))
            elif state == 'transcribing' or note.get('status') == 'transcribing':
                task = dict(task, state='recovering')
                recovered.append(storage.update_note(note['id'], groupTask=task, status='error',
                    stage='批量任务可恢复', error='程序曾中断；已保留每个来源状态，恢复时不会重发状态不明请求。'))
    return recovered


def start_scheduler(interval=1):
    global SCHEDULER
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError('分组维护间隔必须大于零。')
    if SCHEDULER and SCHEDULER.is_alive():
        return SCHEDULER
    SCHEDULER_STOP.clear()
    def loop():
        while not SCHEDULER_STOP.wait(interval):
            try:
                tick()
            except Exception:
                continue
    SCHEDULER = threading.Thread(target=loop, name='tingji-group-scheduler', daemon=True)
    SCHEDULER.start()
    return SCHEDULER


def stop_scheduler():
    global SCHEDULER
    SCHEDULER_STOP.set()
    thread, SCHEDULER = SCHEDULER, None
    if thread and thread is not threading.current_thread():
        thread.join(timeout=2)
