"""Real temporary SQLite/cache files; local FFmpeg and all billable APIs are mocks."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tingji import fast_jobs, fast_providers, jobs, storage
from tingji.providers import ProviderError, AsrSubmitUncertain, AsrSubmitRejected


class FastJobsTests(unittest.TestCase):
    config = {'asrApiKey': 'mock-only-key', 'hotwords': 'PID'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in (patch.object(storage, 'DATA', self.root),
                       patch.object(storage, 'DB', self.root / 'notes.sqlite3'),
                       patch.object(jobs, 'FFMPEG', 'mock-ffmpeg'),
                       patch.object(jobs, 'FFPROBE', 'mock-ffprobe')):
            target.start()
            self.addCleanup(target.stop)
        storage.initialize()
        self.duration = patch.object(jobs, 'duration', return_value=60).start()
        self.addCleanup(patch.stopall)
        self.media = patch.object(jobs, 'run_media', side_effect=self.convert).start()
        self.transcribe = patch.object(fast_providers, 'transcribe_fast', side_effect=self.result).start()
        self.summarize = patch.object(jobs, 'summarize', side_effect=self.summarized).start()
        (self.root / 'audio' / 'source.wav').write_bytes(b'original audio must remain unchanged')
        self.note = storage.create_note('课程', audioFile='source.wav', language='auto', template='lecture')
        self.note_id = self.note['id']

    def convert(self, args, timeout):
        self.assertEqual(args[0], 'mock-ffmpeg')
        self.assertEqual(args[args.index('-ac') + 1], '1')
        self.assertEqual(args[args.index('-ar') + 1], '16000')
        self.assertEqual(args[args.index('-b:a') + 1], '64k')
        output = Path(args[-1])
        output.write_bytes(('mock mp3 ' + args[args.index('-ss') + 1] + ':' + args[args.index('-t') + 1]).encode())
        return b''

    def result(self, path, config, request_id, language):
        note = storage.get_note(self.note_id)
        chunk = next(c for c in note['fastTask']['chunks'] if c.get('requestId') == request_id)
        self.assertEqual(chunk['state'], 'submitting')  # Durable before any billable call.
        self.assertTrue(chunk.get('audioHash'))
        self.assertEqual(path.suffix, '.mp3')
        index = chunk['index']
        return {'text': f'内容{index}', 'utterances': [{'text': f'内容{index}', 'start_time': 0,
                  'end_time': 1000, 'additions': {'speaker': '1'}}]}

    def summarized(self, note_id, config, template, language):
        note = storage.get_note(note_id)
        self.assertTrue(note['asrComplete'])
        self.assertEqual(note['fastTask']['state'], 'completed')
        return storage.update_note(note_id, summary='本地模拟提炼', status='ready')

    def run_job(self, **options):
        return fast_jobs.start_worker(self.note_id, self.config, **options)

    def test_single_chunk_completes_with_durable_id_cache_and_original(self):
        result = self.run_job()
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['asrComplete'])
        self.assertEqual(result['fastTask']['completedChunks'], 1)
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertEqual((self.root / 'audio' / 'source.wav').read_bytes(), b'original audio must remain unchanged')
        task = result['fastTask']
        folder = fast_jobs._folder(self.note_id, task)
        cached = json.loads(fast_jobs._cache_path(folder, task['chunks'][0]).read_text(encoding='utf-8'))
        self.assertTrue(cached['complete'])
        self.assertEqual(cached['binding']['requestId'], task['chunks'][0]['requestId'])
        self.assertTrue((folder / 'original-text.json').is_file())
        self.summarize.assert_not_called()

    def test_long_recording_is_split_under_both_limits_with_local_speaker_ids(self):
        self.duration.return_value = 4010
        result = self.run_job()
        chunks = result['fastTask']['chunks']
        self.assertEqual([(c['start'], c['duration']) for c in chunks], [(0, 2000), (1998, 2002), (3998, 12)])
        self.assertEqual(result['fastTask']['plannedBillableSeconds'], 4014)
        self.assertEqual(self.transcribe.call_count, 3)
        self.assertEqual([s['start'] for s in result['segments']], [0, 1998, 3998])
        self.assertEqual([s['speaker'] for s in result['segments']], ['第 1 段 · 说话人 1', '第 2 段 · 说话人 1', '第 3 段 · 说话人 1'])

    def test_overlap_duplicate_is_removed_without_deleting_repeated_new_speech(self):
        self.duration.return_value = 2010
        self.transcribe.side_effect = [
            {'text': '重叠', 'utterances': [{'text': '重叠', 'start_time': 1998500, 'end_time': 1999900}]},
            {'text': '重叠 重叠', 'utterances': [{'text': '重叠', 'start_time': 500, 'end_time': 1900},
                                                 {'text': '重叠', 'start_time': 3000, 'end_time': 4000}]}]
        result = self.run_job()
        self.assertEqual(len(result['segments']), 2)
        self.assertEqual([s['start'] for s in result['segments']], [1998.5, 2001])

    def test_unknown_preserves_partial_and_requires_explicit_retry(self):
        self.duration.return_value = 2010
        original = self.result
        def fail_second(path, config, request_id, language):
            if '001' in path.stem:
                raise AsrSubmitUncertain('可能已收费，请手动决定', request_id=request_id)
            return original(path, config, request_id, language)
        self.transcribe.side_effect = fail_second
        first = self.run_job()
        self.assertFalse(first['asrComplete'])
        self.assertIn('内容0', first['transcript'])
        self.assertEqual(first['fastTask']['state'], 'submit_unknown')
        old_id = first['fastTask']['chunks'][1]['requestId']
        first_id = first['fastTask']['chunks'][0]['requestId']
        self.transcribe.reset_mock(side_effect=True)
        stopped = self.run_job()
        self.assertEqual(stopped['status'], 'error')
        self.assertIn('重试可能再次计费', stopped['error'])
        self.transcribe.assert_not_called()
        self.transcribe.side_effect = self.result
        done = self.run_job(explicit_retry=True)
        self.assertTrue(done['asrComplete'])
        self.assertEqual(self.transcribe.call_count, 1)
        self.assertNotEqual(done['fastTask']['chunks'][1]['requestId'], old_id)
        self.assertEqual(done['fastTask']['chunks'][0]['requestId'], first_id)
        self.assertEqual(done['fastTask']['chunks'][1]['history'][0]['requestId'], old_id)

    def test_response_cache_recovers_crash_before_database_commit_without_fee(self):
        with patch.object(fast_jobs, '_publish', side_effect=OSError('mock disk interruption')):
            stopped = self.run_job()
        self.assertEqual(stopped['fastTask']['state'], 'submit_unknown')
        self.assertEqual(self.transcribe.call_count, 1)
        self.transcribe.reset_mock()
        done = self.run_job()
        self.assertTrue(done['asrComplete'])
        self.assertIn('内容0', done['transcript'])
        self.transcribe.assert_not_called()

    def test_rejected_chunk_is_not_automatically_submitted_again(self):
        self.transcribe.side_effect = AsrSubmitRejected('请开通极速服务')
        first = self.run_job()
        self.assertEqual(first['fastTask']['state'], 'rejected')
        self.assertFalse(first['fastTask']['retryMayCharge'])
        self.transcribe.reset_mock()
        self.run_job()
        self.transcribe.assert_not_called()

    def test_changed_source_and_hotwords_cannot_reuse_old_partial_job(self):
        self.transcribe.side_effect = AsrSubmitUncertain('unknown')
        self.run_job()
        self.transcribe.reset_mock()
        result = fast_jobs.start_worker(self.note_id, {**self.config, 'hotwords': 'different'}, explicit_retry=True)
        self.assertIn('设置已变化', result['error'])
        self.transcribe.assert_not_called()
        (self.root / 'audio' / 'source.wav').write_bytes(b'changed original')
        result = self.run_job(explicit_retry=True)
        self.assertIn('设置已变化', result['error'])
        self.transcribe.assert_not_called()

    def test_oversized_local_output_is_never_submitted(self):
        with patch.object(fast_providers, 'FLASH_MAX_BYTES', 1):
            result = self.run_job()
        self.assertEqual(result['fastTask']['state'], 'local_error')
        self.assertFalse(result['asrComplete'])
        self.transcribe.assert_not_called()

    def test_corrupt_completed_cache_requires_manual_retry(self):
        self.duration.return_value = 2010
        self.transcribe.side_effect = [{'text': '已完成', 'utterances': []}, AsrSubmitUncertain('unknown')]
        result = self.run_job()
        task = result['fastTask']
        fast_jobs._cache_path(fast_jobs._folder(self.note_id, task), task['chunks'][0]).write_text('{broken', encoding='utf-8')
        self.transcribe.reset_mock()
        stopped = self.run_job()
        self.assertIn('已完成', stopped['transcript'])
        self.assertEqual(stopped['fastTask']['state'], 'submit_unknown')
        self.assertTrue(stopped['fastTask']['retryMayCharge'])
        self.transcribe.assert_not_called()

    def test_silent_chunk_does_not_abort_later_speech_or_erase_existing_text(self):
        self.duration.return_value = 2010
        self.transcribe.side_effect = [{'text': '', 'utterances': [], 'silent': True}, {'text': '后段语音', 'utterances': []}]
        done = self.run_job()
        self.assertTrue(done['asrComplete'])
        self.assertIn('后段语音', done['transcript'])
        self.assertEqual(done['fastTask']['completedChunks'], 2)

    def test_all_silence_keeps_previous_document_and_skips_summary(self):
        storage.update_note(self.note_id, transcript='原先手动文稿')
        self.transcribe.return_value = {'text': '', 'utterances': [], 'silent': True}
        self.transcribe.side_effect = None
        done = fast_jobs.start_worker(self.note_id, {**self.config, 'deepseekApiKey': 'mock-ds'})
        self.assertEqual(done['transcript'], '原先手动文稿')
        self.assertFalse(done['asrComplete'])
        self.assertEqual(done['fastTask']['state'], 'completed')
        self.summarize.assert_not_called()

    def test_summary_only_after_full_asr_and_repeat_complete_is_noop(self):
        config = {**self.config, 'deepseekApiKey': 'mock-ds'}
        done = fast_jobs.start_worker(self.note_id, config)
        self.assertEqual(done['summary'], '本地模拟提炼')
        self.summarize.assert_called_once()
        self.transcribe.reset_mock()
        self.summarize.reset_mock()
        storage.update_note(self.note_id, transcript='用户后续修订', transcriptEdited=True)
        again = fast_jobs.start_worker(self.note_id, config, explicit_retry=True)
        self.assertEqual(again['transcript'], '用户后续修订')
        self.transcribe.assert_not_called()
        self.summarize.assert_not_called()

    def test_summary_failure_preserves_completed_asr(self):
        self.summarize.side_effect = ProviderError('模拟提炼失败')
        done = fast_jobs.start_worker(self.note_id, {**self.config, 'deepseekApiKey': 'mock-ds'})
        self.assertTrue(done['asrComplete'])
        self.assertEqual(done['fastTask']['state'], 'completed')
        self.assertIn('内容0', done['transcript'])
        self.assertEqual(done['stage'], '转写完成，提炼未完成')

    def test_old_standard_task_remains_untouched(self):
        task = {'requestId': 'old-standard', 'state': 'queued'}
        storage.update_note(self.note_id, asrTask=task)
        before = storage.get_note(self.note_id)
        with self.assertRaises(ProviderError):
            self.run_job(explicit_retry=True)
        self.assertEqual(storage.get_note(self.note_id), before)
        self.transcribe.assert_not_called()
        self.media.assert_not_called()

    def test_user_edits_during_interruption_are_not_overwritten(self):
        with patch.object(fast_jobs, '_publish', side_effect=OSError('disk interruption')):
            self.run_job()
        storage.update_note(self.note_id, transcript='我手动修正的文字', transcriptEdited=True)
        self.transcribe.reset_mock()
        stopped = self.run_job()
        self.assertEqual(stopped['transcript'], '我手动修正的文字')
        self.transcribe.assert_not_called()

    def test_second_worker_cannot_duplicate_active_request(self):
        entered, release = threading.Event(), threading.Event()
        original = self.result
        def blocking(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        self.transcribe.side_effect = blocking
        worker = threading.Thread(target=self.run_job)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ProviderError):
                self.run_job(explicit_retry=True)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.transcribe.call_count, 1)

    def test_resume_publishes_all_cached_chunks_together(self):
        self.duration.return_value = 4010
        self.transcribe.side_effect = [{'text': '第一段', 'utterances': []}, {'text': '第二段', 'utterances': []}, AsrSubmitUncertain('unknown')]
        initial = self.run_job()
        self.assertIn('第二段', initial['transcript'])
        updates = []
        original = storage.update_note
        def observe(note_id, **values):
            if 'transcript' in values:
                updates.append(values['transcript'])
            return original(note_id, **values)
        self.transcribe.reset_mock()
        with patch.object(storage, 'update_note', side_effect=observe):
            self.run_job()
        self.assertTrue(updates)
        self.assertTrue(all('第一段' in text and '第二段' in text for text in updates))
        self.transcribe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
