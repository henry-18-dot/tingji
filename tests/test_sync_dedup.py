from tests import test_sync
from tingji import storage, sync
import unittest


class DuplicateImportTests(unittest.TestCase):
    setUp = test_sync.SyncTests.setUp
    upload = test_sync.SyncTests.upload
    part = test_sync.SyncTests.part

    def test_upload_ack_during_automatic_transcription_keeps_one_note(self):
        _, upload = self.upload(b'abcd', local_id='auto-device-file')
        self.part(upload['uploadId'], 0, b'abcd')
        note = sync.complete_upload(upload['uploadId'])['note']
        storage.update_note(note['id'], status='transcribing', stage='云端排队',
                            asrTask={'requestId':'one-cloud-task','state':'accepted'})
        result = sync.push({'operationId':'ack-auto-device-file', 'baseRevision':note['revision'],
                            'note':dict(note, localId='auto-device-file'), 'acceptUploadedAudio':True})
        self.assertFalse(result['conflict'])
        self.assertEqual(result['note']['id'], note['id'])
        self.assertEqual(result['note']['asrTask']['requestId'], 'one-cloud-task')
        self.assertEqual(len(storage.all_notes()), 1)

    def test_upload_ack_requires_matching_server_receipt(self):
        note = storage.create_note('another')
        with self.assertRaisesRegex(ValueError, '上传记录'):
            sync.push({'operationId':'ack-missing-upload', 'baseRevision':0,
                        'note':dict(note, localId='missing-device-file'), 'acceptUploadedAudio':True})

    def test_equal_files_with_different_device_ids_share_note_without_replacing_original(self):
        _, first = self.upload(b'abcd', local_id='first-device-file')
        self.part(first['uploadId'], 0, b'abcd')
        original = sync.complete_upload(first['uploadId'])['note']
        storage.update_note(original['id'], title='Manual title', summary='Original note')
        _, second = self.upload(b'abcd', local_id='second-device-file', name='renamed.webm')
        self.part(second['uploadId'], 0, b'abcd')
        result = sync.complete_upload(second['uploadId'])['note']
        self.assertEqual(result['id'], original['id'])
        self.assertTrue(result['duplicateUpload'])
        self.assertEqual(result['title'], 'Manual title')
        self.assertEqual(result['summary'], 'Original note')
        self.assertEqual(len(storage.all_notes()), 1)
        self.assertEqual(len(list((self.root / 'audio').iterdir())), 1)
        self.assertTrue(sync.complete_upload(second['uploadId'])['note']['duplicateUpload'])


if __name__ == '__main__':
    unittest.main()
