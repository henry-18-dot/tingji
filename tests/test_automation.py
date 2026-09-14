import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tingji import automation, jobs, standard_jobs, storage, workflow


class AutomationTests(unittest.TestCase):
    def test_budget_change_wakes_waiting_group_and_source_without_dispatch(self):
        source = storage.create_note('等待来源', autoProcessState='waiting', budgetWaitUntil='2099-01-01T00:00:00+08:00')
        parent = storage.create_note('等待整组', groupTask={'state': 'waiting_budget', 'budgetWaitUntil': '2099-01-01T00:00:00+08:00'})
        with patch.object(standard_jobs, 'start') as start:
            automation.wake_budget_waiters()
        self.assertIsNone(storage.get_note(source['id'])['budgetWaitUntil'])
        self.assertIsNone(storage.get_note(parent['id'])['groupTask']['budgetWaitUntil'])
        self.assertEqual(storage.get_note(parent['id'])['groupTask']['state'], 'waiting_budget')
        start.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for obj, name, value in [(storage, 'DATA', root), (storage, 'DB', root/'notes.sqlite3'),
                                 (automation, 'SEEN', {}), (jobs, 'ACTIVE', set())]:
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        storage.initialize()
        self.root = root
        (root / 'inbox').mkdir()

    def file(self, name='lecture.m4a', content=b'recording'):
        path = self.root / 'inbox' / name
        path.write_bytes(content)
        return path

    def test_received_file_moves_without_copy_and_duplicate_removed(self):
        first = self.file()
        note = automation.register_file(first)
        duplicate = self.file('another-name.m4a')
        self.assertEqual(automation.register_file(duplicate)['id'], note['id'])
        self.assertFalse(first.exists())
        self.assertTrue((self.root / 'audio' / note['audioFile']).exists())
        self.assertFalse(duplicate.exists())
        self.assertEqual(len(storage.all_notes()), 1)
        self.assertEqual(note['sourceName'], first.name)

    def test_partially_written_file_must_stabilize(self):
        path = self.file()
        with patch.object(automation.time, 'monotonic', return_value=0):
            automation.scan()
        self.assertEqual(storage.all_notes(), [])
        path.write_bytes(b'changed-recording')
        with patch.object(automation.time, 'monotonic', return_value=20):
            automation.scan()
        self.assertEqual(storage.all_notes(), [])
        with patch.object(automation.time, 'monotonic', return_value=36):
            automation.scan()
        self.assertEqual(len(storage.all_notes()), 1)

    def test_unfinished_transfer_extension_and_external_path_rejected(self):
        self.file('incomplete.uploading')
        automation.scan()
        self.assertEqual(storage.all_notes(), [])
        external = self.root/'outside.m4a'; external.write_bytes(b'file')
        with self.assertRaises(ValueError):
            automation.register_file(external)

    def test_interrupted_inbox_move_recovers_same_note_without_deleting_source(self):
        source = self.file()
        with patch.object(Path, 'replace', side_effect=PermissionError('writing')):
            with self.assertRaises(PermissionError):
                automation.register_file(source)
        self.assertTrue(source.exists())
        ident = storage.all_notes()[0]['id']
        recovered = automation.register_file(source)
        self.assertEqual(recovered['id'], ident)
        self.assertEqual(recovered['autoProcessState'], 'waiting')
        self.assertTrue((self.root/'audio'/recovered['audioFile']).is_file())
        self.assertEqual(len(storage.all_notes()), 1)

    def test_missing_config_saves_recording_without_paid_call(self):
        automation.register_file(self.file())
        with patch.object(standard_jobs, 'start') as start:
            automation.tick()
        start.assert_not_called()
        self.assertEqual(automation.status()['queuedCount'], 1)
        self.assertIn('配置', automation.status()['blockedReason'])

    def test_queue_uses_standard_and_both_consents_once(self):
        note = automation.register_file(self.file())
        with patch.object(automation, 'blocked_reason', return_value=''), \
             patch.object(jobs, 'run_media', return_value=b'{"streams":[{"codec_type":"audio"}]}'), \
             patch.object(jobs, 'duration', return_value=10), \
             patch.object(standard_jobs, 'start', side_effect=lambda ident, *_: storage.update_note(ident, status='transcribing')) as start:
            automation.tick(); automation.tick()
        start.assert_called_once()
        params = start.call_args.args[2]
        self.assertEqual(params['asrMode'], 'standard')
        self.assertTrue(params['cloudUploadConsent'])
        self.assertTrue(params['audioUploadConsent'])
        self.assertEqual(storage.get_note(note['id'])['autoProcessState'], 'running')

    def test_invalid_audio_blocks_before_cloud_and_advances_queue(self):
        automation.register_file(self.file())
        with patch.object(automation, 'blocked_reason', return_value=''), \
             patch.object(jobs, 'run_media', return_value=b'{"streams":[]}'), \
             patch.object(standard_jobs, 'start') as start:
            automation.tick()
        start.assert_not_called()
        note = storage.all_notes()[0]
        self.assertEqual(note['autoProcessState'], 'blocked')
        self.assertIn('音轨', note['error'])

    def test_unknown_dispatch_never_resubmitted(self):
        note = automation.register_file(self.file())
        storage.update_note(note['id'], autoProcessState='dispatching', status='transcribing',
                            asrTask={'requestId':'existing', 'state':'submit_unknown'})
        with patch.object(automation, 'blocked_reason', return_value=''), patch.object(standard_jobs, 'start') as start:
            automation.tick()
        start.assert_not_called()

    def test_pause_persists(self):
        storage.save_settings({'autoProcess':False})
        self.assertFalse(automation.status()['enabled'])
        self.assertIn('暂停', automation.status()['blockedReason'])

    def test_arbitrary_calendar_date_and_archive_are_metadata_only(self):
        note = automation.register_file(self.file())
        assigned = workflow.assign_lesson(note['id'], {'classDate':'2026-09-12'})
        self.assertEqual(assigned['classDate'], '2026-09-12')
        result = workflow.archive_batch([note['id']], True)
        self.assertEqual(result['items'][0]['state'], 'completed')
        self.assertTrue(self.file().exists())
        self.assertEqual(storage.get_note(note['id'])['autoProcessState'], 'waiting')


if __name__ == '__main__':
    unittest.main()
