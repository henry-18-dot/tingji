"""Retention uses isolated files and mocked providers; no live recordings or API calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tingji import fast_jobs, group_jobs, jobs, lifecycle, providers, standard_jobs, storage


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-retention-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for replacement in (patch.object(storage, 'DATA', self.root),
                            patch.object(storage, 'DB', self.root / 'notes.sqlite3')):
            replacement.start()
            self.addCleanup(replacement.stop)
        storage.initialize()
        self.addCleanup(jobs.ACTIVE.clear)

    def note(self, **values):
        name = values.pop('audioFile', 'original.wav')
        (self.root / 'audio' / name).write_bytes(b'original audio')
        return storage.create_note('课程', '完整原文末尾内容', audioFile=name, asrComplete=True,
                                   segments=[{'text': '完整原文末尾内容'}], chat=[{'content': '旧追问'}], **values)

    def cache(self, note_id, name='result.json'):
        path = self.root / 'jobs' / note_id / 'standard-test' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('缓存中的完整原文', encoding='utf-8')
        return path

    def test_summary_commit_precedes_purge_and_clears_only_its_receipts_and_text_caches(self):
        note = self.note()
        other = self.note(audioFile='other.wav')
        caches = [self.cache(note['id'], name) for name in ('result.json', 'result.tmp', 'original-text.json')]
        audio = self.cache(note['id'], 'audio.mp3')
        other_cache = self.cache(other['id'])
        with storage.connect() as db:
            db.execute('INSERT INTO sync_receipts VALUES (?,?,?)',
                       ('operation-one', 'same-fingerprint', json.dumps({'note': note, 'remote': other})))
        original_purge = lifecycle.purge_transcript

        def purge_after_commit(note_id):
            saved = storage.get_note(note_id)
            self.assertEqual(saved['summary'], '精简后的课堂笔记')
            self.assertFalse(saved['summaryStale'])
            self.assertTrue(saved['transcript'])
            return original_purge(note_id)

        with patch.object(providers, 'summarize', return_value='精简后的课堂笔记'), \
                patch.object(lifecycle, 'purge_transcript', side_effect=purge_after_commit):
            jobs.summarize(note['id'], {}, 'lecture', 'zh')
        saved = storage.get_note(note['id'])
        self.assertEqual((saved['transcript'], saved['segments'], saved['chat']), ('', [], []))
        self.assertTrue(saved['transcriptPurgedAt'])
        self.assertTrue(all(not path.exists() for path in caches))
        self.assertTrue(audio.exists())
        self.assertTrue(other_cache.exists())
        with storage.connect() as db:
            fingerprint, body = db.execute('SELECT payload_hash,response FROM sync_receipts').fetchone()
        self.assertEqual(fingerprint, 'same-fingerprint')
        self.assertEqual(json.loads(body)['note']['transcript'], '')
        self.assertEqual(json.loads(body)['remote']['transcript'], other['transcript'])

    def test_group_source_purge_requires_parent_commit_and_clears_children_only(self):
        child = self.note(audioFile='group-child.wav')
        other = self.note(audioFile='unrelated.wav')
        parent = storage.create_note('整课成稿', '', sourceNoteIds=[child['id']], asrComplete=True,
                                     status='ready', summary='', groupTask={'state': 'summarizing'})
        storage.update_note(child['id'], groupParentId=parent['id'])
        child_cache = self.cache(child['id'], 'result.json')
        other_cache = self.cache(other['id'], 'result.json')
        with storage.connect() as db:
            db.execute('INSERT INTO sync_receipts VALUES (?,?,?)',
                       ('group-source-receipt', 'fingerprint', json.dumps({'note': child, 'other': other})))
        with self.assertRaisesRegex(ValueError, '尚未持久化'):
            lifecycle.purge_group_sources(parent['id'])
        self.assertEqual(storage.get_note(child['id'])['transcript'], child['transcript'])
        self.assertTrue(child_cache.exists())

        committed = storage.update_note(parent['id'], summary='统一课堂成稿', summaryStale=False,
                                        groupTask={'state': 'summarizing'})
        result = lifecycle.purge_group_sources(parent['id'])
        saved_child = storage.get_note(child['id'])
        self.assertEqual(result['summary'], committed['summary'])
        self.assertEqual((saved_child['transcript'], saved_child['segments'], saved_child['chat']), ('', [], []))
        self.assertTrue(saved_child['transcriptPurgedAt'])
        self.assertTrue((self.root / 'audio' / 'group-child.wav').exists())
        self.assertFalse(child_cache.exists())
        self.assertTrue(other_cache.exists())
        with storage.connect() as db:
            body = json.loads(db.execute('SELECT response FROM sync_receipts WHERE operation_id=?',
                                         ('group-source-receipt',)).fetchone()[0])
        self.assertEqual(body['note']['transcript'], '')
        self.assertEqual(body['other']['transcript'], other['transcript'])
        marker = saved_child['transcriptPurgedAt']
        repeated = lifecycle.purge_group_sources(parent['id'])
        self.assertEqual(repeated['summary'], committed['summary'])
        self.assertEqual(storage.get_note(child['id'])['transcriptPurgedAt'], marker)

    def test_group_processing_persists_parent_summary_then_runs_real_source_purge(self):
        child = self.note(audioFile='group-chain.wav')
        parent = group_jobs.create_group([child['id']], '整课成稿')
        cache = self.cache(child['id'], 'source-result.json')

        def fake_summary(note_id, *_args):
            return storage.update_note(note_id, summary='统一成稿', status='ready', summaryStale=False)

        with patch.object(jobs, 'summarize', side_effect=fake_summary):
            result = group_jobs.process_group(parent['id'])
        self.assertEqual(result['summary'], '统一成稿')
        self.assertEqual(result['groupTask']['state'], 'completed')
        saved_child = storage.get_note(child['id'])
        self.assertEqual(saved_child['transcript'], '')
        self.assertTrue(saved_child['transcriptPurgedAt'])
        self.assertFalse(cache.exists())
        self.assertTrue((self.root / 'audio' / 'group-chain.wav').exists())

    def test_failed_empty_or_stale_summary_preserves_source(self):
        for outcome in (ValueError('整理失败'), '', '   '):
            note = self.note(summary='旧笔记', summaryStale=True)
            cached = self.cache(note['id'])
            options = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with patch.object(providers, 'summarize', **options), self.assertRaises(ValueError):
                jobs.summarize(note['id'], {}, 'lecture', 'zh')
            lifecycle.purge_transcript(note['id'])
            saved = storage.get_note(note['id'])
            self.assertEqual(saved['transcript'], note['transcript'])
            self.assertFalse(saved.get('transcriptPurgedAt'))
            self.assertTrue(cached.exists())

    def test_seven_day_boundary_and_audio_deletion_stop_reminder(self):
        note = self.note()
        archived = lifecycle.archive_audio(note['id'], '2026-09-10T08:00:00+00:00')
        self.assertEqual(archived['audioDeleteDueAt'], '2026-09-17T08:00:00+00:00')
        self.assertFalse(lifecycle.public_fields(archived, '2026-09-17T07:59:59+00:00')['cleanupDue'])
        self.assertTrue(lifecycle.public_fields(archived, '2026-09-17T08:00:00+00:00')['cleanupDue'])
        self.assertEqual(lifecycle.archive_audio(note['id'])['audioArchivedAt'], archived['audioArchivedAt'])
        deleted = lifecycle.delete_audio(note['id'])
        self.assertEqual(lifecycle.public_fields(deleted), {'audioAvailable': False, 'cleanupDue': False})

    def test_delete_audio_only_removes_computer_original_and_cache_cleanup_is_separate(self):
        note = self.note(summary='保留笔记')
        other = self.note(audioFile='other.wav')
        converted = self.cache(note['id'], 'audio.mp3')
        transcript = self.cache(note['id'])
        other_audio = self.cache(other['id'], 'audio.mp3')
        repaired = self.root / 'audio' / 'original.playable.mka'
        repaired.write_bytes(b'derived')
        recovery = self.root / 'recovery' / note['id'] / 'full-recording.mp3'
        recovery.parent.mkdir(parents=True)
        recovery.write_bytes(b'derived recovery')
        chunk_dir = self.root / 'sync' / 'upload-test-one'
        chunk_dir.mkdir(parents=True)
        chunk = chunk_dir / '000000.part'
        chunk.write_bytes(b'uploaded')
        info = {'id': 'upload-test-one', 'localId': 'device-note-one', 'noteId': note['id'], 'parts': []}
        with storage.connect() as db:
            db.execute('INSERT INTO sync_uploads VALUES (?,?,?)', (info['id'], info['localId'], json.dumps(info)))
        saved = lifecycle.delete_audio(note['id'])
        self.assertEqual(saved['summary'], '保留笔记')
        self.assertEqual(saved['transcript'], note['transcript'])
        self.assertTrue(saved['audioDeletedAt'])
        self.assertEqual(saved['audioFile'], '')
        for path in (converted, repaired, recovery, chunk):
            self.assertTrue(path.exists(), str(path))
        self.assertFalse((self.root / 'audio' / 'original.wav').exists())
        for path in (transcript, other_audio, self.root / 'audio' / 'other.wav'):
            self.assertTrue(path.exists())
        lifecycle.clean_audio_cache(note['id'])
        for path in (converted, repaired, recovery):
            self.assertFalse(path.exists())
        self.assertTrue(transcript.exists())
        self.assertFalse(chunk.exists())
        self.assertEqual(lifecycle.delete_audio(note['id'])['audioDeletedAt'], saved['audioDeletedAt'])
        with storage.connect() as db:
            self.assertNotIn('audioDeletedAt', json.loads(db.execute('SELECT body FROM sync_uploads').fetchone()[0]))

    def test_shared_audio_remains_until_last_record_releases_it(self):
        first = self.note()
        second = self.note()
        lifecycle.delete_audio(first['id'])
        self.assertTrue(lifecycle.audio_available(storage.get_note(second['id'])))
        lifecycle.delete_audio(second['id'])
        self.assertFalse((self.root / 'audio' / 'original.wav').exists())

    def test_invalid_audio_path_aborts_before_deleting_cache_and_active_job_is_blocked(self):
        note = storage.create_note('非法路径', audioFile='../outside.wav')
        cache = self.cache(note['id'], 'audio.mp3')
        with self.assertRaises(ValueError):
            lifecycle.delete_audio(note['id'])
        self.assertTrue(cache.exists())
        storage.update_note(note['id'], audioFile='valid.wav', status='transcribing')
        with self.assertRaises(ValueError):
            lifecycle.delete_audio(note['id'])
        self.assertTrue(cache.exists())

    def test_cache_cleanup_rejects_running_job_and_never_removes_original(self):
        note = self.note()
        derived = self.cache(note['id'], 'audio.mp3')
        recovery = self.root / 'recovery' / note['id'] / 'raw.webm'
        recovery.parent.mkdir(parents=True)
        recovery.write_bytes(b'recovery')
        storage.update_note(note['id'], status='transcribing')
        with self.assertRaisesRegex(ValueError, '正在处理'):
            lifecycle.clean_audio_cache(note['id'])
        self.assertTrue(derived.exists())
        self.assertTrue(recovery.exists())
        self.assertTrue((self.root / 'audio' / 'original.wav').exists())
        storage.update_note(note['id'], status='ready')
        lifecycle.clean_audio_cache(note['id'])
        self.assertFalse(derived.exists())
        self.assertFalse(recovery.exists())
        self.assertTrue((self.root / 'audio' / 'original.wav').exists())

    def test_purged_note_never_requeries_resubmits_or_restores_old_result(self):
        note = self.note(summary='最终笔记', asrTask={'attemptId': 'test-attempt', 'state': 'completed',
                                                  'requestId': 'test-request'})
        lifecycle.apply_retention(note['id'])
        clean = storage.get_note(note['id'])
        with patch.object(providers, 'query_transcription') as query, patch.object(providers, 'summarize') as summary, \
                patch.object(jobs.POOL, 'submit') as submit:
            standard_jobs.finish_result(note['id'], {'result': {'text': '旧结果'}}, {})
            standard_jobs.query_once(note['id'])
            standard_jobs.start(note['id'], 'transcribe', {'resubmit': True})
            jobs.summarize(note['id'], {}, 'lecture', 'zh')
            fast_jobs._publish(note['id'], {}, {0: {'text': '旧内容'}}, complete=True)
            query.assert_not_called()
            summary.assert_not_called()
            submit.assert_not_called()
        self.assertEqual(storage.get_note(note['id']), clean)
        self.assertFalse((self.root / 'jobs' / note['id']).exists())
        updated = storage.update_note(note['id'], transcript='旧设备原文', segments=[{'text': '旧文'}],
                                      chat=[{'content': '旧追问'}], transcriptPurgedAt='')
        self.assertEqual((updated['transcript'], updated['segments'], updated['chat']), ('', [], []))
        self.assertEqual(updated['transcriptPurgedAt'], clean['transcriptPurgedAt'])

    def test_migration_skips_partial_and_running_records_and_preserves_audio(self):
        done = self.note(summary='最终笔记', asrTask={'completedAt': '2026-09-02T08:00:00+00:00'})
        stale = self.note(summary='旧笔记', summaryStale=True)
        partial = self.note(summary='部分笔记')
        storage.update_note(partial['id'], asrComplete=False)
        running = self.note(summary='进行中', status='summarizing')
        lifecycle.migrate_completed_notes()
        self.assertTrue(storage.get_note(done['id'])['transcriptPurgedAt'])
        self.assertEqual(storage.get_note(done['id'])['audioDeleteDueAt'], '2026-09-09T08:00:00+00:00')
        for note in (stale, partial, running):
            self.assertEqual(storage.get_note(note['id'])['transcript'], note['transcript'])
        self.assertTrue((self.root / 'audio' / 'original.wav').exists())


if __name__ == '__main__':
    unittest.main()
