"""Bounded recovery helper checks with isolated files, fake time and no network."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'recover-standard.py'
SPEC = importlib.util.spec_from_file_location('tingji_recover_standard_test', SCRIPT)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
NOTE_ID = 'a7f0a7fc-f155-4a29-99ca-d1262a096b61'
CONFIG = {'asrMode': 'standard', 'asrConfigured': True,
          'tosConfigured': True, 'tosPrivateConfirmed': True,
          'deepseekConfigured': True}


def note(state=None, *, status='error', request_id=True):
    value = {'id': NOTE_ID, 'title': '测试录音', 'status': status,
             'stage': '极速版提交被拒绝', 'transcript': '', 'summary': '',
             'template': 'lecture', 'language': 'auto',
             'fastTask': {'state': 'rejected'}}
    if state:
        value['asrTask'] = {'state': state}
        if request_id:
            value['asrTask'].update(requestId='original-task', queryId='original-query')
    return value


class RecoverStandardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-recovery-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = 1000.0
        self.requests = []
        self.config = dict(CONFIG)
        self.notes = [note()]
        self.submission_error = None
        self.patches = [
            patch.object(recovery, 'ROOT', self.root),
            patch.object(recovery, 'api', side_effect=self.fake_api),
            patch.object(recovery.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(recovery.time, 'sleep', side_effect=self.advance),
            # Defense in depth: an accidentally unmocked transport cannot call out.
            patch.object(recovery.OPENER, 'open', side_effect=AssertionError('Unexpected network request')),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    @property
    def state_path(self):
        return self.root / 'data' / 'recovery' / NOTE_ID / 'standard-recovery.json'

    def saved(self):
        return json.loads(self.state_path.read_text(encoding='utf-8'))

    def advance(self, seconds):
        self.clock += seconds
        self.assertLess(self.clock, 1200, 'Recovery did not stop at its configured bound')

    def fake_api(self, path, body=None, token='', action_id=''):
        self.requests.append((path, copy.deepcopy(body), token, action_id))
        if path == '/api/bootstrap':
            return {'settings': dict(self.config), 'token': 'mock-local-token'}
        if path == '/api/notes/' + NOTE_ID:
            return copy.deepcopy(self.notes.pop(0) if len(self.notes) > 1 else self.notes[0])
        self.assertEqual(path, '/api/notes/' + NOTE_ID + '/transcribe')
        self.assertTrue(self.saved()['submissionAttempted'])
        self.assertEqual(self.saved()['phase'], 'submitting_once')
        self.assertEqual(action_id, self.saved()['actionId'])
        if self.submission_error:
            raise self.submission_error
        return {'ok': True}

    def run_helper(self, *extra):
        argv = [str(SCRIPT), '--note-id', NOTE_ID, '--execute',
                '--wait-config-seconds', '20', '--observe-seconds', '50', *extra]
        with patch('sys.argv', argv):
            return recovery.main()

    def submissions(self):
        return [request for request in self.requests if request[1] is not None]

    def test_missing_configuration_waits_without_submitting_and_exits_at_deadline(self):
        self.config.update(tosConfigured=False, tosPrivateConfirmed=False)
        self.assertEqual(self.run_helper(), 3)
        self.assertEqual(self.submissions(), [])
        self.assertFalse(self.saved()['submissionAttempted'])
        self.assertEqual(self.saved()['phase'], 'configuration_deadline')
        self.assertEqual(self.saved()['missing'], ['tosConfigured', 'tosPrivateConfirmed'])
        self.assertEqual(self.clock, 1020)

    def test_submits_once_then_observes_preparation_queue_and_processing(self):
        self.notes = [note(), note('preparing', status='transcribing', request_id=False),
                      note('accepted', status='transcribing'), note('queued', status='transcribing'),
                      note('processing', status='transcribing')]
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(len(self.submissions()), 1)
        _, body, token, _ = self.submissions()[0]
        self.assertEqual(body, {'asrMode': 'standard', 'cloudUploadConsent': True,
                              'autoSummarize': True, 'template': 'lecture', 'language': 'auto'})
        self.assertEqual(token, 'mock-local-token')
        self.assertEqual(self.saved()['phase'], 'processing_confirmed')
        self.assertNotIn('mock-local-token', self.state_path.read_text(encoding='utf-8'))

    def test_existing_task_only_observes_even_when_configuration_is_now_missing(self):
        self.config['tosConfigured'] = False
        self.notes = [note('queued', status='transcribing'), note('processing', status='transcribing')]
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.submissions(), [])
        self.assertFalse(self.saved()['submissionAttempted'])
        self.assertEqual(self.saved()['note']['standardTask']['queryId'], 'original-query')

    def test_existing_rejected_or_failed_task_never_resubmits(self):
        for state in ('rejected', 'failed', 'paused', 'not_found'):
            with self.subTest(state=state):
                self.notes = [note(state)]
                self.assertEqual(self.run_helper(), 2)
                self.assertEqual(self.saved()['phase'], 'needs_attention')
                self.assertEqual(self.submissions(), [])

    def test_unknown_submission_is_observed_without_another_post(self):
        self.submission_error = TimeoutError('mock local response lost')
        self.notes = [note(), note('submit_unknown', status='transcribing'),
                      note('queued', status='transcribing'), note('processing', status='transcribing')]
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.saved()['phase'], 'processing_confirmed')

    def test_existing_unknown_task_only_observes_original_identity(self):
        self.notes = [note('submit_unknown', status='transcribing'),
                      note('processing', status='transcribing')]
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.submissions(), [])

    def test_ambiguous_local_submission_without_task_is_not_retried_after_restart(self):
        self.submission_error = OSError('mock local server disconnected')
        self.assertEqual(self.run_helper(), 2)
        self.assertEqual(self.saved()['phase'], 'submission_needs_review')
        self.assertEqual(len(self.submissions()), 1)
        action_id = self.saved()['actionId']
        self.assertEqual(self.run_helper(), 2)
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.saved()['actionId'], action_id)

    def test_processing_exits_successfully_without_wait_or_submission(self):
        self.notes = [note('processing', status='transcribing')]
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.clock, 1000)
        self.assertEqual(self.submissions(), [])
        self.assertEqual(self.saved()['phase'], 'processing_confirmed')

    def test_asr_completion_with_failed_summary_or_silence_needs_attention(self):
        self.notes = [note('completed', status='error')]
        self.assertEqual(self.run_helper(), 2)
        self.assertEqual(self.saved()['phase'], 'needs_attention')
        self.assertEqual(self.submissions(), [])

    def test_queued_deadline_does_not_claim_processing_or_resubmit(self):
        self.notes = [note('queued', status='transcribing')]
        self.assertEqual(self.run_helper(), 3)
        self.assertEqual(self.saved()['phase'], 'observation_deadline')
        self.assertEqual(self.saved()['note']['standardTask']['state'], 'queued')
        self.assertEqual(self.submissions(), [])

    def test_three_consecutive_local_connection_failures_stop(self):
        with patch.object(recovery, 'api', side_effect=OSError('mock unavailable')) as api:
            self.assertEqual(self.run_helper(), 2)
        self.assertEqual(api.call_count, 3)
        self.assertEqual(self.saved()['phase'], 'local_connection_failed')
        self.assertFalse(self.saved()['submissionAttempted'])

    def test_unresolved_fast_submission_is_never_silently_replaced(self):
        self.notes[0]['fastTask']['state'] = 'submit_unknown'
        self.assertEqual(self.run_helper(), 2)
        self.assertEqual(self.saved()['phase'], 'previous_fast_result_needs_review')
        self.assertEqual(self.submissions(), [])


if __name__ == '__main__':
    unittest.main()
