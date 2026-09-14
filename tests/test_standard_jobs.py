"""Durable standard-ASR state machine using isolated data and mocked providers."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tingji import jobs, object_storage, providers, standard_jobs as standard, storage, timetable

CONFIG = {'asrApiKey': 'mock-asr-key', 'deepseekApiKey': 'mock-ds-key',
          'tosRegion': 'cn-beijing', 'tosBucket': 'mock-private-bucket',
          'tosAccessKeyId': 'mock-ak', 'tosSecretAccessKey': 'mock-sk', 'tosPrivateConfirmed': True}
CONSENT = {'cloudUploadConsent': True, 'autoSummarize': False}
RESULT = {'state': 'completed', 'code': '20000000', 'result': {'utterances': [
    {'text': '控制周期 10 ms。', 'start_time': 0, 'end_time': 1000, 'additions': {'speaker': '1'}},
    {'text': '最后结论：电流限幅 1.5 A。', 'start_time': 3500000, 'end_time': 3503000, 'additions': {'speaker': '1'}}]}}


class StandardJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-standard-test-')
        self.root = Path(self.temp.name)
        self.patchers = [patch.object(storage, 'DATA', self.root), patch.object(storage, 'DB', self.root / 'notes.sqlite3')]
        for item in self.patchers: item.start()
        storage.initialize()
        self.original = self.root / 'audio' / 'original.wav'
        self.original.write_bytes(b'original-must-survive')
        self.prepared = self.root / 'audio' / 'prepared.mp3'
        self.prepared.write_bytes(b'prepared-test-audio')
        self.addCleanup(self.finish)
        self.mock_settings = self.replace(storage, 'settings', return_value=dict(CONFIG))
        self.mock_prepare = self.replace(standard, 'prepare_audio', return_value=self.prepared)
        self.mock_upload = self.replace(object_storage, 'prepare_and_upload', return_value={
            'objectKey': 'tingji/private-object', 'downloadUrl': 'https://mock.invalid/audio?X-Tos-Signature=do-not-expose',
            'expiresAt': standard.future(86400), 'sha256': 'mock-sha256'})
        self.mock_submit = self.replace(providers, 'submit_transcription', return_value={'code': '20000000'})
        self.mock_query = self.replace(providers, 'query_transcription', return_value={'state': 'queued', 'code': '20000002'})
        self.mock_summary = self.replace(providers, 'summarize', return_value='## 测试提炼\n\n电流限制 1.5 A。')
        self.executor = self.replace(jobs.POOL, 'submit', side_effect=lambda fn, *args: fn(*args))

    def replace(self, target, name, **kwargs):
        item = patch.object(target, name, **kwargs)
        self.patchers.append(item)
        return item.start()

    def finish(self):
        jobs.ACTIVE.clear()
        for item in reversed(self.patchers): item.stop()
        self.temp.cleanup()

    def note(self, task=None, status='idle'):
        extra = {'audioFile': self.original.name, 'summary': '已有总结', 'duration': 3503}
        if task is not None: extra.update(asrTask=task, asrComplete=False)
        return storage.create_note('已有录音', '已有原稿', status=status, **extra)

    def task_note(self, state='accepted', status='transcribing'):
        return self.note({'attemptId': 'attempt-one', 'state': state, 'requestId': 'original-request-id',
                          'queryId': 'original-query-id', 'nextCheckAt': storage.now(), 'monitorUntil': standard.future(86400)}, status)

    def test_request_id_is_committed_before_submit_and_signed_url_is_not_persisted(self):
        note = self.note()
        def submit_hook(url, config, request_id, *args, **kwargs):
            with closing(sqlite3.connect(storage.DB)) as db:
                body = json.loads(db.execute('SELECT body FROM notes WHERE id=?', (note['id'],)).fetchone()[0])
            self.assertEqual(body['asrTask']['requestId'], request_id)
            self.assertEqual(body['asrTask']['state'], 'submitting')
            self.assertNotIn('downloadUrl', json.dumps(body))
            self.assertNotIn('X-Tos-Signature', json.dumps(body))
            return {'code': '20000000'}
        self.mock_submit.side_effect = submit_hook
        standard.start(note['id'], 'transcribe', CONSENT)
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['asrTask']['state'], 'accepted')
        self.assertTrue(saved['asrTask']['requestId'])
        self.assertEqual(self.mock_submit.call_count, 1)

    def test_submit_derives_hotwords_from_timetable_with_provider_limit(self):
        note = self.note()
        storage.update_note(note['id'], courseId='robotics')
        courses = [{'id': 'math', 'name': '高等数学', 'keywords': ['微积分']},
                   {'id': 'robotics', 'name': '机器人建模与控制',
                    'keywords': ['正运动学', '正运动学'] + [f'术语{i}' for i in range(120)]}]
        original_config = {**CONFIG, 'hotwords': '过时的手工词汇'}
        self.mock_settings.return_value = original_config
        with patch.object(timetable, 'load', return_value={'courses': courses}):
            standard.start(note['id'], 'transcribe', CONSENT)
        sent = self.mock_submit.call_args.args[1]
        words = providers.hotword_list(sent['hotwords'])
        self.assertEqual(words[:3], ['机器人建模与控制', '高等数学', '正运动学'])
        self.assertEqual(len(words), 100)
        self.assertNotIn('过时的手工词汇', words)
        self.assertEqual(original_config['hotwords'], '过时的手工词汇')

    def test_unknown_submission_and_5xx_only_query_the_original_request(self):
        for status in (None, 503):
            with self.subTest(http_status=status):
                note = self.note()
                self.mock_submit.reset_mock()
                self.mock_upload.reset_mock()
                self.mock_query.reset_mock()
                self.mock_submit.side_effect = providers.AsrSubmitUncertain('mock uncertain', http_status=status)
                standard.start(note['id'], 'transcribe', CONSENT)
                task = storage.get_note(note['id'])['asrTask']
                self.assertEqual(task['state'], 'submit_unknown')
                standard.request_query(note['id'])
                with self.assertRaises(ValueError):
                    standard.start(note['id'], 'transcribe', CONSENT)
                self.assertEqual(self.mock_submit.call_count, 1)
                self.assertEqual(self.mock_upload.call_count, 1)
                self.assertEqual(self.mock_query.call_args.args[1], task['requestId'])

    def test_queued_processing_completed_persists_full_result_before_summary(self):
        note = self.note()
        def summarize_after_result(text, *args):
            saved = storage.get_note(note['id'])
            result_path = self.root / 'jobs' / note['id'] / ('standard-' + saved['asrTask']['attemptId']) / 'result.json'
            persisted = json.loads(result_path.read_text(encoding='utf-8'))
            self.assertEqual(persisted['response']['result'], RESULT['result'])
            self.assertTrue(persisted['complete'])
            self.assertEqual(persisted['requestId'], saved['asrTask']['requestId'])
            self.assertIn('最后结论：电流限幅 1.5 A。', text)
            return '## 测试提炼\n\n电流限制 1.5 A。'
        self.mock_summary.side_effect = summarize_after_result
        standard.start(note['id'], 'transcribe', {**CONSENT, 'autoSummarize': True})
        self.mock_query.side_effect = [{'state': 'queued'}, {'state': 'processing'}, RESULT]
        standard.query_once(note['id'])
        self.assertEqual(storage.get_note(note['id'])['asrTask']['state'], 'queued')
        self.assertFalse(self.mock_summary.called)
        standard.query_once(note['id'])
        self.assertEqual(storage.get_note(note['id'])['asrTask']['state'], 'processing')
        self.assertFalse(self.mock_summary.called)
        standard.query_once(note['id'])
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['asrTask']['state'], 'completed')
        self.assertEqual(saved['status'], 'ready')
        self.assertTrue(saved['asrComplete'])
        self.assertEqual(saved['transcript'], '')
        self.assertTrue(saved['transcriptPurgedAt'])
        self.assertTrue(saved['audioArchivedAt'])
        self.assertIn('1.5 A', saved['summary'])
        result_path = self.root / 'jobs' / note['id'] / ('standard-' + saved['asrTask']['attemptId']) / 'result.json'
        self.assertFalse(result_path.exists())
        self.assertEqual(self.mock_upload.call_count, 1)
        self.assertEqual(self.mock_submit.call_count, 1)
        self.assertEqual(self.original.read_bytes(), b'original-must-survive')

    def test_restart_recovers_submitting_as_unknown_then_scheduler_queries_same_id(self):
        note = self.task_note('submitting')
        storage.initialize()
        recovered = storage.get_note(note['id'])
        self.assertEqual(recovered['asrTask']['state'], 'submit_unknown')
        self.assertEqual(recovered['status'], 'transcribing')
        standard.run_due_queries()
        self.assertEqual(self.mock_query.call_args.args[1], 'original-query-id')
        self.assertFalse(self.mock_submit.called)
        self.assertFalse(self.mock_upload.called)
        self.assertEqual(storage.get_note(note['id'])['transcript'], '已有原稿')

    def test_three_query_errors_pause_and_manual_continue_only_queries(self):
        note = self.task_note()
        self.mock_query.side_effect = providers.AsrQueryError('mock temporarily unavailable', retryable=True)
        for count in range(1, 4):
            standard.query_once(note['id'])
            saved = storage.get_note(note['id'])
            self.assertEqual(saved['asrTask']['errorCount'], count)
            self.assertEqual(saved['asrTask']['state'], 'paused' if count == 3 else 'waiting_query')
        self.assertIsNone(saved['asrTask']['nextCheckAt'])
        self.mock_query.side_effect = None
        self.mock_query.return_value = {'state': 'processing'}
        standard.request_query(note['id'])
        self.assertEqual(storage.get_note(note['id'])['asrTask']['state'], 'processing')
        self.assertEqual(self.mock_query.call_args.args[1], 'original-query-id')
        self.assertFalse(self.mock_upload.called)
        self.assertFalse(self.mock_submit.called)

    def test_nonretryable_query_error_pauses_immediately(self):
        note = self.task_note()
        self.mock_query.side_effect = providers.AsrQueryError('mock auth rejected', retryable=False)
        standard.query_once(note['id'])
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['asrTask']['state'], 'paused')
        self.assertEqual(saved['asrTask']['errorCount'], 1)
        self.assertIsNone(saved['asrTask']['nextCheckAt'])

    def test_legacy_transcribe_action_never_creates_another_task_for_existing_id(self):
        for state in ('submit_unknown', 'queued', 'paused', 'not_found', 'completed', 'failed', 'rejected'):
            with self.subTest(state=state):
                note = self.task_note(state, 'error')
                with self.assertRaises(ValueError):
                    jobs.start(note['id'], 'transcribe', CONSENT)
                self.assertEqual(storage.get_note(note['id'])['asrTask']['requestId'], 'original-request-id')
        self.assertFalse(self.mock_upload.called)
        self.assertFalse(self.mock_submit.called)

    def test_missing_consent_private_confirmation_or_credentials_never_uploads(self):
        cases = [({}, CONFIG), (CONSENT, {**CONFIG, 'tosPrivateConfirmed': False}),
                 (CONSENT, {**CONFIG, 'asrApiKey': ''}), (CONSENT, {**CONFIG, 'tosSecretAccessKey': ''})]
        for options, config in cases:
            note = self.note()
            self.mock_settings.return_value = config
            with self.assertRaises(ValueError):
                standard.start(note['id'], 'transcribe', options)
            self.assertEqual(storage.get_note(note['id'])['transcript'], '已有原稿')
        self.assertFalse(self.mock_upload.called)
        self.assertFalse(self.mock_submit.called)

    def test_explicit_replacement_requires_matching_terminal_request_id(self):
        note = self.task_note('completed', 'ready')
        with self.assertRaises(ValueError):
            standard.start(note['id'], 'transcribe', {**CONSENT, 'resubmit': True, 'replaceRequestId': 'outdated-id'})
        self.assertFalse(self.mock_submit.called)
        standard.start(note['id'], 'transcribe', {**CONSENT, 'resubmit': True, 'replaceRequestId': 'original-request-id'})
        saved = storage.get_note(note['id'])
        self.assertEqual(self.mock_submit.call_count, 1)
        self.assertNotEqual(saved['asrTask']['requestId'], 'original-request-id')
        self.assertEqual(saved['asrTaskHistory'][-1]['requestId'], 'original-request-id')
        self.assertEqual(saved['transcript'], '已有原稿')

    def test_waiting_task_blocks_summary_and_chat_even_between_queries(self):
        note = self.task_note('queued')
        self.assertNotIn(note['id'], jobs.ACTIVE)
        with self.assertRaises(ValueError): jobs.start(note['id'], 'summarize')
        with self.assertRaises(ValueError): jobs.chat(note['id'], '总结结论')
        self.assertFalse(self.mock_summary.called)

    def test_failure_and_silent_completion_preserve_existing_audio_and_words(self):
        for response in ({'state': 'failed', 'message': 'mock service failure'},
                         {'state': 'completed', 'result': {'text': '', 'utterances': []}}):
            note = self.task_note()
            self.mock_query.return_value = response
            standard.query_once(note['id'])
            saved = storage.get_note(note['id'])
            self.assertEqual(saved['transcript'], '已有原稿')
            self.assertEqual(saved['summary'], '已有总结')
            self.assertEqual(saved['status'], 'error')
            self.assertEqual(self.original.read_bytes(), b'original-must-survive')

    def test_public_note_and_list_hide_object_metadata_signed_url_and_task_history(self):
        note = self.task_note('paused', 'error')
        hidden = {'downloadUrl': 'https://private.invalid?X-Tos-Signature=hidden', 'cloudObject': {'objectKey': 'do-not-expose'},
                  'tosAccessKeyId': 'private-ak', 'tosSecretAccessKey': 'private-sk'}
        task = {**note['asrTask'], **hidden}
        note = storage.update_note(note['id'], asrTask=task, asrTaskHistory=[task])
        for public in (storage.public_note(note), storage.list_notes()):
            serialized = json.dumps(public)
            for sensitive in ('downloadUrl', 'X-Tos-Signature', 'cloudObject', 'asrTaskHistory', 'private-ak', 'private-sk', 'do-not-expose'):
                self.assertNotIn(sensitive, serialized)

    def cache_path(self, note):
        folder = self.root / 'jobs' / note['id'] / ('standard-' + note['asrTask']['attemptId'])
        folder.mkdir(parents=True, exist_ok=True)
        return folder / 'result.json'

    def test_complete_cache_restores_result_without_query_submit_or_upload(self):
        note = self.task_note('submit_unknown')
        envelope = {'requestId': 'original-request-id', 'queryId': 'original-query-id', 'complete': True, 'response': RESULT}
        self.cache_path(note).write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')
        storage.initialize()
        standard.run_due_queries()
        restored = storage.get_note(note['id'])
        self.assertEqual(restored['asrTask']['state'], 'completed')
        self.assertEqual(restored['status'], 'ready')
        self.assertTrue(restored['asrComplete'])
        self.assertIn('最后结论：电流限幅 1.5 A。', restored['transcript'])
        self.assertFalse(self.mock_query.called)
        self.assertFalse(self.mock_submit.called)
        self.assertFalse(self.mock_upload.called)

    def test_completed_asr_continues_waiting_summary_without_upload_or_query(self):
        note = self.task_note('completed', 'error')
        storage.update_note(note['id'], asrComplete=True, summary='',
                            asrTask={**note['asrTask'], 'summaryState': 'waiting', 'autoSummarize': True})
        standard.run_due_queries()
        standard.run_due_queries()
        self.mock_summary.assert_called_once()
        self.assertEqual(storage.get_note(note['id'])['asrTask']['summaryState'], 'completed')
        self.assertFalse(self.mock_query.called)
        self.assertFalse(self.mock_submit.called)
        self.assertFalse(self.mock_upload.called)

    def test_dispatched_or_failed_summary_is_never_automatically_repeated(self):
        for state in ('running', 'failed'):
            note = self.task_note('completed', 'error')
            storage.update_note(note['id'], asrComplete=True, summary='',
                                asrTask={**note['asrTask'], 'summaryState': state, 'autoSummarize': True})
        standard.run_due_queries()
        self.assertFalse(self.mock_summary.called)
        self.assertFalse(self.mock_submit.called)

    def test_completed_asr_waits_for_summary_configuration_then_continues(self):
        note = self.task_note()
        storage.update_note(note['id'], summary='', asrTask={**note['asrTask'], 'autoSummarize': True})
        self.mock_settings.return_value = {**CONFIG, 'deepseekApiKey': ''}
        self.mock_query.return_value = RESULT
        standard.query_once(note['id'])
        standard.run_due_queries()
        self.assertEqual(storage.get_note(note['id'])['asrTask']['summaryState'], 'waiting')
        self.assertFalse(self.mock_summary.called)
        self.mock_settings.return_value = dict(CONFIG)
        standard.run_due_queries()
        self.mock_summary.assert_called_once()
        self.assertEqual(self.mock_query.call_count, 1)
        self.assertFalse(self.mock_submit.called)

    def test_invalid_or_mismatched_cache_only_queries_original_task(self):
        envelope = {'requestId': 'original-request-id', 'queryId': 'original-query-id', 'complete': True, 'response': RESULT}
        fixtures = [
            '{broken-json', json.dumps([]),
            json.dumps({**envelope, 'requestId': 'different-request'}),
            json.dumps({**envelope, 'queryId': 'different-query'}),
            json.dumps({**envelope, 'complete': False}),
            json.dumps({**envelope, 'response': {'state': 'processing', 'result': RESULT['result']}}),
            json.dumps({**envelope, 'response': {'state': 'completed', 'result': None}}),
        ]
        for index, content in enumerate(fixtures):
            with self.subTest(fixture=index):
                note = self.task_note()
                self.mock_query.reset_mock()
                self.cache_path(note).write_text(content, encoding='utf-8')
                standard.query_once(note['id'])
                self.assertEqual(self.mock_query.call_count, 1)
                self.assertEqual(self.mock_query.call_args.args[1], 'original-query-id')
                self.assertEqual(storage.get_note(note['id'])['transcript'], '已有原稿')
        self.assertFalse(self.mock_submit.called)
        self.assertFalse(self.mock_upload.called)


if __name__ == '__main__':
    unittest.main()
