"""Durable asynchronous speech tasks. Querying never creates or uploads a task."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import budget, jobs, lifecycle, providers, storage

PENDING = {'submitting', 'submit_unknown', 'accepted', 'queued', 'processing', 'waiting_query'}
QUERYABLE = PENDING | {'paused', 'not_found'}
TERMINAL = {'completed', 'failed', 'rejected'}
POLL_SECONDS = max(1, int(os.environ.get('TINGJI_POLL_SECONDS', '15')))
SCHEDULER_STOP = threading.Event()
SCHEDULER = None


def future(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec='seconds')


def elapsed(value):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    except (ValueError, TypeError):
        return 0


def task_update(note_id, task_values=None, **note_values):
    with jobs.LOCK:
        note = storage.get_note(note_id)
        task = {**(note.get('asrTask') or {}), **(task_values or {})}
        return storage.update_note(note_id, asrTask=task, **note_values)


def validate_upload(config, options):
    from . import object_storage
    if options.get('cloudUploadConsent') is not True:
        raise ValueError('尚未允许自动上传到私有 TOS。请在设置中开启自动转录后继续；本条录音还未提交语音服务。')
    if config.get('tosPrivateConfirmed') is not True:
        raise ValueError('请先在设置中确认 TOS 桶为私有且没有公共访问策略；不会公开录音。')
    object_storage.validate_config(config)


def prepare_audio(note_id):
    note = storage.get_note(note_id)
    source = storage.DATA / 'audio' / note['audioFile']
    task = note['asrTask']
    seconds = jobs.duration(source)
    folder = storage.DATA / 'jobs' / note_id / ('standard-' + task['attemptId'])
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / 'audio.mp3'
    # One whole recording per task preserves speaker continuity and avoids the
    # old flash limit. 16 kHz mono / 64 kbit MP3 is <= ~144 MB over five hours.
    jobs.run_media([jobs.FFMPEG, '-nostdin', '-y', '-v', 'error', '-i', str(source),
                    '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k', str(audio)], timeout=900)
    if not audio.is_file() or not 0 < audio.stat().st_size <= 512 * 1024 * 1024:
        raise ValueError('转换后的音频为空或超过标准版 512 MB 限制，请拆分文件后重试。原文件已保留。')
    storage.update_note(note_id, duration=round(seconds, 3))
    return audio


def submit(note_id, config, auto_summary=True):
    from . import object_storage
    config = course_asr_config(storage.get_note(note_id), config)
    task_update(note_id, {'state': 'preparing'}, stage='在本机准备整段音频')
    audio = prepare_audio(note_id)
    prepared_note = storage.get_note(note_id)
    task = prepared_note['asrTask']
    budget_key = 'asr:' + task['attemptId']
    # Reserve before TOS upload and include one second for MP3 frame padding.
    seconds = float(prepared_note.get('duration') or 0) + 1
    amount = budget.estimate_asr(seconds)
    budget.reserve(budget_key, amount, kind='asr')
    task_update(note_id, {'budgetKey': budget_key, 'budgetAmountYuan': amount})
    config = {**config, '_budgetKey': budget_key, '_budgetDurationSeconds': seconds}
    task_update(note_id, {'state': 'uploading'}, stage='上传至你的私有 TOS，尚未提交语音任务')
    uploaded = object_storage.prepare_and_upload(audio, config, note_id + '-' + task['attemptId'])
    request_id = str(uuid.uuid4())
    # Persist BEFORE the network side effect. A crash after this commit becomes
    # submit_unknown and is reconciled only by querying the very same ID.
    task_update(note_id, {'state': 'submitting', 'requestId': request_id, 'queryId': request_id,
                         'submittedAt': storage.now(), 'monitorUntil': future(24 * 3600),
                         'autoSummarize': bool(auto_summary), 'uploadSize': audio.stat().st_size,
                         'cloudObject': {k: v for k, v in uploaded.items() if k != 'downloadUrl'},
                         'nextCheckAt': future(POLL_SECONDS)}, stage='正在提交标准版任务')
    try:
        note = storage.get_note(note_id)
        ack = providers.submit_transcription(uploaded['downloadUrl'], config, request_id,
                                             note['language'], audio_format='mp3')
    except providers.AsrSubmitUncertain as exc:
        return task_update(note_id, {'state': 'submit_unknown', 'code': exc.code, 'logId': exc.log_id,
                          'nextCheckAt': future(POLL_SECONDS)}, status='transcribing',
                          stage='提交结果暂未确认，将查询原任务；不会重新提交', error='')
    except providers.AsrSubmitRejected as exc:
        return task_update(note_id, {'state': 'rejected', 'code': exc.code, 'logId': exc.log_id,
                           'nextCheckAt': None}, status='error', stage='提交被服务拒绝', error=str(exc))
    return task_update(note_id, {'state': 'accepted', 'queryId': ack.get('task_id') or request_id,
                       'code': ack.get('code', ''), 'logId': ack.get('log_id', ''),
                       'nextCheckAt': future(POLL_SECONDS)}, status='transcribing',
                       stage='标准版已接收任务，等待查询结果', error='')


def course_asr_config(note, config):
    """Derive recognition vocabulary from the saved timetable each submission.
    Course names take priority over keywords; the provider caps the list at 100.
    """
    from . import timetable
    courses = list(timetable.load().get('courses') or [])
    courses.sort(key=lambda course: not (
        (note.get('courseId') and course.get('id') == note['courseId']) or
        (note.get('courseName') and course.get('name') == note['courseName'])))
    words = [course.get('name', '') for course in courses]
    for course in courses:
        words.extend(course.get('keywords') or [])
    return {**config, 'hotwords': '\n'.join(providers.hotword_list('\n'.join(words)))}


def handle_unexpected(note_id, exc):
    note = storage.get_note(note_id)
    task = note.get('asrTask') or {}
    if isinstance(exc, budget.BudgetExceeded):
        if task.get('budgetKey'):
            budget.release(task['budgetKey'])
        return task_update(note_id, {'state': 'budget_wait', 'requestId': None, 'queryId': None,
                                    'nextCheckAt': None},
                           status='idle', stage=str(exc), error='', autoProcessState='waiting',
                           budgetWaitUntil=exc.resetsAt)
    if task.get('budgetKey') and not task.get('requestId'):
        budget.release(task['budgetKey'])
    message = str(exc) if isinstance(exc, ValueError) else '本地处理中断。原录音与已有内容仍在，请查看任务状态后继续。'
    if task.get('requestId') and task.get('state') not in TERMINAL:
        # Even an unexpected local failure after submission is not permission to
        # repeat the charge. Keep the durable identity and pause querying.
        task_update(note_id, {'state': 'paused', 'nextCheckAt': None}, status='error',
                    stage='查询暂停，原任务编号已保留', error=message)
    else:
        task_update(note_id, {'state': 'local_error', 'nextCheckAt': None}, status='error',
                    stage='本地准备或上传未完成', error=message)


def finish_result(note_id, response, config):
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt') or (note.get('asrTask') or {}).get('state') == 'completed':
        return note
    task = note['asrTask']
    result = response.get('result') or {}
    folder = storage.DATA / 'jobs' / note_id / ('standard-' + task['attemptId'])
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / 'result.tmp'
    temporary.write_text(json.dumps({'requestId': task['requestId'], 'queryId': task.get('queryId'),
                         'complete': True, 'response': response}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(folder / 'result.json')
    segments = jobs.normalize_segments(result, 0, 0, False, note.get('duration', 0))
    done = {'state': 'completed', 'completedAt': storage.now(), 'nextCheckAt': None,
            'errorCount': 0, 'code': response.get('code', ''), 'logId': response.get('log_id', '')}
    if not segments:
        return task_update(note_id, done, status='error', stage='任务完成，未检测到可识别语音',
                           error='标准版返回静音或空转写。原录音及已有文稿保留，请先回听确认。')
    text = jobs.format_transcript(segments)
    auto = task.get('autoSummarize') and config.get('deepseekApiKey')
    done['summaryState'] = 'waiting' if task.get('autoSummarize') else 'disabled'
    task_update(note_id, done, transcript=text, segments=segments, transcriptEdited=False,
                asrComplete=True, summaryStale=bool(note['summary']), error='',
                status='summarizing' if auto else 'ready', stage='开始提炼' if auto else '转写完成')
    lifecycle.archive_audio(note_id, at=done['completedAt'])
    if auto:
        _summarize_completed(note_id, config)
    return storage.get_note(note_id)


def _summarize_completed(note_id, config):
    """Persist summary dispatch separately so a restart can resume the gap
    after ASR commit, without automatically repeating a dispatched request.
    """
    note = storage.get_note(note_id)
    task_update(note_id, {'summaryState': 'running', 'summaryStartedAt': storage.now()},
                status='summarizing', stage='正在整理文字', error='')
    try:
        jobs.summarize(note_id, config, note['template'], note['language'])
        task_update(note_id, {'summaryState': 'completed'}, budgetSummaryPending=False, budgetWaitUntil=None)
    except budget.BudgetExceeded as exc:
        task_update(note_id, {'summaryState': 'waiting'}, status='ready', stage=str(exc),
                    error='', autoProcessState='waiting', budgetWaitUntil=exc.resetsAt, budgetSummaryPending=True)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else '整理中断，请重试整理；无需重新上传或转录。'
        task_update(note_id, {'summaryState': 'failed'}, status='error',
                    stage='转写完成，整理未完成', error=message)


def _run_summary(note_id, config):
    try:
        _summarize_completed(note_id, config)
    finally:
        with jobs.LOCK:
            jobs.ACTIVE.discard(note_id)


def query_once(note_id):
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt'):
        return note
    task = note.get('asrTask') or {}
    if not task.get('requestId') or task.get('state') not in QUERYABLE:
        return note
    config = storage.settings(secrets=True)
    cached = storage.DATA / 'jobs' / note_id / ('standard-' + str(task.get('attemptId', ''))) / 'result.json'
    if cached.is_file():
        try:
            saved = json.loads(cached.read_text(encoding='utf-8'))
            if (isinstance(saved, dict) and saved.get('complete') is True and saved.get('requestId') == task['requestId']
                    and (saved.get('queryId') or saved.get('requestId')) == (task.get('queryId') or task['requestId'])
                    and isinstance(saved.get('response'), dict) and saved['response'].get('state') == 'completed'
                    and isinstance(saved['response'].get('result'), dict)):
                return finish_result(note_id, saved['response'], config)
        except (ValueError, OSError):
            pass  # Invalid/incomplete cache is never accepted as a final transcript.
    task_update(note_id, {'lastCheckedAt': storage.now()}, stage='查询标准版任务')
    try:
        result = providers.query_transcription(config, task.get('queryId') or task['requestId'])
    except providers.AsrQueryError as exc:
        errors = task.get('errorCount', 0) + 1
        pause = errors >= 3 or not exc.retryable
        return task_update(note_id, {'state': 'paused' if pause else 'waiting_query', 'errorCount': errors,
                     'code': exc.code, 'logId': exc.log_id, 'nextCheckAt': None if pause else future(POLL_SECONDS * (2 ** errors))},
                     status='error' if pause else 'transcribing', stage='查询已暂停，可继续查询原任务' if pause else '查询暂时失败，稍后查询同一任务',
                     error=str(exc) if pause else '')
    state = result['state']
    if state == 'completed':
        return finish_result(note_id, result, config)
    if state == 'failed':
        return task_update(note_id, {'state': 'failed', 'code': result.get('code', ''), 'logId': result.get('log_id', ''),
                         'nextCheckAt': None}, status='error', stage='语音任务失败',
                         error=result.get('message') or '语音服务确认任务失败，请检查音频与控制台。已有内容保留。')
    if state == 'not_found':
        return task_update(note_id, {'state': 'not_found', 'nextCheckAt': None, 'code': result.get('code', '')},
                         status='error', stage='暂未查到原任务',
                         error='未查到这个任务不代表可以安全重提。请用保留的任务编号核对控制台／工单，然后继续查询。')
    if state not in ('queued', 'processing'):
        raise ValueError('收到无法确认的任务状态，保留原任务并暂停查询。')
    if elapsed(task.get('monitorUntil')) >= 0 and task.get('monitorUntil'):
        return task_update(note_id, {'state': 'paused', 'nextCheckAt': None}, status='error', stage='自动查询已暂停',
                         error='已连续查询约 24 小时仍未完成。可继续查询原任务；不会重新上传或重新提交。')
    return task_update(note_id, {'state': state, 'errorCount': 0, 'code': result.get('code', ''),
                     'logId': result.get('log_id', ''), 'nextCheckAt': future(POLL_SECONDS)}, status='transcribing', error='',
                     stage='标准版排队中；通常 3 小时内完成，高峰可能更久' if state == 'queued' else '标准版正在转写；结果尚未完成')


def _run_query(note_id):
    try:
        query_once(note_id)
    except Exception as exc:
        handle_unexpected(note_id, exc)
    finally:
        with jobs.LOCK:
            jobs.ACTIVE.discard(note_id)


def request_query(note_id):
    with jobs.LOCK:
        note = storage.get_note(note_id)
        if note.get('transcriptPurgedAt'):
            return note
        task = note.get('asrTask') or {}
        if not task.get('requestId'):
            raise ValueError('尚无已提交的标准版任务。请完成音频存储配置后提交。')
        if task.get('state') in TERMINAL:
            return note
        if note_id in jobs.ACTIVE:
            return note
        providers.asr_headers(storage.settings(secrets=True))
        updated = task_update(note_id, {'state': 'waiting_query', 'errorCount': 0, 'nextCheckAt': storage.now(),
                               'monitorUntil': future(24 * 3600)}, status='transcribing', stage='继续查询原任务', error='')
        jobs.ACTIVE.add(note_id)
        jobs.POOL.submit(_run_query, note_id)
        return updated


def start(note_id, action, options=None):
    options = options or {}
    if action == 'query':
        return request_query(note_id)
    with jobs.LOCK:
        note = storage.get_note(note_id)
        if note.get('transcriptPurgedAt'):
            return note
        if note_id in jobs.ACTIVE or note.get('status') in ('transcribing', 'summarizing'):
            raise ValueError('这条记录正在处理中；已提交任务请继续查询，不要重复提交。')
        task = note.get('asrTask') or {}
        config = storage.settings(secrets=True)
        if action == 'transcribe':
            if task.get('requestId'):
                if task.get('state') not in TERMINAL:
                    raise ValueError('这条记录已有未确认的标准版任务，请继续查询原任务，不能重复提交。')
                if options.get('resubmit') is not True or options.get('replaceRequestId') != task['requestId']:
                    raise ValueError('这条记录已有标准版任务。只有明确确认重新提交且原任务编号一致时，才会创建新任务；不会重复提交旧请求。')
            if not note.get('audioFile'):
                raise ValueError('请先录音或导入音视频。')
            providers.asr_headers(config)
            validate_upload(config, options)
            if not jobs.FFMPEG or not jobs.FFPROBE:
                raise ValueError('未找到 FFmpeg，请检查本地音频工具。')
            if task:
                storage.update_note(note_id, asrTaskHistory=note.get('asrTaskHistory', []) + [task])
            next_task = {'attemptId': str(uuid.uuid4()), 'state': 'preparing', 'uploadConsentedAt': storage.now(),
                         'autoSummarize': options.get('autoSummarize', True), 'errorCount': 0}
            updated = storage.update_note(note_id, asrTask=next_task, asrComplete=False, status='transcribing',
                         stage='准备本条音频', error='', language=options.get('language', note['language']),
                         template=options.get('template', note['template']))
        elif action == 'summarize':
            if not note['transcript'].strip() or note.get('asrComplete') is False:
                raise ValueError('请先完成整条录音转写，或粘贴完整文稿。')
            if not config.get('deepseekApiKey'):
                raise ValueError('请先在设置中填写 DeepSeek API Key。')
            updated = storage.update_note(note_id, status='summarizing', stage='开始提炼', error='')
        else:
            raise ValueError('不支持此操作。')
        jobs.ACTIVE.add(note_id)

    def worker():
        try:
            if action == 'transcribe':
                submit(note_id, config, options.get('autoSummarize', True))
            else:
                jobs.summarize(note_id, config, options.get('template', note['template']), options.get('language', note['language']))
        except Exception as exc:
            if action == 'summarize':
                if isinstance(exc, budget.BudgetExceeded):
                    storage.update_note(note_id, status='ready', stage=str(exc), error='',
                                        budgetSummaryPending=True, budgetWaitUntil=exc.resetsAt,
                                        autoProcessState='waiting')
                else:
                    storage.update_note(note_id, status='error', stage='提炼未完成',
                                        error=str(exc) if isinstance(exc, ValueError) else '提炼中断，原文已保留。')
            else:
                handle_unexpected(note_id, exc)
        finally:
            with jobs.LOCK:
                jobs.ACTIVE.discard(note_id)
    jobs.POOL.submit(worker)
    return updated


def run_due_queries():
    for note in storage.all_notes():
        if note.get('trashedAt'):
            continue
        task = note.get('asrTask') or {}
        if note.get('budgetWaitUntil') and elapsed(note['budgetWaitUntil']) < 0:
            # Already-submitted queries always proceed despite the allowance.
            if not task.get('requestId') or task.get('state') == 'completed':
                continue
        if (((task.get('state') == 'completed' and task.get('summaryState') == 'waiting') or note.get('budgetSummaryPending'))
                and note.get('asrComplete') is True and str(note.get('transcript') or '').strip()
                and not note.get('transcriptPurgedAt') and not note.get('groupParentId')):
            config = storage.settings(secrets=True)
            if config.get('deepseekApiKey'):
                with jobs.LOCK:
                    if note['id'] not in jobs.ACTIVE:
                        jobs.ACTIVE.add(note['id'])
                        jobs.POOL.submit(_run_summary, note['id'], config)
            continue
        if note.get('status') != 'transcribing' or task.get('state') not in PENDING or not task.get('requestId'):
            continue
        if task.get('nextCheckAt') and elapsed(task['nextCheckAt']) < 0:
            continue
        with jobs.LOCK:
            if note['id'] in jobs.ACTIVE:
                continue
            jobs.ACTIVE.add(note['id'])
            jobs.POOL.submit(_run_query, note['id'])


def start_scheduler():
    global SCHEDULER
    if SCHEDULER and SCHEDULER.is_alive():
        return
    SCHEDULER_STOP.clear()

    def loop():
        while not SCHEDULER_STOP.wait(1):
            try:
                run_due_queries()
            except Exception:
                # A transient local failure cannot justify re-submitting audio.
                continue
    SCHEDULER = threading.Thread(target=loop, name='tingji-asr-query-scheduler', daemon=True)
    SCHEDULER.start()
