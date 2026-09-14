"""Device sync invariants with isolated SQLite and tiny local binary chunks.

No HTTP/provider calls, real credentials, user data, or production ports.
"""

from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tingji import jobs, storage, sync


class SyncTests(unittest.TestCase):
    def test_partial_transcript_stays_partial_after_conflict_and_edit(self):
        note = self.note(asrComplete=False)
        current = storage.update_note(note['id'], summary='电脑已修改')
        conflict = sync.push(self.payload(note))
        self.assertTrue(conflict['conflict'])
        self.assertFalse(conflict['note']['asrComplete'])
        updated = sync.push(self.payload(current, transcript='手机校对的部分文字'))
        self.assertFalse(updated['note']['asrComplete'])

    def test_old_device_partial_stays_incomplete_after_computer_finishes(self):
        partial = self.note(asrComplete=False)
        storage.update_note(partial['id'], transcript='电脑已经返回完整原文', asrComplete=True)
        result = sync.push(self.payload(partial, title='手机只改标题'))
        self.assertTrue(result['conflict'])
        self.assertFalse(result['note']['asrComplete'])
        self.assertTrue(result['remote']['asrComplete'])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='tingji-sync-test-')))
        self.stack.enter_context(patch.object(storage, 'DATA', self.root))
        self.stack.enter_context(patch.object(storage, 'DB', self.root / 'notes.sqlite3'))
        self.stack.enter_context(patch.object(sync, 'CHUNK', 4))
        self.stack.enter_context(patch.object(jobs, 'ACTIVE', {}))
        storage.initialize()

    def note(self, **fields):
        created = storage.create_note('电脑原稿', '原文末尾：12.5 N·m', **fields)
        return storage.get_note(created['id'])

    def payload(self, note, base=None, operation=None, **changes):
        raw = dict(note)
        raw.update(changes)
        return {'operationId': operation or str(uuid.uuid4()),
                'baseRevision': note['revision'] if base is None else base,
                'note': raw}

    def upload(self, content=b'abcdefghij', local_id=None, name='手机录音.webm'):
        request = {'localId': local_id or str(uuid.uuid4()), 'name': name, 'size': len(content)}
        return request, sync.start_upload(request)

    def part(self, upload_id, index, content):
        return sync.put_part(upload_id, index, io.BytesIO(content), len(content),
                             hashlib.sha256(content).hexdigest())

    def assert_counts(self, notes=None, receipts=None, uploads=None):
        with storage.connect() as db:
            for table, expected in [('notes', notes), ('sync_receipts', receipts), ('sync_uploads', uploads)]:
                if expected is not None:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], expected, table)

    def test_storage_revision_is_monotonic_and_cannot_be_reset_by_update_values(self):
        note = self.note()
        self.assertEqual(note['revision'], 0)
        self.assertEqual(storage.public_note(note)['revision'], 0)
        first = storage.update_note(note['id'], transcript='电脑修改', revision=800)
        second = storage.update_note(note['id'], summary='新提炼', revision=-7)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(second['revision'], 2)
        self.assertEqual(storage.list_notes()[0]['revision'], 2)

    def test_matching_base_revision_updates_existing_note_and_preserves_original_audio(self):
        note = self.note(audioFile='immutable.wav', audioUrl='/api/audio/example',
                         segments=[{'text': '原段'}], chat=[{'role': 'user', 'content': '问题'}])
        result = sync.push(self.payload(note, transcript='手机修正；保留末尾事实。', summary='提炼'))
        saved = storage.get_note(note['id'])
        self.assertFalse(result['conflict'])
        self.assertEqual(result['note']['id'], note['id'])
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(saved['transcript'], '手机修正；保留末尾事实。')
        self.assertEqual(saved['summary'], '提炼')
        self.assertEqual(saved['audioFile'], 'immutable.wav')
        self.assertEqual(saved['chat'], note['chat'])
        self.assertEqual(saved['segments'], [])
        self.assert_counts(notes=1, receipts=1)

    def test_retry_operation_returns_original_receipt_without_another_revision(self):
        note = self.note()
        request = self.payload(note, transcript='手机稿')
        first = sync.push(request)
        again = sync.push(request)
        self.assertEqual(again, first)
        self.assertEqual(storage.get_note(note['id'])['revision'], 1)
        storage.update_note(note['id'], summary='电脑后续编辑')
        retry_after_edit = sync.push(request)
        self.assertEqual(retry_after_edit, first)
        self.assertEqual(storage.get_note(note['id'])['revision'], 2)
        self.assertEqual(storage.get_note(note['id'])['summary'], '电脑后续编辑')
        self.assert_counts(notes=1, receipts=1)

    def test_same_operation_with_different_content_is_rejected_without_writes(self):
        note = self.note()
        request = self.payload(note, transcript='第一次的内容')
        sync.push(request)
        changed = json.loads(json.dumps(request))
        changed['note']['transcript'] = '同一编号的另一份内容'
        with self.assertRaisesRegex(ValueError, '不同内容'):
            sync.push(changed)
        self.assertEqual(storage.get_note(note['id'])['transcript'], '第一次的内容')
        self.assertEqual(storage.get_note(note['id'])['revision'], 1)
        self.assert_counts(notes=1, receipts=1)

    def test_stale_version_preserves_current_note_and_creates_audio_linked_copy(self):
        note = self.note(audioFile='original.wav', audioUrl='/api/audio/' + 'source', duration=10.2)
        current = storage.update_note(note['id'], transcript='电脑最新正文', summary='电脑最新提炼')
        result = sync.push(self.payload(note, transcript='手机离线全文尾部', summary='手机离线提炼'))
        copy = result['note']
        self.assertTrue(result['conflict'])
        self.assertNotEqual(copy['id'], note['id'])
        self.assertEqual(storage.get_note(note['id']), current)
        self.assertEqual(result['remote']['transcript'], '电脑最新正文')
        self.assertEqual(copy['transcript'], '手机离线全文尾部')
        self.assertEqual(copy['summary'], '手机离线提炼')
        self.assertIn('同步副本', copy['title'])
        self.assertEqual(copy['audioFile'], 'original.wav')
        self.assertEqual(copy['audioUrl'], '/api/audio/' + copy['id'])
        self.assertEqual(copy['revision'], 1)
        self.assert_counts(notes=2, receipts=1)

    def test_busy_or_recoverable_job_always_forks_even_when_revision_matches(self):
        cases = [({'status': 'transcribing'}, False), ({'status': 'summarizing'}, False),
                 ({'status': 'error', 'asrTask': {'requestId': 'existing-task', 'state': 'paused'}}, False),
                 ({'status': 'error', 'asrTask': {'requestId': 'existing-task', 'state': 'not_found'}}, False),
                 ({'status': 'ready'}, True)]
        for fields, active in cases:
            with self.subTest(fields=fields, active=active):
                note = self.note(**fields)
                if active:
                    jobs.ACTIVE[note['id']] = object()
                result = sync.push(self.payload(note, transcript='同步时的本机稿'))
                self.assertTrue(result['conflict'])
                self.assertNotEqual(result['note']['id'], note['id'])
                self.assertEqual(storage.get_note(note['id']), note)
                self.assertNotIn('asrTask', result['note'])

    def test_client_paths_tasks_credentials_and_internal_fields_are_ignored(self):
        note = self.note(audioFile='trusted.wav')
        malicious = {'audioFile': '../../secret', 'audioUrl': 'https://attacker.invalid/',
                     'asrTask': {'requestId': 'injected', 'state': 'processing'},
                     'asrTaskHistory': [{'requestId': 'injected-old'}], 'cloudObject': {'url': 'signed-secret'},
                     'status': 'transcribing', 'revision': 99999, 'ownerId': 'attacker',
                     'createdAt': 'malicious', 'segments': [{'text': 'injected'}]}
        malicious.update({key: 'fake-secret-for-test' for key in storage.SECRET_KEYS})
        result = sync.push(self.payload(note, transcript='合法正文', **malicious))
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['audioFile'], 'trusted.wav')
        self.assertEqual(saved['audioUrl'], note['audioUrl'])
        self.assertEqual(saved['createdAt'], note['createdAt'])
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(saved['status'], 'ready')
        for field in ('asrTask', 'asrTaskHistory', 'cloudObject', 'ownerId', *storage.SECRET_KEYS):
            self.assertNotIn(field, saved)
        self.assertNotIn('fake-secret-for-test', json.dumps(result))
        self.assertEqual(saved['segments'], [])

    def test_new_client_note_receives_server_id_and_no_client_audio_path(self):
        request = {'operationId': str(uuid.uuid4()), 'baseRevision': 0,
                   'note': {'id': 'local-note-0001', 'title': '新稿', 'transcript': '新原文',
                            'audioFile': 'foreign.wav', 'audioUrl': '/api/audio/foreign'}}
        result = sync.push(request)
        self.assertFalse(result['conflict'])
        self.assertNotEqual(result['note']['id'], 'local-note-0001')
        self.assertNotIn('audioFile', result['note'])
        self.assertEqual(result['note']['audioUrl'], '')
        self.assertEqual(result['note']['revision'], 1)

    def test_parts_can_arrive_out_of_order_and_completion_preserves_exact_bytes(self):
        content = b'abcdefghij'
        _, started = self.upload(content)
        upload_id = started['uploadId']
        for index in (2, 0, 1):
            self.part(upload_id, index, content[index * 4:(index + 1) * 4])
        info = sync.load_upload(upload_id)
        self.assertEqual([part['index'] for part in info['parts']], [0, 1, 2])
        result = sync.complete_upload(upload_id)
        saved = storage.get_note(result['note']['id'])
        self.assertEqual((self.root / 'audio' / saved['audioFile']).read_bytes(), content)
        self.assertEqual(saved['fileSize'], len(content))
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(saved['transcript'], '')
        self.assert_counts(notes=1, uploads=1)

    def test_wrong_part_hash_preserves_previous_parts_and_leaves_no_temp_file(self):
        _, started = self.upload()
        upload_id = started['uploadId']
        self.part(upload_id, 0, b'abcd')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            sync.put_part(upload_id, 1, io.BytesIO(b'efgh'), 4, '0' * 64)
        self.assertEqual([part['index'] for part in sync.load_upload(upload_id)['parts']], [0])
        self.assertEqual((sync.folder_for(upload_id) / '000000.part').read_bytes(), b'abcd')
        self.assertEqual(list(sync.folder_for(upload_id).glob('*.tmp')), [])
        self.assertFalse((sync.folder_for(upload_id) / '000001.part').exists())

    def test_wrong_length_is_rejected_without_reading_source(self):
        _, started = self.upload()
        class Unreadable:
            def read(self, size):
                raise AssertionError('invalid length must fail before reading')
        for index, length in ((0, 3), (2, 4), (0, 5), (0, -1)):
            with self.subTest(index=index, length=length):
                with self.assertRaisesRegex(ValueError, '长度不匹配'):
                    sync.put_part(started['uploadId'], index, Unreadable(), length, 'a' * 64)
        self.assertEqual(sync.load_upload(started['uploadId'])['parts'], [])

    def test_short_stream_is_recoverable_without_publishing_partial_part(self):
        _, started = self.upload()
        upload_id = started['uploadId']
        self.part(upload_id, 0, b'abcd')
        with self.assertRaisesRegex(ValueError, '尚未传完'):
            sync.put_part(upload_id, 1, io.BytesIO(b'ef'), 4, hashlib.sha256(b'efgh').hexdigest())
        self.assertFalse((sync.folder_for(upload_id) / '000001.part').exists())
        self.assertEqual(list(sync.folder_for(upload_id).glob('*.tmp')), [])
        self.part(upload_id, 1, b'efgh')
        self.assertEqual(len(sync.load_upload(upload_id)['parts']), 2)

    def test_repeat_part_is_idempotent_and_changed_content_cannot_replace_it(self):
        _, started = self.upload()
        upload_id = started['uploadId']
        first = self.part(upload_id, 0, b'abcd')
        self.assertEqual(self.part(upload_id, 0, b'abcd'), first)
        with self.assertRaisesRegex(ValueError, '不同内容'):
            self.part(upload_id, 0, b'xxxx')
        self.assertEqual((sync.folder_for(upload_id) / '000000.part').read_bytes(), b'abcd')
        self.assertEqual(len(sync.load_upload(upload_id)['parts']), 1)

    def test_start_and_complete_retries_do_not_duplicate_upload_or_note(self):
        content = b'abcd'
        request, started = self.upload(content)
        self.assertEqual(sync.start_upload(request), started)
        self.part(started['uploadId'], 0, content)
        after_part = sync.start_upload(request)
        self.assertEqual(after_part['uploadId'], started['uploadId'])
        self.assertEqual(len(after_part['parts']), 1)
        first = sync.complete_upload(started['uploadId'])
        self.assertEqual(sync.complete_upload(started['uploadId']), first)
        self.assertEqual(sync.start_upload(request)['noteId'], first['note']['id'])
        self.assertEqual(storage.get_note(first['note']['id'])['revision'], 1)
        self.assert_counts(notes=1, uploads=1)
        self.assertEqual(len(list((self.root / 'audio').iterdir())), 1)

    def test_upload_receipts_resume_after_reinitializing_storage(self):
        request, started = self.upload(b'abcdefgh')
        self.part(started['uploadId'], 0, b'abcd')
        storage.initialize()
        resumed = sync.start_upload(request)
        self.assertEqual(resumed['uploadId'], started['uploadId'])
        self.assertEqual(len(resumed['parts']), 1)
        self.part(resumed['uploadId'], 1, b'efgh')
        done = sync.complete_upload(resumed['uploadId'])
        storage.initialize()
        self.assertEqual(sync.complete_upload(resumed['uploadId'])['note']['id'], done['note']['id'])
        self.assert_counts(notes=1, uploads=1)

    def test_changed_local_upload_size_or_name_is_rejected(self):
        request, _ = self.upload()
        for field, value in [('size', 11), ('name', 'different.wav')]:
            with self.subTest(field=field):
                changed = dict(request, **{field: value})
                with self.assertRaisesRegex(ValueError, '已改变'):
                    sync.start_upload(changed)
        self.assert_counts(notes=0, uploads=1)

    def test_complete_rejects_missing_or_corrupted_parts_without_publishing_note(self):
        _, started = self.upload(b'abcdefgh')
        upload_id = started['uploadId']
        self.part(upload_id, 0, b'abcd')
        with self.assertRaisesRegex(ValueError, '未传完'):
            sync.complete_upload(upload_id)
        self.part(upload_id, 1, b'efgh')
        (sync.folder_for(upload_id) / '000001.part').write_bytes(b'CORRUPTED')
        with self.assertRaisesRegex(ValueError, '校验未通过'):
            sync.complete_upload(upload_id)
        self.assert_counts(notes=0, uploads=1)
        self.assertEqual(list((self.root / 'audio').iterdir()), [])
        self.assertNotIn('noteId', sync.load_upload(upload_id))

    def test_invalid_part_index_or_digest_is_rejected(self):
        _, started = self.upload()
        for index in (-1, 3, '0', 0.5):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    sync.put_part(started['uploadId'], index, io.BytesIO(b'abcd'), 4, 'a' * 64)
        for digest in ('', 'a' * 63, 'Z' * 64, '../../escape'):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError):
                    sync.put_part(started['uploadId'], 0, io.BytesIO(b'abcd'), 4, digest)

    def test_path_traversal_identifiers_rejected_across_all_entry_points(self):
        invalid_ids = ['../outside', '..\\outside', '/tmp/absolute', 'C:\\outside', 'a/bbbbbbbb',
                       'a%2fbbbbbbb', 'short', 'x' * 101]
        note = self.note()
        for invalid in invalid_ids:
            with self.subTest(identifier=invalid):
                calls = [lambda: sync.folder_for(invalid), lambda: sync.load_upload(invalid),
                         lambda: sync.complete_upload(invalid),
                         lambda: sync.start_upload({'localId': invalid, 'name': 'x.wav', 'size': 4}),
                         lambda: sync.push(self.payload(note, operation=invalid)),
                         lambda: sync.push(self.payload(note, id=invalid)),
                         lambda: sync.put_part(invalid, 0, io.BytesIO(b'abcd'), 4, 'a' * 64)]
                for call in calls:
                    with self.assertRaises(ValueError):
                        call()
        self.assert_counts(notes=1, receipts=0, uploads=0)
        self.assertFalse((self.root / 'sync').exists())

    def test_client_filename_is_only_a_display_basename_and_server_generates_disk_path(self):
        _, started = self.upload(b'abcd', name='../../outside/evil.wav')
        self.part(started['uploadId'], 0, b'abcd')
        result = sync.complete_upload(started['uploadId'])
        note = storage.get_note(result['note']['id'])
        self.assertEqual(note['sourceName'], 'evil.wav')
        self.assertEqual(note['audioFile'], started['uploadId'] + '.wav')
        self.assertEqual((self.root / 'audio' / note['audioFile']).read_bytes(), b'abcd')

    def test_purged_note_rejects_stale_device_without_creating_copy(self):
        old = self.note(summary='先前笔记')
        current = storage.update_note(old['id'], transcriptPurgedAt=storage.now(), transcript='', segments=[], chat=[], summary='电脑最终笔记')
        with self.assertRaisesRegex(ValueError, '本机修改已保留'):
            sync.push(self.payload(old, summary='离线修改'))
        self.assertEqual(storage.get_note(old['id']), current)
        self.assert_counts(notes=1, receipts=0)

    def test_matching_purged_note_accepts_summary_without_restoring_raw_text(self):
        note = storage.update_note(self.note()['id'], transcriptPurgedAt=storage.now(), transcript='')
        result = sync.push(self.payload(note, transcript='不应恢复的旧原文', summary='用户修改后的笔记',
                                        segments=[{'text': '旧段落'}], chat=[{'content': '旧问答'}]))
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['summary'], '用户修改后的笔记')
        self.assertEqual((saved['transcript'], saved['segments'], saved['chat']), ('', [], []))
        self.assertEqual(result['note']['transcriptPurgedAt'], note['transcriptPurgedAt'])

    def test_old_push_receipt_cannot_return_text_after_purge(self):
        note = self.note()
        request = self.payload(note, transcript='旧回执原文')
        sync.push(request)
        latest = storage.update_note(note['id'], transcriptPurgedAt=storage.now(), transcript='', summary='最终笔记', segments=[], chat=[])
        replay = sync.push(request)
        self.assertEqual(replay['note']['transcript'], '')
        self.assertEqual(replay['note']['summary'], '最终笔记')
        self.assertEqual(replay['note']['revision'], latest['revision'])

    def test_changed_audio_identity_rejected_even_with_same_name_and_size(self):
        request = {'localId': str(uuid.uuid4()), 'audioId': 'audio-first-123', 'name': '课程.webm', 'size': 4}
        started = sync.start_upload(request)
        self.assertEqual(started['audioId'], request['audioId'])
        self.part(started['uploadId'], 0, b'abcd')
        sync.complete_upload(started['uploadId'])
        with self.assertRaisesRegex(ValueError, '已改变'):
            sync.start_upload(dict(request, audioId='audio-second-456'))

    def test_deleted_upload_retry_does_not_rebuild_audio(self):
        request, started = self.upload(b'abcd')
        self.part(started['uploadId'], 0, b'abcd')
        result = sync.complete_upload(started['uploadId'])
        note = storage.get_note(result['note']['id'])
        (self.root / 'audio' / note['audioFile']).unlink()
        storage.update_note(note['id'], audioDeletedAt=storage.now())
        retried = sync.complete_upload(started['uploadId'])
        self.assertFalse(retried['note']['audioAvailable'])
        self.assertTrue(retried['note']['audioDeletedAt'])
        self.assertEqual(sync.start_upload(request)['noteId'], note['id'])
        self.assertFalse((self.root / 'audio' / note['audioFile']).exists())


if __name__ == '__main__':
    unittest.main()
