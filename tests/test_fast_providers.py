"""No paid network calls: HTTP, WebSocket and MP3 probing are locally mocked."""
import base64
import gzip
import json
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from tingji import fast_providers as fast
from tingji.providers import ProviderError, AsrSubmitRejected, AsrSubmitUncertain


def server_packet(payload, *, final=False, sequence=1, compressed=True, code=0, event=None):
    flags = (3 if final else 1) | (4 if event is not None else 0)
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    raw = gzip.compress(raw) if compressed else raw
    packet = bytes((0x11, ((15 if code else 9) << 4) | flags, 0x10 | int(compressed), 0))
    packet += struct.pack('>i', -sequence if final else sequence)
    if event is not None:
        packet += struct.pack('>i', event)
    if code:
        packet += struct.pack('>I', code)
    return packet + struct.pack('>I', len(raw)) + raw


def client_packet(packet):
    sequence, length = struct.unpack('>iI', packet[4:12])
    assert length == len(packet) - 12
    raw = gzip.decompress(packet[12:])
    return packet[1] >> 4, packet[1] & 15, sequence, raw


class FakeSocket:
    def __init__(self, final=True, initial=None):
        self.responses = queue.Queue()
        self.responses.put(initial if initial is not None else server_packet({}))
        self.sent = []
        self.send_times = []
        self.closed = False
        self.final = final
        self.finish_result = {'text': '机器人。Hello.', 'utterances': [
            {'text': '机器人。Hello.', 'start_time': 0, 'end_time': 600,
             'definite': True, 'additions': {'speaker_id': '1'}}]}

    def send(self, packet):
        if self.closed:
            raise OSError('secret key should never appear')
        self.sent.append(packet)
        self.send_times.append(time.monotonic())
        kind, flags, sequence, raw = client_packet(packet)
        if kind == 2 and flags & 2 and self.final:
            self.responses.put(server_packet({'result': self.finish_result,
                                               'audio_info': {'duration': 600}}, final=True, sequence=abs(sequence)))

    def recv(self, timeout=None):
        if self.closed:
            raise OSError('private URL should never appear')
        try:
            packet = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if packet is None:
            raise OSError('closed mock socket')
        return packet

    def close(self):
        self.closed = True
        self.responses.put(None)


class FlashTests(unittest.TestCase):
    config = {'asrApiKey': 'mock-only-key', 'asrResourceId': 'volc.seedasr.auc', 'hotwords': 'PID，机器人;PID'}
    request_id = 'mock-flash-request'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'clip.wav'
        with wave.open(str(self.path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 3200)

    def response(self, code='20000000', body=None, status=200, message='OK'):
        return status, {'X-Api-Status-Code': code, 'X-Api-Message': message, 'X-Tt-Logid': 'safe-log'}, body or {'result': {'text': '你好', 'utterances': []}}

    def test_direct_base64_forced_resource_and_one_attempt(self):
        with patch.object(fast, '_asr_post', return_value=self.response()) as transport:
            result = fast.transcribe_fast(self.path, self.config, self.request_id)
        self.assertEqual(result['text'], '你好')
        self.assertEqual(transport.call_count, 1)
        url, body, headers = transport.call_args.args
        self.assertEqual(url, fast.FLASH_URL)
        self.assertEqual(headers['X-Api-Resource-Id'], 'volc.bigasr.auc_turbo')
        self.assertEqual(headers['X-Api-Sequence'], '-1')
        self.assertEqual(headers['X-Api-Key'], 'mock-only-key')
        self.assertEqual(base64.b64decode(body['audio']['data']), self.path.read_bytes())
        self.assertNotIn('url', body['audio'])
        self.assertNotIn('language', body['audio'])
        self.assertNotIn('ssd_version', body['request'])
        self.assertFalse(body['request']['enable_ddc'])
        self.assertEqual(json.loads(body['request']['corpus']['context'])['hotwords'], [{'word': 'PID'}, {'word': '机器人'}])

    def test_resource_permission_error_points_to_standard_without_fallback_request(self):
        with patch.object(fast, '_asr_post', return_value=self.response(code='45000030', status=403)) as transport:
            with self.assertRaisesRegex(AsrSubmitRejected, '没有极速版权限.*标准版'):
                fast.transcribe_fast(self.path, self.config, self.request_id)
        self.assertEqual(transport.call_count, 1)

    def test_english_disables_flash_speaker_and_preserves_words(self):
        value = {'text': 'Hello', 'utterances': [{'text': 'Hello', 'start_time': 0, 'end_time': 100,
                'speaker_id': '2', 'words': [{'text': 'Hello', 'start_time': 0, 'end_time': 100}]}]}
        with patch.object(fast, '_asr_post', return_value=self.response(body={'result': [value]})) as transport:
            result = fast.transcribe_fast(self.path, self.config, self.request_id, 'en')
        self.assertEqual(transport.call_args.args[1]['audio']['language'], 'en-US')
        self.assertFalse(transport.call_args.args[1]['request']['enable_speaker_info'])
        self.assertEqual(result['utterances'][0]['additions']['speaker'], '2')
        self.assertEqual(result['utterances'][0]['words'], value['utterances'][0]['words'])

    def test_silence_is_success(self):
        with patch.object(fast, '_asr_post', return_value=self.response('20000003')):
            self.assertTrue(fast.transcribe_fast(self.path, self.config, self.request_id)['silent'])

    def test_limits_are_local_and_do_not_call_network(self):
        with patch.object(fast, '_asr_post') as transport:
            with patch.object(fast, 'FLASH_MAX_BYTES', 10), self.assertRaisesRegex(ProviderError, '20 MB'):
                fast.transcribe_fast(self.path, self.config, self.request_id)
            for seconds in (0, 7200.01, float('nan')):
                with patch.object(fast, '_audio_seconds', return_value=seconds), self.assertRaises(ProviderError):
                    fast.transcribe_fast(self.path, self.config, self.request_id)
            with self.assertRaises(ProviderError):
                fast.transcribe_fast(self.path.with_suffix('.flac'), self.config, self.request_id)
            with self.assertRaises(ProviderError):
                fast.transcribe_fast(self.path, {}, self.request_id)
            with self.assertRaises(ProviderError):
                fast.transcribe_fast(self.path, self.config, 'bad\nrequest')
            transport.assert_not_called()

    def test_mp3_duration_is_validated_locally(self):
        path = self.path.with_suffix('.mp3')
        path.write_bytes(b'mock mp3, never sent')
        with patch.object(fast.shutil, 'which', return_value='mock-ffprobe'), patch.object(fast.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=b'{"format":{"duration":"60"}}')) as probe, patch.object(fast, '_asr_post', return_value=self.response()) as transport:
            fast.transcribe_fast(path, self.config, self.request_id)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(transport.call_args.args[1]['audio']['format'], 'mp3')

    def test_unknown_outcome_is_redacted_and_never_retried(self):
        with patch.object(fast, '_asr_post', side_effect=TimeoutError('mock-only-key private-url')) as transport:
            with self.assertRaises(AsrSubmitUncertain) as caught:
                fast.transcribe_fast(self.path, self.config, self.request_id)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(caught.exception.request_id, self.request_id)
        self.assertNotIn('mock-only-key', str(caught.exception))
        self.assertIn('不能查询恢复', str(caught.exception))

    def test_error_response_classification_and_invalid_result(self):
        uncertain = [self.response(status=503), self.response('55000031'),
                     self.response('45000001', message='duplicate request secret'),
                     self.response(body={'result': {}}), (200, {}, None)]
        for response in uncertain:
            with self.subTest(response=response), patch.object(fast, '_asr_post', return_value=response) as transport:
                with self.assertRaises(AsrSubmitUncertain):
                    fast.transcribe_fast(self.path, self.config, self.request_id)
                self.assertEqual(transport.call_count, 1)
        with patch.object(fast, '_asr_post', return_value=self.response('45000151', message='mock-only-key')):
            with self.assertRaises(AsrSubmitRejected) as caught:
                fast.transcribe_fast(self.path, self.config, self.request_id)
        self.assertNotIn('mock-only-key', str(caught.exception))


class ProtocolTests(unittest.TestCase):
    def test_client_sequence_audio_and_gzip_flags(self):
        first = fast._encode_packet(b'{"audio":{}}', 1)
        self.assertEqual(first[:4], b'\x11\x11\x11\x00')
        self.assertEqual(client_packet(first), (1, 1, 1, b'{"audio":{}}'))
        last = fast._encode_packet(b'\0\0', 3, audio=True, final=True)
        self.assertEqual(last[:4], b'\x11\x23\x01\x00')
        self.assertEqual(client_packet(last), (2, 3, -3, b'\0\0'))

    def test_server_final_event_and_error(self):
        decoded = fast._decode_packet(server_packet({'result': {'text': '字'}}, final=True, sequence=2, event=7))
        self.assertEqual(decoded['sequence'], -2)
        self.assertTrue(decoded['final'])
        self.assertEqual(decoded['event'], 7)
        self.assertEqual(decoded['payload']['result']['text'], '字')
        error = fast._decode_packet(server_packet({'message': 'mock secret'}, code=45000151, compressed=False))
        self.assertEqual(error['code'], 45000151)
        self.assertEqual(error['payload'], {})

    def test_malformed_frames_are_not_partial_results(self):
        valid = server_packet({'result': {'text': 'test'}})
        for raw in (b'', 'text', valid[:-1], valid + b'\0', b'\x21' + valid[1:], valid[:2] + b'\x12' + valid[3:]):
            with self.subTest(raw=str(raw)[:30]), self.assertRaises(ValueError):
                fast._decode_packet(raw)


class StreamTests(unittest.TestCase):
    config = {'asrApiKey': 'mock-only-key', 'asrResourceId': 'volc.seedasr.auc'}

    def make(self, socket=None, callback=None, language='auto'):
        socket = socket or FakeSocket()
        patcher = patch.object(fast, 'connect', return_value=socket)
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        session = fast.StreamSession(self.config, 'mock-stream-id', callback, language)
        def cleanup():
            session.close()
            for thread in session._threads:
                thread.join(1)
        self.addCleanup(cleanup)
        return session, socket

    def test_paced_audio_full_snapshot_and_final_negative_packet(self):
        events = []
        session, socket = self.make(callback=events.append, language='en')
        session.start()
        session.feed(bytes(fast.PACKET_BYTES * 3))
        result = session.stop(timeout=2)
        self.assertEqual(session.state, 'completed')
        self.assertEqual(result['text'], '机器人。Hello.')
        self.assertEqual(result['utterances'][0]['additions']['speaker'], '1')
        self.assertTrue(events[-1]['final'])
        self.assertEqual(events[-1]['request_id'], 'mock-stream-id')
        initial = json.loads(client_packet(socket.sent[0])[3])
        self.assertNotIn('language', initial['audio'])
        self.assertEqual(initial['request']['ssd_version'], '200')
        self.assertTrue(initial['request']['enable_nonstream'])
        self.assertEqual(initial['request']['result_type'], 'full')
        self.assertEqual(self.connect.call_args.kwargs['additional_headers']['X-Api-Resource-Id'], fast.STREAM_RESOURCE_ID)
        audio_packets = [client_packet(value) for value in socket.sent[1:]]
        self.assertEqual(b''.join(item[3] for item in audio_packets), bytes(fast.PACKET_BYTES * 3))
        self.assertEqual([abs(item[2]) for item in audio_packets], list(range(2, len(audio_packets) + 2)))
        self.assertEqual(audio_packets[-1][1], 3)
        self.assertLess(audio_packets[-1][2], 0)
        self.assertGreaterEqual(socket.send_times[3] - socket.send_times[1], .36)
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(session.stop(timeout=1), result)  # Idempotent stop cannot send another billable attempt.

    def test_interim_revisions_replace_snapshot_without_duplicate_text(self):
        changed = threading.Event()
        events = []
        def callback(event):
            events.append(event)
            if event['result']['text'] == '修正。':
                changed.set()
        session, socket = self.make(callback=callback)
        session.start()
        socket.responses.put(server_packet({'result': {'text': '错误', 'utterances': [{'text': '错误', 'definite': False}]}}))
        socket.responses.put(server_packet({'result': {'text': '修正。', 'utterances': [{'text': '修正。', 'definite': True}]}}))
        self.assertTrue(changed.wait(1))
        self.assertEqual(session.last_result['text'], '修正。')
        self.assertEqual(len(events), 2)
        self.assertFalse(events[-1]['final'])

    def test_stop_flushes_subpacket_pcm(self):
        session, socket = self.make()
        session.start()
        session.feed(b'\x01\x02' * 99)
        session.stop(timeout=1)
        self.assertEqual(client_packet(socket.sent[-1]), (2, 3, -2, b'\x01\x02' * 99))

    def test_buffer_overflow_terminates_without_reconnect(self):
        session, socket = self.make()
        session.start()
        with self.assertRaisesRegex(AsrSubmitUncertain, '积压超过 10 秒'):
            session.feed(bytes(fast.MAX_BUFFER_BYTES + 2))
        self.assertEqual(session.state, 'failed')
        self.assertTrue(socket.closed)
        self.assertEqual(self.connect.call_count, 1)
        self.assertEqual(len(socket.sent), 1)

    def test_final_timeout_preserves_snapshot_and_does_not_retry(self):
        session, socket = self.make(FakeSocket(final=False))
        session.start()
        session.feed(bytes(100))
        with self.assertRaisesRegex(AsrSubmitUncertain, '最终结果超时'):
            session.stop(timeout=.05)
        self.assertEqual(session.state, 'failed')
        self.assertEqual(self.connect.call_count, 1)
        self.assertTrue(socket.closed)

    def test_unexpected_server_final_is_failure(self):
        session, socket = self.make()
        session.start()
        socket.responses.put(server_packet({'result': socket.finish_result}, final=True))
        self.assertTrue(session._done.wait(1))
        self.assertEqual(session.state, 'failed')
        self.assertIsInstance(session.error, AsrSubmitUncertain)

    def test_secret_server_error_and_handshake_rejection(self):
        socket = FakeSocket(initial=server_packet({'message': 'mock-only-key'}, code=45000001))
        session, socket = self.make(socket)
        with self.assertRaises(AsrSubmitRejected) as caught:
            session.start()
        self.assertNotIn('mock-only-key', str(caught.exception))
        self.assertEqual(caught.exception.code, '45000001')
        self.assertEqual(self.connect.call_count, 1)

    def test_close_and_invalid_pcm_never_send_more_audio(self):
        session, socket = self.make()
        session.start()
        with self.assertRaises(ProviderError):
            session.feed(b'\0')
        session.close()
        with self.assertRaises(ProviderError):
            session.feed(bytes(200))
        with self.assertRaises(ProviderError):
            session.start()
        self.assertEqual(len(socket.sent), 1)
        self.assertEqual(session.state, 'closed')

    def test_missing_final_result_is_not_success_after_audio(self):
        session, socket = self.make(FakeSocket(final=False))
        session.start()
        session.feed(bytes(100))
        def finish_without_result():
            deadline = time.monotonic() + 1
            while not session._final_sent and time.monotonic() < deadline:
                time.sleep(.001)
            socket.responses.put(server_packet({}, final=True))
        thread = threading.Thread(target=finish_without_result)
        thread.start()
        with self.assertRaisesRegex(AsrSubmitUncertain, '没有最终识别结果'):
            session.stop(timeout=1)
        thread.join(1)
        self.assertEqual(session.state, 'failed')

    def test_callback_failure_keeps_received_text_but_does_not_claim_completion(self):
        def broken_callback(event):
            raise OSError('private path and mock-only-key')
        session, socket = self.make(callback=broken_callback)
        session.start()
        session.feed(bytes(100))
        with self.assertRaises(AsrSubmitUncertain) as caught:
            session.stop(timeout=1)
        self.assertEqual(session.last_result['text'], '机器人。Hello.')
        self.assertNotIn('mock-only-key', str(caught.exception))
        self.assertEqual(session.state, 'failed')

    def test_connection_timeout_is_one_attempt_and_redacted(self):
        with patch.object(fast, 'connect', side_effect=TimeoutError('mock-only-key')) as connection:
            session = fast.StreamSession(self.config, 'timeout-request')
            with self.assertRaises(AsrSubmitUncertain) as caught:
                session.start()
        self.assertEqual(connection.call_count, 1)
        self.assertNotIn('mock-only-key', str(caught.exception))
        self.assertEqual(session.state, 'failed')

    def test_transport_disconnect_preserves_last_snapshot_without_reconnect(self):
        observed = threading.Event()
        session, socket = self.make(callback=lambda event: observed.set())
        session.start()
        socket.responses.put(server_packet({'result': {'text': '已保存。', 'utterances': []}}))
        self.assertTrue(observed.wait(1))
        socket.responses.put(None)
        self.assertTrue(session._done.wait(1))
        self.assertEqual(session.last_result['text'], '已保存。')
        self.assertEqual(session.state, 'failed')
        self.assertEqual(self.connect.call_count, 1)


if __name__ == '__main__':
    unittest.main()
