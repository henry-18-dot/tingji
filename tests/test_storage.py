"""Local persistence and Windows credential protection; isolated temporary data."""

import os
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tingji import storage


class StorageTests(unittest.TestCase):
    def test_daily_budget_and_failure_notifications_persist_and_validate(self):
        self.assertEqual(storage.settings()['dailyBudgetYuan'], 10)
        self.assertTrue(storage.settings()['failureOnlyNotifications'])
        storage.save_settings({'dailyBudgetYuan': 0, 'failureOnlyNotifications': False})
        storage.initialize()
        self.assertEqual(storage.settings()['dailyBudgetYuan'], 0)
        self.assertFalse(storage.settings()['failureOnlyNotifications'])
        for value in (-1, 10001, float('nan'), float('inf'), True, 'invalid'):
            with self.assertRaises(ValueError):
                storage.save_settings({'dailyBudgetYuan': value})
        with self.assertRaises(ValueError):
            storage.save_settings({'failureOnlyNotifications': 'false'})

    def test_restart_exposes_uncertain_flash_billing_before_user_retry(self):
        note = storage.create_note('中断极速任务','已保存部分',status='transcribing',
            fastTask={'state':'transcribing','retryMayCharge':False,'chunks':[{'state':'submitting','requestId':'kept-id'}]})
        storage.initialize()
        recovered = storage.get_note(note['id'])
        self.assertEqual(recovered['transcript'],'已保存部分')
        self.assertEqual(recovered['fastTask']['chunks'][0]['requestId'],'kept-id')
        self.assertTrue(storage.public_note(recovered)['fastTask']['retryMayCharge'])
        self.assertEqual(recovered['status'],'error')
        self.assertFalse(recovered['asrComplete'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-storage-test-")
        self.root = Path(self.temp.name)
        self.data_patch = patch.object(storage, "DATA", self.root)
        self.db_patch = patch.object(storage, "DB", self.root / "notes.sqlite3")
        self.data_patch.start()
        self.db_patch.start()
        storage.initialize()

    def tearDown(self):
        self.db_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def test_missing_mode_uses_standard_and_explicit_choice_survives_restart(self):
        self.assertEqual(storage.settings()['asrMode'], 'standard')
        storage.save_settings({'asrMode': 'fast'})
        storage.initialize()
        self.assertEqual(storage.settings()['asrMode'], 'fast')
        storage.save_settings({'asrMode': 'standard'})
        storage.save_settings({'hotwords': '机器人'})
        storage.initialize()
        self.assertEqual(storage.settings()['asrMode'], 'standard')

    def test_switching_to_standard_preserves_rejected_fast_history_and_audio(self):
        previous = {'state': 'rejected', 'chunks': [{'state': 'failed', 'requestId': 'previous-fast-id'}]}
        note = storage.create_note('保留录音', status='error', audioFile='original.wav',
                                   fastTask=previous, error='极速服务拒绝')
        storage.save_settings({'asrMode': 'standard'})
        storage.initialize()
        restored = storage.get_note(note['id'])
        self.assertEqual(restored['fastTask'], previous)
        self.assertEqual(restored['audioFile'], 'original.wav')
        self.assertEqual(restored['error'], '极速服务拒绝')

    @unittest.skipUnless(os.name == "nt", "Windows account encryption")
    def test_keys_are_encrypted_at_rest_and_never_returned_by_public_settings(self):
        secret = "test-key-中文-DoNotUseForRealRequests"
        public = storage.save_settings({"deepseekApiKey": secret, "asrApiKey": "test-asr-key"})
        self.assertTrue(public["deepseekConfigured"])
        self.assertTrue(public["asrConfigured"])
        for field in storage.SECRET_KEYS:
            self.assertNotIn(field, public)
        with storage.connect() as db:
            encrypted = db.execute("SELECT value FROM settings WHERE key='deepseekApiKey'").fetchone()[0]
        self.assertNotEqual(encrypted, secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(storage.settings(secrets=True)["deepseekApiKey"], secret)
        self.assertNotIn(secret.encode(), storage.DB.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows account encryption")
    def test_empty_key_preserves_saved_key_and_explicit_clear_removes_it(self):
        storage.save_settings({"deepseekApiKey": "test-saved-key"})
        storage.save_settings({"deepseekApiKey": "", "hotwords": "机器人, PID"})
        self.assertEqual(storage.settings(secrets=True)["deepseekApiKey"], "test-saved-key")
        public = storage.save_settings({"clearKeys": ["deepseekApiKey"]})
        self.assertFalse(public["deepseekConfigured"])
        self.assertEqual(storage.settings(secrets=True)["deepseekApiKey"], "")

    def test_restart_marks_inflight_job_as_interrupted_without_losing_user_content(self):
        original = storage.create_note("课程录音", "已保存转写", status="transcribing", audioUrl="/audio/example.wav")
        storage.initialize()
        recovered = storage.get_note(original["id"])
        self.assertEqual(recovered["status"], "error")
        self.assertIn("中断", recovered["error"])
        self.assertEqual(recovered["transcript"], "已保存转写")
        self.assertEqual(recovered["audioUrl"], "/audio/example.wav")

    def test_edit_and_list_preserve_full_note_but_keep_list_payload_small(self):
        note = storage.create_note("Original", "transcript", segments=[{"text": "segment"}], chat=[{"content": "question"}])
        storage.update_note(note["id"], title="Edited", summary="summary " * 50)
        fetched = storage.get_note(note["id"])
        listed = storage.list_notes()[0]
        self.assertEqual(fetched["title"], "Edited")
        self.assertEqual(fetched["transcript"], "transcript")
        self.assertEqual(fetched["segments"], [{"text": "segment"}])
        self.assertTrue(listed["hasTranscript"])
        self.assertTrue(listed["hasSummary"])
        self.assertLessEqual(len(listed["preview"]), 140)
        for field in ("transcript", "summary", "segments", "chat"):
            self.assertNotIn(field, listed)

    @unittest.skipUnless(os.name == "nt", "Windows account encryption")
    def test_legacy_migration_backs_up_original_and_preserves_notes_audio_and_keys(self):
        note = storage.create_note('迁移前标题', '迁移前原稿与末尾重要事实', summary='迁移前提炼', status='ready', audioFile='original.wav')
        audio = storage.DATA / 'audio' / 'original.wav'
        audio.write_bytes(b'legacy-original-audio')
        storage.save_settings({'asrApiKey': 'legacy-asr-test-key', 'deepseekApiKey': 'legacy-ds-test-key'})
        with storage.connect() as db:
            db.execute("DELETE FROM settings WHERE key='_schemaVersion'")
            db.execute("UPDATE settings SET value='volc.bigasr.auc_turbo' WHERE key='asrResourceId'")
            old_cipher = db.execute("SELECT value FROM settings WHERE key='asrApiKey'").fetchone()[0]
        storage.initialize()
        backups = list((storage.DATA / 'backups').glob('before-standard-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            backed_note = json.loads(db.execute('SELECT body FROM notes WHERE id=?', (note['id'],)).fetchone()[0])
            self.assertEqual(backed_note['transcript'], note['transcript'])
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='asrResourceId'").fetchone()[0], 'volc.bigasr.auc_turbo')
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='asrApiKey'").fetchone()[0], old_cipher)
        current = storage.get_note(note['id'])
        self.assertEqual(current['transcript'], note['transcript'])
        self.assertEqual(current['summary'], note['summary'])
        self.assertEqual(audio.read_bytes(), b'legacy-original-audio')
        self.assertEqual(storage.settings()['asrResourceId'], 'volc.seedasr.auc')
        self.assertEqual(storage.settings(secrets=True)['asrApiKey'], 'legacy-asr-test-key')
        self.assertEqual(storage.settings(secrets=True)['deepseekApiKey'], 'legacy-ds-test-key')
        storage.initialize()
        self.assertEqual(len(list((storage.DATA / 'backups').glob('*.sqlite3'))), 1)

    def test_changing_bucket_revokes_previous_private_confirmation(self):
        storage.save_settings({'tosBucket': 'old-bucket', 'tosPrivateConfirmed': True})
        self.assertTrue(storage.settings()['tosPrivateConfirmed'])
        storage.save_settings({'tosBucket': 'new-bucket'})
        self.assertFalse(storage.settings()['tosPrivateConfirmed'])


if __name__ == "__main__":
    unittest.main()
