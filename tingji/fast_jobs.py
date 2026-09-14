"""Resumable local flash jobs. Completed chunks are never automatically re-billed."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import uuid

from . import fast_providers, jobs, lifecycle, storage
from .providers import ProviderError, AsrSubmitRejected, AsrSubmitUncertain, _asr_id, hotword_list

CHUNK_SECONDS = 2000
OVERLAP_SECONDS = 2
RUNNING = set()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _source(note):
    name = note.get('audioFile')
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ProviderError('请先录音或导入音视频。')
    base = (storage.DATA / 'audio').resolve()
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ProviderError('找不到本条原录音，请保留记录并重新检查音频文件。')
    stat = path.stat()
    return path, {'file': name, 'size': stat.st_size, 'mtimeNs': stat.st_mtime_ns}


def _spec(config, language):
    return _digest({'version': 1, 'resource': fast_providers.FLASH_RESOURCE_ID,
                    'language': language, 'hotwords': hotword_list(config.get('hotwords', '')),
                    'codec': 'mp3-mono-16k-64k', 'chunkSeconds': CHUNK_SECONDS, 'overlapSeconds': OVERLAP_SECONDS})


def _folder(note_id, task):
    base = (storage.DATA / 'jobs').resolve()
    folder = (base / _asr_id(note_id) / ('fast-' + _asr_id(task['attemptId']))).resolve()
    if not folder.is_relative_to(base):
        raise ProviderError('本机转写缓存目录不正确。')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _write_json(path, value):
    temporary = path.with_name(path.name + '.' + str(uuid.uuid4()) + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _save(note_id, task, **values):
    return storage.update_note(note_id, fastTask=task, **values)


def _plan(seconds):
    chunks = []
    for index in range(math.ceil(seconds / CHUNK_SECONDS)):
        start = max(0, index * CHUNK_SECONDS - (OVERLAP_SECONDS if index else 0))
        end = min(seconds, (index + 1) * CHUNK_SECONDS)
        chunks.append({'index': index, 'start': start, 'duration': round(end - start, 6), 'state': 'pending'})
    return chunks


def _binding(note_id, task, chunk):
    return {'noteId': note_id, 'attemptId': task['attemptId'], 'asrSpecHash': task['asrSpecHash'],
            'sourceInfo': task['sourceInfo'], 'index': chunk['index'], 'start': chunk['start'],
            'duration': chunk['duration'], 'audioHash': chunk.get('audioHash'), 'requestId': chunk.get('requestId')}


def _cache_path(folder, chunk):
    return folder / f"chunk-{chunk['index']:03d}-{_asr_id(chunk['requestId'])}.json"


def _cached(note_id, task, chunk, folder):
    if not chunk.get('requestId') or not chunk.get('audioHash'):
        return None
    try:
        value = json.loads(_cache_path(folder, chunk).read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('complete') is not True or value.get('binding') != _binding(note_id, task, chunk):
            return None
        result = fast_providers._result(value['result'])
        if not result['text'] and not result['utterances'] and not result.get('silent'):
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _prepare(source, folder, chunk):
    target = folder / f"chunk-{chunk['index']:03d}.mp3"
    if target.is_file() and chunk.get('audioHash') and 0 < target.stat().st_size <= fast_providers.FLASH_MAX_BYTES:
        if _file_hash(target) == chunk['audioHash']:
            return target
    # Unique temporary output prevents partial FFmpeg output being reused after interruption.
    temporary = folder / f"chunk-{chunk['index']:03d}-{uuid.uuid4()}.tmp.mp3"
    jobs.run_media([jobs.FFMPEG, '-nostdin', '-y', '-v', 'error', '-ss', str(chunk['start']),
                    '-i', str(source), '-t', str(chunk['duration']), '-vn', '-ac', '1', '-ar', '16000',
                    '-c:a', 'libmp3lame', '-b:a', '64k', str(temporary)], timeout=900)
    if not temporary.is_file() or not 0 < temporary.stat().st_size <= fast_providers.FLASH_MAX_BYTES:
        raise ProviderError('本机准备的分段为空或超过 20 MB，尚未发送这一段。原录音和已完成段保留。')
    temporary.replace(target)
    chunk.update(audioHash=_file_hash(target), uploadSize=target.stat().st_size, state='prepared')
    return target


def _publish(note_id, task, results, *, complete=False):
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt'):
        return note
    if _digest(note.get('transcript', '')) != task['lastPublishedHash']:
        raise ProviderError('原文在转写期间被编辑，已停止自动覆盖；已收到的语音结果缓存在本机。')
    segments = []
    multi = len(task['chunks']) > 1
    for chunk in task['chunks']:
        if chunk['index'] in results:
            incoming = jobs.normalize_segments(results[chunk['index']], chunk['start'], chunk['index'], multi, chunk['duration'])
            segments = jobs.merge_segments(segments, incoming)
    task['completedChunks'] = len(results)
    values = {'asrComplete': complete and bool(segments)}
    if segments:
        text = jobs.format_transcript(segments)
        task['lastPublishedHash'] = _digest(text)
        values.update(transcript=text, segments=segments, transcriptEdited=False,
                      summaryStale=bool(note.get('summary')))
    return _save(note_id, task, **values)


def _manual(note_id, task, chunk, message, *, uncertain=True):
    chunk['state'] = 'submit_unknown' if uncertain else 'rejected'
    task.update(state=chunk['state'], currentChunk=chunk['index'] + 1,
                retryMayCharge=uncertain, errorAt=storage.now())
    return _save(note_id, task, status='error', asrComplete=False,
                 stage='该段结果未确认，已停止自动发送' if uncertain else '该段请求被拒绝，已完成内容保留', error=message)


def _run(note_id, config, summarize_after, explicit_retry):
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt'):
        return note
    if (note.get('asrTask') or {}).get('requestId'):
        raise ProviderError('这条记录已有标准版任务，请保留并查询原任务；需要极速转写时请创建新记录，避免覆盖或重复收费。')
    task = note.get('fastTask') or {}
    if task.get('state') == 'completed':
        return note  # Repeated action cannot re-run ASR or DeepSeek on a finished task.
    source, source_info = _source(note)
    language = note.get('language', 'auto')
    fast_providers._language(language)
    fast_providers._headers(config, str(uuid.uuid4()), fast_providers.FLASH_RESOURCE_ID)
    if not jobs.FFMPEG or not jobs.FFPROBE:
        raise ProviderError('未找到 FFmpeg，请检查本机音频工具。')
    spec_hash = _spec(config, language)
    if task:
        if task.get('version') != 1 or task.get('sourceInfo') != source_info or task.get('asrSpecHash') != spec_hash:
            raise ProviderError('原录音、语种或热词设置已变化，不能混用旧分段缓存；请保留原记录并创建新记录转写。')
        task['autoSummarize'] = bool(summarize_after)
    else:
        seconds = jobs.duration(source)
        if not math.isfinite(seconds) or not 0 < seconds <= 5 * 3600:
            raise ProviderError('本产品单条录音需大于零且不超过 5 小时。')
        chunks = _plan(seconds)
        task = {'version': 1, 'attemptId': str(uuid.uuid4()), 'state': 'preparing',
                'sourceInfo': source_info, 'asrSpecHash': spec_hash, 'language': language,
                'chunks': chunks, 'totalChunks': len(chunks), 'completedChunks': 0, 'currentChunk': 1,
                'autoSummarize': bool(summarize_after), 'startedAt': storage.now(), 'retryMayCharge': False,
                'plannedBillableSeconds': round(sum(c['duration'] for c in chunks), 3),
                'overlapSeconds': OVERLAP_SECONDS, 'lastPublishedHash': _digest(note.get('transcript', ''))}
        folder = _folder(note_id, task)
        _write_json(folder / 'original-text.json', {k: note.get(k) for k in ('transcript', 'segments', 'summary', 'transcriptEdited')})
        _save(note_id, task, asrComplete=False, duration=round(seconds, 3), status='transcribing',
              stage=f'在本机准备音频，共 {len(chunks)} 段；边界重叠 {OVERLAP_SECONDS} 秒' if len(chunks) > 1 else '在本机准备音频', error='')
    folder = _folder(note_id, task)
    results = {}
    # Restore every available chunk before publishing. A resume must not briefly
    # replace an existing multi-chunk transcript with only its first cached chunk.
    for chunk in task['chunks']:
        result = _cached(note_id, task, chunk, folder)
        if result is not None:
            chunk.update(state='completed', completedAt=chunk.get('completedAt') or storage.now())
            results[chunk['index']] = result
    if results:
        _publish(note_id, task, results)
    for chunk in task['chunks']:
        # Atomic response files were recovered BEFORE any decision to submit again.
        if chunk['index'] in results:
            continue
        if chunk.get('state') in ('submitting', 'submit_unknown', 'rejected', 'completed'):
            uncertain = chunk['state'] != 'rejected'
            if not explicit_retry:
                return _manual(note_id, task, chunk,
                    '这一段此前已提交，但没有可恢复的完整结果。极速接口不能查询恢复；请检查控制台后明确选择重试，重试可能再次计费。' if uncertain else
                    '这一段此前被服务拒绝。请修正服务设置后手动选择重试；不会自动重复提交。', uncertain=uncertain)
            history = chunk.setdefault('history', [])
            history.append({k: chunk.get(k) for k in ('requestId', 'state', 'submittedAt', 'code', 'logId')})
            chunk.update(state='pending', retryConfirmedAt=storage.now())
            task.update(retryMayCharge=uncertain, retryCount=task.get('retryCount', 0) + 1)
        if _source(storage.get_note(note_id))[1] != source_info:
            raise ProviderError('原录音文件发生变化，已停止发送后续分段；已完成内容保留。')
        if _digest(storage.get_note(note_id).get('transcript', '')) != task['lastPublishedHash']:
            raise ProviderError('原文已被编辑，已停止后续收费转写；已完成结果保留在本机。')
        task.update(state='preparing', currentChunk=chunk['index'] + 1)
        _save(note_id, task, status='transcribing', stage=f"在本机准备第 {chunk['index'] + 1}/{len(task['chunks'])} 段", error='')
        audio = _prepare(source, folder, chunk)
        chunk.update(requestId=str(uuid.uuid4()), state='submitting', submittedAt=storage.now())
        task['state'] = 'transcribing'
        # This durable transition MUST precede the only potentially billable call.
        _save(note_id, task, status='transcribing', stage=f"极速转写第 {chunk['index'] + 1}/{len(task['chunks'])} 段", error='')
        try:
            result = fast_providers.transcribe_fast(audio, config, chunk['requestId'], language)
        except AsrSubmitUncertain as exc:
            chunk.update(code=exc.code, logId=exc.log_id)
            return _manual(note_id, task, chunk, str(exc))
        except AsrSubmitRejected as exc:
            chunk.update(code=exc.code, logId=exc.log_id)
            return _manual(note_id, task, chunk, str(exc), uncertain=False)
        except ProviderError as exc:
            # ProviderError alone is validation before its HTTP call; it is safe to prepare again.
            chunk['state'] = 'local_error'
            task['state'] = 'local_error'
            return _save(note_id, task, status='error', stage='这一段在发送前检查失败', error=str(exc), asrComplete=False)
        _write_json(_cache_path(folder, chunk), {'complete': True, 'binding': _binding(note_id, task, chunk), 'result': result})
        chunk.update(state='completed', completedAt=storage.now())
        results[chunk['index']] = result
        _publish(note_id, task, results)
    note = _publish(note_id, task, results, complete=True)
    task.update(state='completed', completedAt=storage.now(), currentChunk=len(task['chunks']))
    if not note.get('asrComplete'):
        return _save(note_id, task, status='error', stage='全部分段完成，未检测到可识别语音',
                     error='原录音与已有文稿保留，请先回听确认；未使用空识别结果覆盖原文。')
    auto = summarize_after and bool(config.get('deepseekApiKey'))
    note = _save(note_id, task, status='summarizing' if auto else 'ready',
                  stage='转写完成，开始提炼' if auto else '极速转写完成', error='')
    lifecycle.archive_audio(note_id, at=task['completedAt'])
    if auto:
        try:
            jobs.summarize(note_id, config, note.get('template', 'general'), language)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ProviderError) else '提炼中断；转写原文已经完整保存，可以单独重试提炼。'
            storage.update_note(note_id, status='error', stage='转写完成，提炼未完成', error=message)
    return storage.get_note(note_id)


def start_worker(note_id, config, summarize_after=True, explicit_retry=False):
    """Synchronous worker; caller owns thread scheduling and jobs.ACTIVE.

    Explicit retry authorizes only incomplete/unknown chunks, never completed cache hits.
    Public APIs must redact fastTask chunks, sourceInfo, hashes, IDs and attempt history.
    """
    with jobs.LOCK:
        if note_id in RUNNING:
            raise ProviderError('这条极速转写正在处理中，不能重复启动。')
        RUNNING.add(note_id)
    try:
        return _run(note_id, config, summarize_after, explicit_retry is True)
    except Exception as exc:
        note = storage.get_note(note_id)
        task = note.get('fastTask') or {}
        if not task or (note.get('asrTask') or {}).get('requestId'):
            raise
        pending = next((c for c in task.get('chunks', []) if c.get('state') == 'submitting'), None)
        if pending:
            return _manual(note_id, task, pending, '该段提交后处理意外中断，可能已产生用量。原录音和部分文字保留；请检查控制台后决定是否手动重试。')
        task['state'] = 'local_error'
        message = str(exc) if isinstance(exc, ProviderError) else '本机处理中断。原录音、分段缓存与已有文字保留，可以手动继续。'
        return _save(note_id, task, status='error', stage='本机准备或保存未完成', error=message, asrComplete=False)
    finally:
        with jobs.LOCK:
            RUNNING.discard(note_id)
