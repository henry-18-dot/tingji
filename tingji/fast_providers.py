"""Direct flash and real-time PCM ASR. One attempt per explicit user action.

Official contracts: docs/6561/{1631584,2608628,1354869,2630027}.
No automatic retry, reconnect, or fallback to another billable speech product.
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import wave
import zlib

from websockets.sync.client import connect

from .providers import (ProviderError, AsrSubmitRejected, AsrSubmitUncertain,
                        _asr_id, _asr_post, _asr_response, _asr_duplicate, hotword_list)

FLASH_URL = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
FLASH_RESOURCE_ID = 'volc.bigasr.auc_turbo'
FLASH_SERVICE_MAX_BYTES = 100_000_000
FLASH_MAX_BYTES = 20_000_000  # Deliberate application upload limit, not the service's hard limit.
FLASH_MAX_SECONDS = 7200
STREAM_URL = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async'
STREAM_RESOURCE_ID = 'volc.seedasr.sauc.duration'
PCM_BYTES_PER_SECOND = 32000
PACKET_BYTES = 6400
MAX_BUFFER_BYTES = PCM_BYTES_PER_SECOND * 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _headers(config, request_id, resource):
    headers = {'X-Api-Resource-Id': resource, 'X-Api-Request-Id': _asr_id(request_id)}
    if config.get('asrApiKey'):
        headers['X-Api-Key'] = config['asrApiKey']
    elif config.get('asrAppId') and config.get('asrAccessToken'):
        headers.update({'X-Api-App-Key': config['asrAppId'], 'X-Api-Access-Key': config['asrAccessToken']})
    else:
        raise ProviderError('请先在设置中填写豆包语音 API Key。')
    if any(not isinstance(v, str) or '\r' in v or '\n' in v for v in headers.values()):
        raise ProviderError('语音密钥格式不正确，请重新填写。')
    return headers


def _language(language):
    if language not in ('auto', 'zh', 'en'):
        raise ProviderError('仅支持中文、英文或中英混合识别。')


def _request(config, *, stream=False, language='auto'):
    request = {'model_name': 'bigmodel', 'enable_itn': True, 'enable_punc': True,
               'enable_ddc': False, 'show_utterances': True,
               'enable_speaker_info': stream or language != 'en'}
    if stream:
        request.update(enable_nonstream=True, result_type='full', ssd_version='200')
    words = hotword_list(config.get('hotwords', ''))
    if words:
        request['corpus'] = {'context': json.dumps({'hotwords': [{'word': w} for w in words]}, ensure_ascii=False)}
    return request


def _audio_seconds(path):
    """Read duration locally. Do not remux, modify, or send the user's file."""
    try:
        if path.suffix.lower() == '.wav':
            with wave.open(str(path), 'rb') as audio:
                return audio.getnframes() / audio.getframerate()
        probe = shutil.which('ffprobe')
        if not probe:
            raise ProviderError('需要 FFmpeg 音频工具确认 MP3 时长；也可先转换为 WAV。')
        process = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration',
                                  '-of', 'json', str(path)], capture_output=True, timeout=30,
                                 creationflags=0x08000000 if os.name == 'nt' else 0)
        if process.returncode:
            raise ValueError
        return float(json.loads(process.stdout)['format']['duration'])
    except ProviderError:
        raise
    except (OSError, wave.Error, EOFError, ZeroDivisionError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        raise ProviderError('无法读取音频时长，请使用能正常播放的 WAV 或 MP3。原文件已保留。') from None


def _result(value):
    """Keep provider times/definite fields; map speaker IDs for jobs.normalize_segments."""
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if not isinstance(value, dict):
        raise ValueError('invalid result')
    result = dict(value)
    text = result.get('text', '')
    if not isinstance(text, str):
        raise ValueError('invalid text')
    utterances = result.get('utterances') or []
    if not isinstance(utterances, list):
        raise ValueError('invalid utterances')
    clean = []
    for item in utterances:
        if not isinstance(item, dict) or not isinstance(item.get('text', ''), str):
            raise ValueError('invalid utterance')
        for field in ('start_time', 'end_time'):
            if item.get(field) is not None and (not isinstance(item[field], (int, float)) or not math.isfinite(item[field])):
                raise ValueError('invalid timestamp')
        utterance = dict(item)
        additions = utterance.get('additions') or {}
        if isinstance(additions, str):
            try:
                additions = json.loads(additions)
            except (ValueError, TypeError):
                additions = {}
        additions = dict(additions) if isinstance(additions, dict) else {}
        speaker = utterance.get('speaker_id', additions.get('speaker_id'))
        if speaker is not None and 'speaker' not in additions:
            additions['speaker'] = speaker
        utterance['additions'] = additions
        clean.append(utterance)
    result.update(text=text, utterances=clean)
    return result


def transcribe_fast(path, config, request_id, language='auto'):
    """Transcribe one <=20 MB WAV/MP3, synchronously. Caller persists ID first.

    A lost flash response cannot be reconciled via the standard query endpoint.
    The local upload limit intentionally leaves headroom below the 100 MB service limit.
    """
    _language(language)
    headers = _headers(config, request_id, FLASH_RESOURCE_ID)
    headers['X-Api-Sequence'] = '-1'
    path = Path(path)
    if path.suffix.lower() not in ('.wav', '.mp3'):
        raise ProviderError('极速转写请先准备 WAV 或 MP3 音频。')
    try:
        size = path.stat().st_size
    except OSError:
        raise ProviderError('找不到待转写音频，原记录仍然保留。') from None
    if not 0 < size <= FLASH_MAX_BYTES:
        raise ProviderError('本产品极速直传每段最多 20 MB，请先在本机压缩或分段；服务硬上限为 100 MB。')
    seconds = _audio_seconds(path)
    if not math.isfinite(seconds) or not 0 < seconds <= FLASH_MAX_SECONDS:
        raise ProviderError('极速转写每段必须大于零且不超过 2 小时。')
    try:
        with path.open('rb') as audio_file:
            raw = audio_file.read(FLASH_MAX_BYTES + 1)
    except OSError:
        raise ProviderError('无法读取待转写音频，尚未发送到语音服务。') from None
    if len(raw) != size or len(raw) > FLASH_MAX_BYTES:
        raise ProviderError('音频文件正在变化，请完成录音后再开始转写。')
    audio = {'data': base64.b64encode(raw).decode('ascii'), 'format': path.suffix.lower()[1:]}
    if language != 'auto':
        audio['language'] = {'zh': 'zh-CN', 'en': 'en-US'}[language]
    body = {'user': {'uid': 'tingji-local'}, 'audio': audio, 'request': _request(config, language=language)}
    try:
        status, response_headers, response = _asr_post(FLASH_URL, body, headers, timeout=300)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise AsrSubmitUncertain('极速转写响应未确认，可能已产生用量。该接口不能查询恢复；请保留音频并检查控制台后决定是否手动重转。', request_id=request_id) from None
    code, message, log_id, payload = _asr_response(response_headers, response)
    code = code if re.fullmatch(r'\d{1,10}', code) else ''
    details = dict(code=code, log_id=log_id, request_id=request_id, http_status=status)
    if 200 <= status < 300 and code == '20000003':
        return {'text': '', 'utterances': [], 'silent': True}
    if 200 <= status < 300 and code == '20000000':
        try:
            result = _result(payload.get('result'))
            if not result['text'] and not result['utterances']:
                raise ValueError
            return result
        except (ValueError, TypeError):
            raise AsrSubmitUncertain('服务报告转写成功，但全文未能完整读取。请检查用量后决定是否手动重转；不会自动重复收费。', **details) from None
    if (_asr_duplicate(message) or status in (408, 409) or status >= 500 or code.startswith('550')
            or not code and not 400 <= status < 500 or code and not code.startswith('450')):
        raise AsrSubmitUncertain('极速转写结果状态未确认。该接口不能查询恢复；不会自动重试，请先检查语音服务用量。', **details)
    if code == '45000030':
        raise AsrSubmitRejected('当前密钥没有极速版权限。请在设置中选择标准版。', **details)
    raise AsrSubmitRejected('极速转写请求被拒绝，请检查服务权限、余额和音频格式。', **details)


def _encode_packet(payload, sequence, *, audio=False, final=False):
    data = gzip.compress(payload, mtime=0)
    flags = 3 if final else 1
    header = bytes((0x11, ((2 if audio else 1) << 4) | flags, (0 if audio else 0x10) | 1, 0))
    return header + struct.pack('>iI', -sequence if final else sequence, len(data)) + data


def _decode_packet(message):
    """Decode v1, including optional sequence/event fields and gzip JSON."""
    if not isinstance(message, bytes) or len(message) < 8 or len(message) > MAX_RESPONSE_BYTES:
        raise ValueError('invalid frame')
    version, header_size = message[0] >> 4, (message[0] & 15) * 4
    kind, flags = message[1] >> 4, message[1] & 15
    serialization, compression = message[2] >> 4, message[2] & 15
    if version != 1 or header_size < 4 or header_size > len(message) or flags & 8:
        raise ValueError('invalid header')
    offset = header_size

    def integer(signed=False):
        nonlocal offset
        if len(message) < offset + 4:
            raise ValueError('truncated frame')
        value = struct.unpack_from('>i' if signed else '>I', message, offset)[0]
        offset += 4
        return value

    sequence = integer(True) if flags & 1 else None
    event = integer(True) if flags & 4 else None
    if kind not in (9, 15):
        raise ValueError('unsupported frame')
    code = integer() if kind == 15 else 0
    size = integer()
    if size != len(message) - offset:
        raise ValueError('invalid length')
    raw = message[offset:]
    if compression == 1:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(raw, MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid gzip')
    elif compression != 0:
        raise ValueError('unsupported compression')
    if kind == 15:
        # Error payloads may contain credentials or private transcript text; never expose them.
        payload = {}
    elif serialization == 1:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError('invalid json payload')
    else:
        raise ValueError('unsupported serialization')
    return {'code': code, 'sequence': sequence, 'event': event,
            'final': bool(flags & 2), 'payload': payload}


class StreamSession:
    """Thread-safe PCM input, paced sender, and one receiver; no reconnection.

    on_result(event) runs on the receiver thread and must not call stop().
    event contains result (a full snapshot), final, audio_info and request_id.
    PCM must be little-endian, 16 kHz, signed 16-bit, mono. Persist it before feed().
    feed() accepts at most ten seconds of queued PCM; overflow terminates this attempt.
    stop() sends the final negative sequence and waits for a final server packet.
    """
    def __init__(self, config, request_id, on_result=None, language='auto'):
        _language(language)
        self.request_id = _asr_id(request_id)
        self._headers = _headers(config, request_id, STREAM_RESOURCE_ID)
        self._initial = {'user': {'uid': 'tingji-local'},
                         'audio': {'format': 'pcm', 'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1},
                         'request': _request(config, stream=True)}
        # language intentionally omitted: bidirectional + second-pass uses the mixed Chinese/English model.
        self._callback = on_result
        self._condition = threading.Condition(threading.RLock())
        self._buffer = bytearray()
        self._done = threading.Event()
        self._abort = threading.Event()
        self._finishing = False
        self._ws = None
        self._threads = []
        self._sequence = 2
        self._audio_sent = False
        self._final_sent = False
        self._last_result = {'text': '', 'utterances': []}
        self._error = None
        self._state = 'new'

    @property
    def state(self):
        with self._condition:
            return self._state

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def last_result(self):
        with self._condition:
            return json.loads(json.dumps(self._last_result, ensure_ascii=False))

    def _uncertain(self, message, *, code=''):
        return AsrSubmitUncertain(message, request_id=self.request_id, code=code)

    def _fail(self, error):
        with self._condition:
            if self._state in ('completed', 'closed'):
                return
            if self._error is None:
                self._error = error
            self._state = 'failed'
            self._abort.set()
            self._done.set()
            self._condition.notify_all()
        self._close_socket()

    def _close_socket(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def start(self):
        with self._condition:
            if self._state != 'new':
                raise ProviderError('这一实时转写连接已经启动或结束，不能重复启动。')
            self._state = 'connecting'
        try:
            self._ws = connect(STREAM_URL, additional_headers=self._headers, compression=None, proxy=None,
                               open_timeout=10, close_timeout=1, ping_interval=20, ping_timeout=20,
                               max_size=MAX_RESPONSE_BYTES)
            with self._condition:
                if self._state != 'connecting':
                    self._close_socket()
                    raise self._uncertain('连接启动已取消。请保留本机录音。')
            self._ws.send(_encode_packet(json.dumps(self._initial, ensure_ascii=False).encode('utf-8'), 1))
            # The official demo waits for the initial acknowledgement before sending audio.
            self._handle(_decode_packet(self._ws.recv(timeout=10)))
            with self._condition:
                if self._state != 'connecting':
                    raise self._error or self._uncertain('服务提前结束连接，尚未完成实时录音。')
                self._state = 'streaming'
            self._threads = [threading.Thread(target=self._send_loop, name='tingji-asr-send', daemon=True),
                             threading.Thread(target=self._receive_loop, name='tingji-asr-receive', daemon=True)]
            for thread in self._threads:
                thread.start()
            return self
        except Exception as exc:
            if isinstance(exc, (AsrSubmitRejected, AsrSubmitUncertain)):
                error = exc
            else:
                response = getattr(exc, 'response', None)
                status = getattr(response, 'status_code', None)
                if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409):
                    error = AsrSubmitRejected('实时转写连接被拒绝，请检查流式 2.0 开通状态、密钥权限和余额。', request_id=self.request_id, http_status=status)
                else:
                    error = self._uncertain('未能确认实时转写连接，可能已产生请求用量；不会自动重连。请保留本机录音。')
            self._fail(error)
            raise error from None

    def feed(self, pcm_bytes):
        if not isinstance(pcm_bytes, (bytes, bytearray, memoryview)):
            raise ProviderError('录音分包必须是完整的 16 bit PCM 采样。')
        pcm_bytes = bytes(pcm_bytes)
        if len(pcm_bytes) % 2:
            raise ProviderError('录音分包必须是完整的 16 bit PCM 采样。')
        with self._condition:
            if self._error:
                raise self._error
            if self._state != 'streaming':
                raise ProviderError('实时转写已停止，不能继续发送录音。')
            if len(self._buffer) + len(pcm_bytes) > MAX_BUFFER_BYTES:
                error = self._uncertain('实时音频积压超过 10 秒，已停止本次连接。请保留本机录音，之后手动选择补转；不会自动重连或集中补发。')
            else:
                self._buffer.extend(pcm_bytes)
                self._condition.notify_all()
                return
        self._fail(error)
        raise error

    def _send_loop(self):
        next_send = time.monotonic()
        try:
            while not self._abort.is_set():
                with self._condition:
                    self._condition.wait_for(lambda: self._abort.is_set() or self._finishing or len(self._buffer) >= PACKET_BYTES)
                    if self._abort.is_set():
                        return
                    final = self._finishing and len(self._buffer) <= PACKET_BYTES
                    pcm = bytes(self._buffer[:PACKET_BYTES])
                    del self._buffer[:PACKET_BYTES]
                if self._abort.wait(max(0, next_send - time.monotonic())):
                    return
                if final:
                    # Set before send(): a fast local test/server can reply immediately.
                    with self._condition:
                        self._final_sent = True
                self._audio_sent = self._audio_sent or bool(pcm)
                self._ws.send(_encode_packet(pcm, self._sequence, audio=True, final=final))
                self._sequence += 1
                next_send = time.monotonic() + len(pcm) / PCM_BYTES_PER_SECOND
                if final:
                    return
        except Exception:
            self._fail(self._uncertain('实时音频发送中断，结果可能不完整。请保留本机录音；不会自动重连或重复发送。'))

    def _handle(self, packet):
        payload = packet['payload']
        code = packet['code'] or payload.get('code', 0)
        if code not in (0, '0', 20000000, '20000000', None):
            safe_code = str(code) if re.fullmatch(r'\d{1,10}', str(code)) else ''
            if not self._audio_sent and safe_code.startswith('450'):
                raise AsrSubmitRejected('实时语音服务拒绝了本次请求，请检查参数和服务权限。', code=safe_code, request_id=self.request_id)
            raise self._uncertain('实时语音服务提前终止处理。已收到的文本可能不完整，请保留本机录音；不会自动重连。', code=safe_code)
        if packet['final'] and not self._final_sent:
            raise self._uncertain('语音服务在录音结束前关闭了转写，本机录音需保留；不会把部分文本当成全文。')
        has_result = 'result' in payload
        result = _result(payload['result']) if has_result else self.last_result
        if not has_result and not packet['final']:
            return  # Metadata acknowledgement / keepalive.
        if packet['final'] and not has_result and self._audio_sent:
            raise self._uncertain('服务结束响应没有最终识别结果，已保留最后收到的文本；请保留本机录音。')
        with self._condition:
            self._last_result = result
        event = {'result': self.last_result, 'final': packet['final'],
                 'audio_info': payload.get('audio_info', {}), 'request_id': self.request_id}
        if self._callback:
            self._callback(event)
        if packet['final']:
            with self._condition:
                self._state = 'completed'
                self._abort.set()
                self._done.set()
                self._condition.notify_all()

    def _receive_loop(self):
        try:
            while not self._abort.is_set():
                try:
                    message = self._ws.recv(timeout=1)
                except TimeoutError:
                    continue
                self._handle(_decode_packet(message))
        except (AsrSubmitRejected, AsrSubmitUncertain) as exc:
            self._fail(exc)
        except Exception:
            self._fail(self._uncertain('实时结果接收或保存中断，最后一段可能未完成。请保留本机录音；不会自动重连。'))

    def stop(self, timeout=20):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ProviderError('等待结束的时间必须大于零。')
        if threading.current_thread() in self._threads:
            raise ProviderError('请从录音控制线程结束转写，不要在结果回调中结束连接。')
        with self._condition:
            if self._state == 'new':
                raise ProviderError('实时转写尚未开始。')
            if self._state == 'closed':
                raise self._error or ProviderError('实时转写已取消，未等待最终结果。')
            if self._state == 'streaming':
                self._state = 'stopping'
                self._finishing = True
                self._condition.notify_all()
        if not self._done.wait(timeout):
            self._fail(self._uncertain('等待实时转写最终结果超时，最后一段可能未完成。请保留本机录音；不会自动重连或补转。'))
        self._close_socket()
        if self.error:
            raise self.error
        if self.state != 'completed':
            raise self._uncertain('实时转写未完整结束，请保留本机录音。')
        return self.last_result

    def close(self):
        """Cancel without claiming completion. Local PCM persistence belongs to caller."""
        with self._condition:
            if self._state not in ('completed', 'failed'):
                self._state = 'closed'
            self._abort.set()
            self._done.set()
            self._buffer.clear()
            self._condition.notify_all()
        self._close_socket()
