"""One explicit live ASR attempt, with PCM persisted before any provider feed.

All state transitions use jobs.LOCK. Provider.stop() must run without that lock
because the receiver persists its final snapshot from another thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import math
import os
import re
import threading
import time
import uuid
import wave

from . import fast_providers, jobs, storage
from .providers import ProviderError

MAX_PACKET = 65536
PCM_RATE = 16000
PCM_BYTES_PER_SECOND = 32000
MAX_PCM_BYTES = PCM_BYTES_PER_SECOND * 5 * 3600  # Local disk/WAV guard, not a provider duration promise.
IDLE_SECONDS = 60
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}


@dataclass
class _Attempt:
    note_id: str
    request_id: str
    provider: object
    state: str = 'connecting'
    next_seq: int = 0
    received: int = 0
    last_feed: float = field(default_factory=time.monotonic)
    hashes: dict = field(default_factory=dict)
    final_seen: bool = False


_CURRENT: _Attempt | None = None
_WATCHDOG_THREAD: threading.Thread | None = None
_WATCHDOG_STOP = threading.Event()


def _note_id(value):
    try:
        parsed = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('实时录音编号无效。') from None
    if parsed != value:
        raise ValueError('实时录音编号无效。')
    return parsed


def _pcm_path(note_id):
    return storage.DATA / 'jobs' / _note_id(note_id) / 'live.pcm'


def _task_update(attempt, state=None, **values):
    note = storage.get_note(attempt.note_id)
    task = dict(note.get('liveTask') or {})
    task.update(requestId=attempt.request_id, state=state or attempt.state,
                lastSeq=attempt.next_seq - 1, receivedBytes=attempt.received,
                lastActivityAt=storage.now())
    task.update(values)
    return task


def _snapshot(result, seconds):
    if not isinstance(result, dict) or not isinstance(result.get('text', ''), str):
        raise ValueError('实时结果格式无效。')
    utterances = result.get('utterances') or []
    if not isinstance(utterances, list):
        raise ValueError('实时分句格式无效。')
    segments = jobs.normalize_segments(result, 0, 0, False, seconds)
    meaningful = [item for item in utterances if isinstance(item, dict) and str(item.get('text') or '').strip()]
    if meaningful and len(meaningful) == len(segments):
        for segment, item in zip(segments, meaningful):
            segment['definite'] = item.get('definite') is True
    text = result.get('text', '')
    if not text and segments:
        text = '\n'.join(item['text'] for item in segments)
    return {'transcript': text, 'segments': segments}


def _on_result(note_id, event):
    with jobs.LOCK:
        attempt = _CURRENT
        if not attempt or attempt.note_id != note_id or attempt.state not in ('connecting', 'streaming', 'stopping'):
            return
        if not isinstance(event, dict) or event.get('request_id') != attempt.request_id:
            raise ValueError('实时结果与当前请求不匹配。')
        values = _snapshot(event.get('result'), attempt.received / PCM_BYTES_PER_SECOND)
        attempt.final_seen = attempt.final_seen or event.get('final') is True
        # Results are full snapshots, including second-pass corrections, not deltas.
        storage.update_note(note_id, **values, asrComplete=False,
                            liveTask=_task_update(attempt), status='transcribing',
                            stage='正在等待最后一句' if attempt.state == 'stopping' else '正在实时转写', error='')


def _seal_wav(note_id):
    source = _pcm_path(note_id)
    size = source.stat().st_size if source.exists() else 0
    aligned_size = size - size % 2
    target = storage.DATA / 'audio' / (note_id + '.live.wav')
    temporary = target.with_suffix('.wav.tmp')
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open('w+b') as output:
            with wave.open(output, 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(PCM_RATE)
                if aligned_size:
                    with source.open('rb') as pcm:
                        remain = aligned_size
                        while remain:
                            block = pcm.read(min(remain, 1024 * 1024))
                            if not block:
                                raise OSError('PCM changed during sealing')
                            audio.writeframesraw(block)
                            remain -= len(block)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
        # Keep the raw PCM as a recovery source, including any incomplete final sample.
        return dict(audioFile=target.name, audioUrl='/api/audio/' + note_id,
                    fileSize=target.stat().st_size, duration=aligned_size / PCM_BYTES_PER_SECOND), size % 2 == 0
    finally:
        temporary.unlink(missing_ok=True)


def _finish(attempt, state, message='', result=None):
    """Caller holds jobs.LOCK; never reconnects or resubmits."""
    global _CURRENT
    values = {}
    if result is not None:
        values.update(_snapshot(result, attempt.received / PCM_BYTES_PER_SECOND))
    try:
        audio, aligned = _seal_wav(attempt.note_id)
        values.update(audio)
        if not aligned:
            state, message = 'interrupted', '录音末尾有未完成采样，已保留原始片段与可播放部分。'
    except (OSError, wave.Error, EOFError):
        state, message = 'interrupted', '录音封装未完成，原始 PCM 仍保留在电脑，重新打开听记后可恢复。'
    complete = state == 'completed'
    attempt.state = state
    try:
        note = storage.update_note(attempt.note_id, **values, asrComplete=complete,
            status='ready' if complete else 'error',
            stage='实时转写完成' if complete else '实时录音已中断', error=message,
            liveTask=_task_update(attempt, state, completedAt=storage.now()))
    finally:
        jobs.ACTIVE.discard(attempt.note_id)
        if _CURRENT is attempt:
            _CURRENT = None
    return note


def _abort(attempt, state='failed', message='实时转写中断，录音与已返回文字已保留；不会自动重连或补转。'):
    try:
        attempt.provider.close()
    except Exception:
        pass
    return _finish(attempt, state, message)


def _maintain():
    """Close failed/idle connections; never connect, retry, or submit audio."""
    attempt = _CURRENT
    if not attempt or attempt.state == 'stopping':
        return
    if attempt.provider.state in ('failed', 'closed') or attempt.provider.error:
        _abort(attempt)
    elif time.monotonic() - attempt.last_feed > IDLE_SECONDS:
        _abort(attempt, 'interrupted', '超过一分钟未收到录音，已停止本次识别并保留音频与文字；不会自动重连。')


def maintenance_tick():
    """One local-only maintenance pass, also usable for deterministic tests."""
    with jobs.LOCK:
        _maintain()


def start_watchdog(interval=5):
    """Server lifetime hook: detect closed tabs even without another HTTP call."""
    global _WATCHDOG_THREAD, _WATCHDOG_STOP
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError('实时维护间隔必须大于零。')
    with jobs.LOCK:
        if _WATCHDOG_THREAD and _WATCHDOG_THREAD.is_alive() and not _WATCHDOG_STOP.is_set():
            return _WATCHDOG_THREAD
        halt = threading.Event()
        _WATCHDOG_STOP = halt

        def worker():
            while not halt.wait(interval):
                try:
                    maintenance_tick()
                except Exception:
                    # Never include raw provider/storage exceptions or credentials in logs.
                    logging.getLogger(__name__).warning('实时会话维护未完成；本机原始片段保留，未重新连接语音服务。')

        _WATCHDOG_THREAD = threading.Thread(target=worker, name='tingji-live-maintenance', daemon=True)
        _WATCHDOG_THREAD.start()
        return _WATCHDOG_THREAD


def stop_watchdog():
    """Server shutdown hook: stop maintenance and preserve any unfinished audio."""
    global _WATCHDOG_THREAD
    with jobs.LOCK:
        _WATCHDOG_STOP.set()
        worker = _WATCHDOG_THREAD
        _WATCHDOG_THREAD = None
    if worker and worker is not threading.current_thread():
        worker.join(timeout=2)
    with jobs.LOCK:
        if _CURRENT:
            _abort(_CURRENT, 'interrupted', '电脑服务已停止，原录音和已返回文字已保留；未自动重新识别。')


def start(config, language='auto', template='general', title='实时录音'):
    global _CURRENT
    if language not in ('auto', 'zh', 'en'):
        raise ValueError('实时转写只支持中文和英文。')
    if template not in ('general', 'meeting', 'lecture', 'interview'):
        raise ValueError('请选择有效的提炼模板。')
    if not isinstance(title, str) or len(title) > 160:
        raise ValueError('录音标题格式无效。')
    with jobs.LOCK:
        _maintain()
        if _CURRENT:
            raise ValueError('已有一条实时录音，请先结束后再开始。')
        note_id, request_id = str(uuid.uuid4()), str(uuid.uuid4())
        # Constructor validates credentials before creating a note or making a connection.
        provider = fast_providers.StreamSession(config, request_id,
                    on_result=lambda event: _on_result(note_id, event), language=language)
        attempt = _Attempt(note_id, request_id, provider)
        path = _pcm_path(note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb'):
            pass
        storage.create_note(title=title, id=note_id, language=language, template=template,
            status='transcribing', stage='正在连接实时转写', asrComplete=False,
            sourceName=(title.strip() or '实时录音') + '.wav',
            liveTask={'requestId': request_id, 'state': 'connecting', 'lastSeq': -1,
                      'receivedBytes': 0, 'startedAt': storage.now(), 'lastActivityAt': storage.now()})
        _CURRENT = attempt
        jobs.ACTIVE.add(note_id)
    try:
        provider.start()
        with jobs.LOCK:
            if _CURRENT is not attempt or attempt.state != 'connecting' or provider.state != 'streaming':
                raise ProviderError('实时连接已取消。')
            attempt.state = 'streaming'
            attempt.last_feed = time.monotonic()
            return storage.update_note(note_id, liveTask=_task_update(attempt), stage='正在实时转写')
    except Exception:
        with jobs.LOCK:
            if _CURRENT is attempt:
                _abort(attempt, message='实时连接未能完成，记录已保留；可能已产生请求用量，不会自动重连。')
        raise ProviderError('实时连接未能完成，请查看保留的记录及语音服务权限；不会自动重连。') from None


def feed(note_id, seq, pcm_bytes, sha256):
    _note_id(note_id)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError('录音分包序号无效。')
    if not isinstance(pcm_bytes, (bytes, bytearray, memoryview)):
        raise ValueError('录音需要 16 kHz、16 bit、单声道 PCM 数据。')
    pcm = bytes(pcm_bytes)
    if not 0 < len(pcm) <= MAX_PACKET or len(pcm) % 2:
        raise ValueError('每个录音分包需为完整 16 bit 采样，且不超过 64 KB。')
    if not re.fullmatch(r'[a-f0-9]{64}', str(sha256 or '')) or hashlib.sha256(pcm).hexdigest() != sha256:
        raise ValueError('录音分包校验失败，尚未发送。')
    with jobs.LOCK:
        _maintain()
        attempt = _CURRENT
        if not attempt or attempt.note_id != note_id:
            raise ValueError('这次实时录音已结束，已保存内容仍在；不会自动重新连接。')
        if seq < attempt.next_seq:
            if attempt.hashes.get(seq) != sha256:
                raise ValueError('同一录音分包出现不同内容，已保留先前数据。')
            return {'accepted': True, 'seq': seq, 'nextSeq': attempt.next_seq,
                    'duplicate': True, 'receivedBytes': attempt.received}
        if attempt.state != 'streaming':
            raise ValueError('正在结束实时录音，不能再发送分包。')
        if seq != attempt.next_seq:
            raise ValueError('录音分包顺序不连续，请先发送缺少的分包。')
        if attempt.received + len(pcm) > MAX_PCM_BYTES:
            _abort(attempt, 'interrupted', '本次录音达到本机时长上限，已保留已有内容；请另开录音。')
            raise ValueError('本次录音已达到本机时长上限。')
        try:
            with _pcm_path(note_id).open('ab') as output:
                output.write(pcm)
                output.flush()
                os.fsync(output.fileno())
            attempt.hashes[seq] = sha256
            attempt.next_seq += 1
            attempt.received += len(pcm)
            attempt.last_feed = time.monotonic()
            storage.update_note(note_id, liveTask=_task_update(attempt),
                                duration=attempt.received / PCM_BYTES_PER_SECOND)
            attempt.provider.feed(pcm)
        except Exception:
            _abort(attempt)
            raise ProviderError('实时发送或保存中断，已收到的录音与文字已保留；不会重复发送或自动重连。') from None
        return {'accepted': True, 'seq': seq, 'nextSeq': attempt.next_seq,
                'duplicate': False, 'receivedBytes': attempt.received}


def stop(note_id, summarize_after=False):
    _note_id(note_id)
    with jobs.LOCK:
        _maintain()
        attempt = _CURRENT
        if not attempt or attempt.note_id != note_id:
            note = storage.get_note(note_id)
            if (note.get('liveTask') or {}).get('state') in TERMINAL:
                return note
            raise ValueError('找不到正在进行的实时录音。')
        if attempt.state == 'stopping':
            return storage.get_note(note_id)
        if not attempt.received:
            return _abort(attempt, 'cancelled', '未收到录音数据，本次连接已结束。')
        attempt.state = 'stopping'
        storage.update_note(note_id, liveTask=_task_update(attempt), stage='正在等待最后一句')
    try:
        result = attempt.provider.stop()
        with jobs.LOCK:
            if _CURRENT is not attempt:
                return storage.get_note(note_id)
            if attempt.provider.state != 'completed' or not attempt.final_seen:
                return _abort(attempt)
            note = _finish(attempt, 'completed', result=result)
    except Exception:
        with jobs.LOCK:
            if _CURRENT is attempt:
                note = _abort(attempt)
            else:
                note = storage.get_note(note_id)
    if summarize_after is True and note.get('asrComplete') and note['transcript'].strip():
        try:
            return jobs.start(note_id, 'summarize', {'template': note['template'], 'language': note['language']})
        except ValueError:
            return storage.update_note(note_id, stage='转写完成，提炼未开始',
                                       error='请检查 DeepSeek 设置后手动提炼；完整原文与录音已保存。')
    return note


def cancel(note_id):
    _note_id(note_id)
    with jobs.LOCK:
        attempt = _CURRENT
        if attempt and attempt.note_id == note_id:
            return _abort(attempt, 'cancelled', '已结束本次实时识别，录音和已有文字已保留，尚未确认全文。')
        note = storage.get_note(note_id)
        if (note.get('liveTask') or {}).get('state') in TERMINAL:
            return note
        raise ValueError('找不到这次实时录音。')


def recover():
    """Seal abandoned local PCM after startup; never construct a provider session."""
    recovered = []
    with jobs.LOCK:
        for note in storage.all_notes():
            task = note.get('liveTask') or {}
            if not isinstance(task, dict) or not task.get('requestId'):
                continue
            if _CURRENT and _CURRENT.note_id == note['id']:
                continue
            if task.get('state') in TERMINAL and note.get('audioFile'):
                continue
            try:
                _note_id(note['id'])
                audio, _ = _seal_wav(note['id'])
            except (ValueError, OSError, wave.Error, EOFError):
                continue
            task = dict(task, state='interrupted', completedAt=storage.now())
            recovered.append(storage.update_note(note['id'], **audio, liveTask=task,
                status='error', stage='已恢复中断录音', asrComplete=False,
                error='程序曾中断，已从本机片段恢复录音与已有文字；未自动重连或重复识别。'))
            jobs.ACTIVE.discard(note['id'])
    return recovered
