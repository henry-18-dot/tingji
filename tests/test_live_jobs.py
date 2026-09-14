"""Live-job state and disk tests; StreamSession is local and never opens a socket."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from tingji import fast_providers, jobs, live_jobs as live, storage
from tingji.providers import ProviderError

REAL_STREAM_SESSION = fast_providers.StreamSession


class FakeSession:
    def __init__(self, config, request_id, on_result=None, language='auto'):
        self.request_id = request_id
        self.callback = on_result
        self.language = language
        self.state = 'new'
        self.error = None
        self.started = 0
        self.closed = 0
        self.stopped = 0
        self.feeds = []
        self.feed_error = None
        self.start_error = None
        self.stop_error = None
        self.emit_final = True
        self.final_result = {'text': '完整最终稿。Hello.', 'utterances': [
            {'text': '完整最终稿。Hello.', 'start_time': 0, 'end_time': 20,
             'definite': True, 'additions': {'speaker': '1'}}]}
        self.saved_before_start = False
        self.saved_before_feeds = []
        self.asr_complete_during_final = None

    def current_note(self):
        return next(note for note in storage.all_notes()
                    if (note.get('liveTask') or {}).get('requestId') == self.request_id)

    def start(self):
        self.started += 1
        note = self.current_note()
        self.saved_before_start = note['liveTask']['state'] == 'connecting' and note['id'] in jobs.ACTIVE
        if self.start_error:
            raise self.start_error
        self.state = 'streaming'
        return self

    def feed(self, pcm):
        note = self.current_note()
        self.saved_before_feeds.append(live._pcm_path(note['id']).read_bytes())
        self.feeds.append(bytes(pcm))
        if self.feed_error:
            self.state = 'failed'
            self.error = self.feed_error
            raise self.feed_error

    def emit(self, text, final=False, utterances=None):
        result = {'text': text, 'utterances': utterances or []}
        self.callback({'request_id': self.request_id, 'result': result, 'final': final, 'audio_info': {}})

    def stop(self):
        self.stopped += 1
        if self.stop_error:
            self.state = 'failed'
            self.error = self.stop_error
            raise self.stop_error
        if self.emit_final:
            errors = []
            def receiver():
                try:
                    self.callback({'request_id': self.request_id, 'result': self.final_result,
                                   'final': True, 'audio_info': {}})
                    self.asr_complete_during_final = self.current_note()['asrComplete']
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=receiver, daemon=True)
            thread.start()
            thread.join(2)
            if thread.is_alive():
                raise AssertionError('provider callback is blocked by the job stop lock')
            if errors:
                raise errors[0]
        self.state = 'completed'
        return self.final_result

    def close(self):
        self.closed += 1
        self.state = 'closed' if self.state not in ('failed', 'completed') else self.state


class LiveJobTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(live.stop_watchdog)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='tingji-live-test-')))
        self.stack.enter_context(patch.object(storage, 'DATA', self.root))
        self.stack.enter_context(patch.object(storage, 'DB', self.root / 'notes.sqlite3'))
        self.stack.enter_context(patch.object(jobs, 'ACTIVE', set()))
        self.stack.enter_context(patch.object(live, '_CURRENT', None))
        self.sessions = []
        def factory(*args, **kwargs):
            session = FakeSession(*args, **kwargs)
            self.sessions.append(session)
            return session
        self.factory = self.stack.enter_context(patch.object(fast_providers, 'StreamSession', side_effect=factory))
        self.summarize = self.stack.enter_context(patch.object(jobs, 'start'))
        storage.initialize()

    def start(self, **kwargs):
        return live.start({'asrApiKey': 'fake-only-no-network'}, **kwargs)

    def feed(self, note_id, seq=0, pcm=b'\x01\x00' * 8):
        return live.feed(note_id, seq, pcm, hashlib.sha256(pcm).hexdigest())

    def wav_pcm(self, note):
        with wave.open(str(self.root / 'audio' / note['audioFile']), 'rb') as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getnchannels(), 1)
            return audio.readframes(audio.getnframes())

    def test_request_id_note_and_active_guard_exist_before_first_connection(self):
        note = self.start(language='en', template='lecture', title='Lecture')
        session = self.sessions[0]
        self.assertTrue(session.saved_before_start)
        self.assertEqual(session.language, 'en')
        self.assertEqual(note['liveTask']['requestId'], session.request_id)
        self.assertEqual(note['liveTask']['state'], 'streaming')
        self.assertEqual(note['liveTask']['lastSeq'], -1)
        self.assertFalse(note['asrComplete'])
        self.assertEqual(live._pcm_path(note['id']).read_bytes(), b'')
        with self.assertRaisesRegex(ValueError, '已有'):
            self.start()
        self.assertEqual(self.factory.call_count, 1)

    def test_each_feed_is_on_disk_before_provider_and_identical_retry_is_not_resent(self):
        note = self.start()
        first, second = b'\x01\x00' * 8, b'\x02\x00' * 3
        accepted = self.feed(note['id'], 0, first)
        duplicate = self.feed(note['id'], 0, first)
        self.feed(note['id'], 1, second)
        session = self.sessions[0]
        self.assertFalse(accepted['duplicate'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['nextSeq'], 1)
        self.assertEqual(session.feeds, [first, second])
        self.assertEqual(session.saved_before_feeds, [first, first + second])
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['liveTask']['lastSeq'], 1)
        self.assertEqual(saved['liveTask']['receivedBytes'], len(first + second))

    def test_changed_duplicate_and_sequence_gap_are_rejected_without_append_or_feed(self):
        note = self.start()
        self.feed(note['id'])
        original = live._pcm_path(note['id']).read_bytes()
        with self.assertRaisesRegex(ValueError, '不同内容'):
            self.feed(note['id'], 0, b'\x04\x00')
        with self.assertRaisesRegex(ValueError, '不连续'):
            self.feed(note['id'], 2)
        self.assertEqual(live._pcm_path(note['id']).read_bytes(), original)
        self.assertEqual(len(self.sessions[0].feeds), 1)

    def test_packet_shape_hash_and_sequence_are_checked_before_persist(self):
        note = self.start()
        for pcm in (b'', b'\x00', b'\x00' * 65538):
            with self.subTest(length=len(pcm)), self.assertRaises(ValueError):
                self.feed(note['id'], pcm=pcm)
        for seq in (-1, True, 0.5, '0'):
            with self.subTest(seq=seq), self.assertRaises(ValueError):
                self.feed(note['id'], seq)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            live.feed(note['id'], 0, b'\x00\x00', '0' * 64)
        self.assertEqual(live._pcm_path(note['id']).read_bytes(), b'')
        self.assertEqual(self.sessions[0].feeds, [])
        self.feed(note['id'], pcm=b'\x00' * 65536)
        self.assertEqual(len(self.sessions[0].feeds[0]), 65536)

    def test_full_snapshots_replace_partial_and_second_pass_text(self):
        note = self.start()
        self.feed(note['id'])
        session = self.sessions[0]
        session.emit('机器人制御。')
        session.emit('机器人控制。Hello.', utterances=[
            {'text': '机器人控制。Hello.', 'start_time': 0, 'end_time': 30,
             'definite': True, 'additions': {'speaker': '2'}}])
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['transcript'], '机器人控制。Hello.')
        self.assertNotIn('制御', saved['transcript'])
        self.assertEqual(len(saved['segments']), 1)
        self.assertTrue(saved['segments'][0]['definite'])
        self.assertEqual(saved['segments'][0]['speaker'], '说话人 2')
        self.assertFalse(saved['asrComplete'])

    def test_stop_waits_for_receiver_without_deadlock_then_seals_exact_pcm(self):
        note = self.start()
        pcm = b'\x04\x00\xf8\xff' * 16
        self.feed(note['id'], pcm=pcm)
        done = live.stop(note['id'])
        session = self.sessions[0]
        self.assertFalse(session.asr_complete_during_final)
        self.assertTrue(done['asrComplete'])
        self.assertEqual(done['liveTask']['state'], 'completed')
        self.assertEqual(done['transcript'], session.final_result['text'])
        self.assertEqual(self.wav_pcm(done), pcm)
        self.assertEqual(done['fileSize'], len(pcm) + 44)
        self.assertAlmostEqual(done['duration'], len(pcm) / 32000)
        self.assertNotIn(note['id'], jobs.ACTIVE)
        self.assertIsNone(live._CURRENT)
        again = live.stop(note['id'])
        self.assertEqual(again, done)
        self.assertEqual(session.stopped, 1)
        self.summarize.assert_not_called()

    def test_real_stream_session_protocol_and_job_persistence_with_local_fake_socket(self):
        from tests.test_fast_providers import FakeSocket, client_packet
        socket = FakeSocket()
        pcm = b'\x08\x00' * 160
        with patch.object(fast_providers, 'StreamSession', REAL_STREAM_SESSION), \
                patch.object(fast_providers, 'connect', return_value=socket) as connect:
            note = self.start()
            self.feed(note['id'], pcm=pcm)
            done = live.stop(note['id'])
        self.assertTrue(done['asrComplete'])
        self.assertEqual(done['transcript'], socket.finish_result['text'])
        self.assertEqual(self.wav_pcm(done), pcm)
        self.assertEqual(connect.call_count, 1)
        audio_packets = [client_packet(packet) for packet in socket.sent if client_packet(packet)[0] == 2]
        self.assertEqual(b''.join(packet[3] for packet in audio_packets), pcm)
        self.assertLess(audio_packets[-1][2], 0)
        self.assertEqual(jobs.ACTIVE, set())

    def test_feed_failure_preserves_pcm_and_partial_without_retry_or_secret_error(self):
        note = self.start()
        session = self.sessions[0]
        session.emit('已经识别的尾部事实')
        session.feed_error = OSError('SECRET-KEY-SHOULD-NOT-LEAK')
        pcm = b'\x05\x00' * 8
        with self.assertRaises(ProviderError) as caught:
            self.feed(note['id'], pcm=pcm)
        saved = storage.get_note(note['id'])
        self.assertNotIn('SECRET', str(caught.exception) + saved['error'])
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(saved['transcript'], '已经识别的尾部事实')
        self.assertEqual(self.wav_pcm(saved), pcm)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(len(session.feeds), 1)
        self.assertNotIn(note['id'], jobs.ACTIVE)
        with self.assertRaises(ValueError):
            self.feed(note['id'], pcm=pcm)
        self.summarize.assert_not_called()

    def test_receiver_failure_is_observed_on_next_activity_without_new_connection(self):
        note = self.start()
        self.feed(note['id'])
        session = self.sessions[0]
        session.emit('保留这段')
        session.state, session.error = 'failed', ProviderError('mock error')
        with self.assertRaises(ValueError):
            self.feed(note['id'], 1)
        saved = live.stop(note['id'])
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(saved['transcript'], '保留这段')
        self.assertEqual(len(session.feeds), 1)
        self.assertEqual(self.factory.call_count, 1)

    def test_stop_timeout_keeps_partial_and_audio_and_does_not_summarize(self):
        note = self.start()
        self.feed(note['id'])
        session = self.sessions[0]
        session.emit('部分结果')
        session.stop_error = TimeoutError('private internal endpoint')
        saved = live.stop(note['id'], summarize_after=True)
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(saved['transcript'], '部分结果')
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(session.stopped, 1)
        self.summarize.assert_not_called()

    def test_provider_completed_state_without_final_callback_is_not_complete(self):
        note = self.start()
        self.feed(note['id'])
        self.sessions[0].emit_final = False
        saved = live.stop(note['id'])
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(saved['liveTask']['state'], 'failed')

    def test_explicit_cancel_preserves_partial_and_wav_without_provider_stop(self):
        note = self.start()
        self.feed(note['id'])
        session = self.sessions[0]
        session.emit('主动结束前的文本')
        saved = live.cancel(note['id'])
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(saved['liveTask']['state'], 'cancelled')
        self.assertEqual(saved['transcript'], '主动结束前的文本')
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(session.stopped, 0)
        self.assertEqual(session.closed, 1)
        self.assertEqual(live.cancel(note['id']), saved)
        session.emit('迟到的回调不能覆盖')
        self.assertEqual(storage.get_note(note['id'])['transcript'], '主动结束前的文本')

    def test_stop_without_pcm_cancels_and_never_claims_completed(self):
        note = self.start()
        saved = live.stop(note['id'], summarize_after=True)
        self.assertEqual(saved['liveTask']['state'], 'cancelled')
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(self.wav_pcm(saved), b'')
        self.assertEqual(self.sessions[0].stopped, 0)
        self.summarize.assert_not_called()

    def test_idle_maintenance_seals_old_attempt_before_explicit_new_start(self):
        note = self.start()
        self.feed(note['id'])
        self.sessions[0].emit('闲置前原稿')
        live._CURRENT.last_feed -= live.IDLE_SECONDS + 1
        new_note = self.start()
        old = storage.get_note(note['id'])
        self.assertEqual(old['liveTask']['state'], 'interrupted')
        self.assertFalse(old['asrComplete'])
        self.assertEqual(old['transcript'], '闲置前原稿')
        self.assertTrue(self.wav_pcm(old))
        self.assertEqual(self.factory.call_count, 2)
        self.assertNotEqual(new_note['id'], old['id'])
        self.assertEqual(jobs.ACTIVE, {new_note['id']})

    def test_maintenance_tick_closes_idle_attempt_without_any_new_user_api(self):
        note = self.start()
        self.feed(note['id'])
        live._CURRENT.last_feed -= live.IDLE_SECONDS + 1
        live.maintenance_tick()
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['liveTask']['state'], 'interrupted')
        self.assertFalse(saved['asrComplete'])
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.sessions[0].stopped, 0)
        self.summarize.assert_not_called()

    def test_watchdog_background_tick_closes_failed_connection_and_start_is_idempotent(self):
        note = self.start()
        self.feed(note['id'])
        session = self.sessions[0]
        closed = threading.Event()
        original_close = session.close
        def signal_close():
            original_close()
            closed.set()
        session.close = signal_close
        session.state, session.error = 'failed', ProviderError('mock transport failure')
        worker = live.start_watchdog(interval=0.01)
        self.assertIs(live.start_watchdog(interval=0.01), worker)
        self.assertTrue(worker.daemon)
        self.assertTrue(closed.wait(1), 'watchdog did not close the failed connection')
        live.stop_watchdog()
        self.assertFalse(worker.is_alive())
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['liveTask']['state'], 'failed')
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(session.closed, 1)

    def test_watchdog_shutdown_preserves_active_recording_without_final_or_paid_retry(self):
        note = self.start()
        self.feed(note['id'])
        self.sessions[0].emit('服务器退出前的文字')
        worker = live.start_watchdog()
        live.stop_watchdog()
        saved = storage.get_note(note['id'])
        self.assertFalse(worker.is_alive())
        self.assertEqual(saved['liveTask']['state'], 'interrupted')
        self.assertEqual(saved['transcript'], '服务器退出前的文字')
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(self.sessions[0].stopped, 0)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(jobs.ACTIVE, set())

    def test_restart_recovery_preserves_pcm_and_partial_but_never_reconnects(self):
        note = self.start()
        self.feed(note['id'])
        self.sessions[0].emit('重启前的最后一段')
        live._CURRENT = None  # Model a stopped process; no provider thread exists in this test.
        jobs.ACTIVE.clear()
        storage.initialize()
        recovered = live.recover()
        self.assertEqual(len(recovered), 1)
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['transcript'], '重启前的最后一段')
        self.assertEqual(saved['liveTask']['state'], 'interrupted')
        self.assertFalse(saved['asrComplete'])
        self.assertTrue(self.wav_pcm(saved))
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(live.recover(), [])

    def test_recovery_keeps_odd_partial_sample_in_raw_source_and_seals_complete_samples(self):
        note = self.start()
        self.feed(note['id'], pcm=b'\x01\x00')
        with live._pcm_path(note['id']).open('ab') as source:
            source.write(b'\xff')
        live._CURRENT = None
        jobs.ACTIVE.clear()
        saved = live.recover()[0]
        self.assertEqual(self.wav_pcm(saved), b'\x01\x00')
        self.assertEqual(live._pcm_path(note['id']).read_bytes(), b'\x01\x00\xff')
        self.assertFalse(saved['asrComplete'])

    def test_start_failure_leaves_recoverable_note_and_releases_guard_without_raw_error(self):
        def fail(*args, **kwargs):
            session = FakeSession(*args, **kwargs)
            session.start_error = OSError('private-secret')
            self.sessions.append(session)
            return session
        self.factory.side_effect = fail
        with self.assertRaises(ProviderError) as caught:
            self.start()
        self.assertNotIn('private-secret', str(caught.exception))
        saved = storage.all_notes()[0]
        self.assertEqual(saved['liveTask']['state'], 'failed')
        self.assertFalse(saved['asrComplete'])
        self.assertEqual(jobs.ACTIVE, set())
        self.assertIsNone(live._CURRENT)
        self.assertEqual(self.factory.call_count, 1)

    def test_invalid_configuration_fails_before_note_or_network_start(self):
        self.factory.side_effect = ProviderError('missing key')
        with self.assertRaises(ProviderError):
            self.start()
        self.assertEqual(storage.all_notes(), [])
        self.assertEqual(jobs.ACTIVE, set())
        self.assertEqual(list((self.root / 'jobs').iterdir()), [])

    def test_summary_is_explicit_after_completion_and_not_repeated_by_stop_retry(self):
        note = self.start(template='meeting', language='zh')
        self.feed(note['id'])
        self.summarize.side_effect = lambda note_id, action, options: storage.get_note(note_id)
        done = live.stop(note['id'], summarize_after=True)
        self.assertTrue(done['asrComplete'])
        self.summarize.assert_called_once_with(note['id'], 'summarize', {'template': 'meeting', 'language': 'zh'})
        live.stop(note['id'], summarize_after=True)
        self.assertEqual(self.summarize.call_count, 1)

    def test_summary_configuration_error_preserves_successful_transcript_and_audio(self):
        note = self.start()
        self.feed(note['id'])
        self.summarize.side_effect = ValueError('missing DS key')
        done = live.stop(note['id'], summarize_after=True)
        self.assertTrue(done['asrComplete'])
        self.assertEqual(done['transcript'], self.sessions[0].final_result['text'])
        self.assertIn('提炼未开始', done['stage'])
        self.assertTrue(self.wav_pcm(done))

    def test_path_identifiers_rejected_without_accessing_another_directory(self):
        for invalid in ('../../outside', 'C:\\outside', 'short', '0' * 36):
            with self.subTest(identifier=invalid):
                for call in (lambda: live.feed(invalid, 0, b'\x00\x00', 'a' * 64),
                             lambda: live.stop(invalid), lambda: live.cancel(invalid)):
                    with self.assertRaises(ValueError):
                        call()
        self.assertEqual(storage.all_notes(), [])


if __name__ == '__main__':
    unittest.main()
