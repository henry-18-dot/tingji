from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import providers, storage

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tingji-job')
ACTIVE = set()
LOCK = threading.RLock()
FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')
CREATE_NO_WINDOW = 0x08000000 if __import__('os').name == 'nt' else 0


def run_media(args, timeout=180):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        raise ValueError('音频读取超时或转换工具不可用。原文件已保留，请检查文件后重试。') from None
    if result.returncode:
        raise ValueError('无法读取这份音视频，请确认文件能正常播放且包含音轨。原文件已保留。')
    return result.stdout


def duration(path):
    if not FFMPEG or not FFPROBE:
        raise ValueError('未找到 FFmpeg 音频工具，请查看使用说明中的环境要求。')
    data = json.loads(run_media([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'json', str(path)], 30))
    try:
        seconds = float(data['format']['duration'])
    except (KeyError, TypeError, ValueError):
        # Browser WebM may have no duration until remuxed.
        repaired = path.parent / (path.stem + '.playable.mka')
        run_media([FFMPEG, '-nostdin', '-y', '-v', 'error', '-i', str(path), '-vn', '-c:a', 'copy', str(repaired)])
        data = json.loads(run_media([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(repaired)], 30))
        try:
            seconds = float(data['format']['duration'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('无法确定音频时长，请先下载原录音并转换成 WAV 或 MP3 后重试。') from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('录音为空或时长无效。')
    if seconds > 5 * 3600:
        raise ValueError('当前单条录音支持最长 5 小时，请拆成几份导入。原文件已保留。')
    return seconds


def stamp(seconds):
    value = max(0, int(seconds))
    return f'{value // 3600:02}:{value % 3600 // 60:02}:{value % 60:02}' if value >= 3600 else f'{value // 60:02}:{value % 60:02}'


def normalize_segments(result, offset, chunk_index, multi, fallback_duration):
    segments = []
    for utterance in result.get('utterances') or []:
        text = str(utterance.get('text') or '').strip()
        if not text:
            continue
        start = offset + max(0, float(utterance.get('start_time') or 0) / 1000)
        end = offset + max(0, float(utterance.get('end_time') or 0) / 1000)
        speaker_id = (utterance.get('additions') or {}).get('speaker')
        speaker = f'说话人 {speaker_id}' if speaker_id is not None else ''
        if speaker and multi:
            speaker = f'第 {chunk_index + 1} 段 · {speaker}'
        segments.append({'start': round(start, 3), 'end': round(max(start, end), 3), 'text': text, 'speaker': speaker})
    if not segments and str(result.get('text') or '').strip():
        segments = [{'start': offset, 'end': offset + fallback_duration, 'text': result['text'].strip(), 'speaker': ''}]
    return segments


def merge_segments(previous, incoming):
    result = list(previous)
    normalize = lambda s: re.sub(r'[\W_]', '', s).lower()
    boundary = max((s['end'] for s in previous), default=0)
    for item in incoming:
        text = normalize(item['text'])
        # Compare only to the previous chunk, in its actual overlap. Repetitions
        # within the same chunk (including different speakers saying yes) are facts.
        duplicate = item['start'] < boundary and any(
            abs(old['start'] - item['start']) <= 0.7 and normalize(old['text']) == text
            for old in previous[-5:])
        if not duplicate:
            result.append(item)
    return sorted(result, key=lambda item: item['start'])


def format_transcript(segments):
    return '\n\n'.join(f'[{stamp(s["start"])}]' + (f' {s["speaker"]}' if s['speaker'] else '') + '\n' + s['text'] for s in segments)


def transcribe(note_id, config, auto_summary=True):
    from .standard_jobs import submit
    return submit(note_id, config, auto_summary)


def summarize(note_id, config, template, language):
    from . import course_naming, lifecycle
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt'):
        return note
    if not note['transcript'].strip() or note.get('asrComplete') is False:
        raise ValueError('请先完成整条录音转写。')
    recorded = course_naming.recorded_time(note)
    context = {'note_id': note['id'], 'source_name': note.get('sourceName', ''),
               'recording_date': recorded.date().isoformat() if recorded else '',
               'course_candidate': note.get('courseName', '')}
    result = providers.summarize(note['transcript'], config, template, language,
                                 lambda stage: storage.update_note(note_id, stage=stage), context)
    if not isinstance(result, str) or not result.strip():
        raise ValueError('未生成笔记，请重试整理。')
    with storage.LOCK:
        current = storage.get_note(note_id)
        if current.get('transcript') != note['transcript'] or current.get('transcriptPurgedAt'):
            raise ValueError('文字已更新，请重新整理。')
        naming = course_naming.infer_fields({**current, 'summary': result, 'summaryStale': False})
        storage.update_note(note_id, summary=result, status='ready', stage='整理完成', error='',
                            template=template, language=language, summaryStale=False, **naming)
        return lifecycle.purge_transcript(note_id)


def start(note_id, action, options=None):
    from .standard_jobs import start as start_standard
    options = options or {}
    config = storage.settings(secrets=True)
    note = storage.get_note(note_id)
    if note.get('transcriptPurgedAt'):
        return note
    # Existing standard requests retain their original query and resubmit flow.
    if action != 'transcribe' or config.get('asrMode') != 'fast' or (note.get('asrTask') or {}).get('requestId'):
        return start_standard(note_id, action, options)
    from .fast_jobs import start_worker
    with LOCK:
        note = storage.get_note(note_id)
        if note_id in ACTIVE or note.get('status') in ('transcribing', 'summarizing'):
            raise ValueError('这条记录正在处理中，请等待完成。')
        if (note.get('fastTask') or {}).get('state') == 'completed':
            return note
        providers.asr_headers(config)
        if options.get('asrMode') != 'fast' or options.get('audioUploadConsent') is not True:
            raise ValueError('请确认使用单独开通的极速版，将音频发送到豆包语音。')
        if not note.get('audioFile'):
            raise ValueError('请先录音或导入音视频。')
        if not FFMPEG or not FFPROBE:
            raise ValueError('未找到 FFmpeg，请检查本地音频工具。')
        if (note.get('fastTask') or {}).get('retryMayCharge') and options.get('explicitRetry') is not True:
            raise ValueError('上次请求结果不确定，重试可能再次计费，请明确确认后继续。')
        updated = storage.update_note(note_id, status='transcribing', stage='准备快速转写', error='',
                       language=options.get('language', note['language']), template=options.get('template', note['template']))
        ACTIVE.add(note_id)

    def worker():
        try:
            start_worker(note_id, config, options.get('autoSummarize', True), options.get('explicitRetry') is True)
        except Exception as exc:
            storage.update_note(note_id, status='error', stage='快速转写未完成',
                error=str(exc) if isinstance(exc, ValueError) else '处理已中断，原录音和已完成文字已保留。')
        finally:
            with LOCK:
                ACTIVE.discard(note_id)
    POOL.submit(worker)
    return updated


def resume_queries():
    from .standard_jobs import start_scheduler
    return start_scheduler()


def chat(note_id, message):
    with LOCK:
        if note_id in ACTIVE or storage.get_note(note_id).get('status') in ('transcribing', 'summarizing'):
            raise ValueError('这条记录正在处理中，请等待完成再提问。')
        ACTIVE.add(note_id)
    try:
        note = storage.get_note(note_id)
        if not note['transcript'].strip():
            raise ValueError('请先转写或粘贴文稿。')
        if note.get('asrComplete') is False:
            raise ValueError('这份录音尚未转写完整，请先重试转写。')
        source = note['transcript']
        context_note = ''
        if len(source) > 160000:
            # Select relevant passages but explicitly disclose the narrower evidence basis.
            chunks = providers.split_text(source, 12000)
            terms = re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]{2,}', message.lower())
            scored = sorted(enumerate(chunks), key=lambda pair: sum(pair[1].lower().count(t) for t in terms), reverse=True)[:10]
            source = '\n\n'.join(p for _, p in sorted(scored))
            context_note = '\n原文很长，本轮只提供与问题关键词较相关的选段。回答开头必须说明“本次回答基于选取的原文片段，可能不覆盖全文”，不要断言全文未提及某事。'
        config = storage.settings(secrets=True)
        messages = [{'role': 'system', 'content': providers.BASE_PROMPT + '\n根据资料回答用户问题，优先给出原文时间戳作为依据；资料没有答案就直接说明。用用户提问的语言回答。' + context_note},
                    {'role': 'user', 'content': '<source>\n' + source + '\n</source>'}]
        for item in note['chat'][-12:]:
            if item.get('role') in ('user', 'assistant'):
                messages.append({'role': item['role'], 'content': item['content']})
        messages.append({'role': 'user', 'content': message})
        answer = providers.deepseek(messages, config, 4096)
        history = note['chat'] + [{'role': 'user', 'content': message}, {'role': 'assistant', 'content': answer}]
        storage.update_note(note_id, chat=history)
        return {'answer': answer, 'chat': history}
    finally:
        with LOCK:
            ACTIVE.discard(note_id)
