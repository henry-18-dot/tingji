"""Targeted delete API/transport checks with temporary files and mocked cloud calls."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import server
from tingji import jobs, lifecycle, object_storage as tos, storage


CONFIG = {'tosRegion': 'cn-beijing', 'tosBucket': 'test-bucket', 'tosAccessKeyId': 'mock-only-ak',
          'tosSecretAccessKey': 'mock-only-sk', 'tosPrivateConfirmed': True}


class AudioDeleteEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-audio-delete-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for replacement in (patch.object(storage, 'DATA', self.root),
                            patch.object(storage, 'DB', self.root / 'notes.sqlite3'),
                            patch.object(storage, 'settings', return_value=CONFIG)):
            replacement.start()
            self.addCleanup(replacement.stop)
        storage.initialize()
        self.addCleanup(jobs.ACTIVE.clear)
        self.service = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.service.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.service.shutdown()
        self.service.server_close()
        self.thread.join(timeout=2)

    def note(self, **values):
        name = values.pop('audioFile', 'original.wav')
        (self.root / 'audio' / name).write_bytes(b'original-recording')
        defaults = dict(audioFile=name, asrComplete=True, status='ready', audioArchivedAt='2026-09-10T08:00:00+00:00',
                        summary='整理好的笔记', transcriptPurgedAt='2026-09-10T08:01:00+00:00')
        defaults.update(values)
        return storage.create_note('课程', **defaults)

    def delete(self, note, scope='computer'):
        connection = http.client.HTTPConnection('127.0.0.1', self.service.server_port, timeout=3)
        try:
            connection.request('POST', '/api/notes/' + note['id'] + '/audio-delete', body=json.dumps({'scope': scope}).encode(),
                               headers={'Content-Type': 'application/json', 'X-App-Token': server.TOKEN})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_active_incomplete_unarchived_and_pending_tasks_never_delete_computer_source(self):
        fixtures = [dict(status='transcribing'), dict(status='summarizing'), dict(asrComplete=False),
                    dict(audioArchivedAt=''), dict(status='error', asrTask={'requestId': 'pending-request', 'state': 'paused'}),
                    dict(status='ready', asrTask={'requestId': 'pending-request', 'state': 'queued'})]
        with patch.object(tos, 'delete_note_objects') as cloud:
            for values in fixtures:
                with self.subTest(values=values):
                    note = self.note(**values)
                    status, _ = self.delete(note)
                    self.assertEqual(status, 400)
                    self.assertTrue((self.root / 'audio' / 'original.wav').is_file())
                    self.assertFalse(storage.get_note(note['id']).get('audioDeletedAt'))
                    cloud.assert_not_called()
            note = self.note()
            jobs.ACTIVE.add(note['id'])
            self.assertEqual(self.delete(note)[0], 400)
            cloud.assert_not_called()

    def test_computer_scope_deletes_only_original_and_is_independent_from_cloud_and_cache(self):
        note = self.note()
        other = self.note(audioFile='other.wav')
        audio = self.root / 'jobs' / note['id'] / 'standard-test' / 'audio.mp3'
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b'derived-audio')
        with patch.object(tos, 'delete_note_objects', return_value=1) as cloud:
            status, saved = self.delete(note)
            self.assertEqual(status, 200)
            self.assertEqual(saved['summary'], note['summary'])
            self.assertFalse(saved['audioAvailable'])
            self.assertTrue(saved['audioDeletedAt'])
            self.assertTrue(audio.exists())
            self.assertFalse((self.root / 'audio' / 'original.wav').exists())
            self.assertTrue((self.root / 'audio' / 'other.wav').exists())
            self.assertEqual(storage.get_note(other['id'])['summary'], other['summary'])
            status, repeated = self.delete(note)
            self.assertEqual(status, 200)
            self.assertEqual(repeated, saved)
            cloud.assert_not_called()

    def test_cache_scope_keeps_computer_original_and_cloud_state(self):
        note = self.note()
        cache = self.root / 'jobs' / note['id'] / 'standard-test' / 'audio.mp3'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'derived-audio')
        with patch.object(tos, 'delete_note_objects') as cloud:
            status, saved = self.delete(note, 'cache')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'audio' / 'original.wav').exists())
        self.assertFalse(cache.exists())
        self.assertFalse(saved.get('audioDeletedAt'))
        cloud.assert_not_called()

    def test_cloud_failure_keeps_local_audio_note_and_retry_state(self):
        note = self.note()
        with patch.object(tos, 'delete_note_objects', side_effect=tos.ObjectStorageError('云端录音删除失败。')):
            status, body = self.delete(note, 'cloud')
        self.assertEqual(status, 400)
        self.assertIn('云端录音删除失败', body['error'])
        self.assertEqual(storage.get_note(note['id']), {**note, 'revision': 0})
        self.assertEqual((self.root / 'audio' / 'original.wav').read_bytes(), b'original-recording')

    def test_cloud_scope_marks_cloud_only_and_retries_are_idempotent(self):
        note = self.note(asrTask={'attemptId': 'attempt-one', 'state': 'completed', 'cloudObject': {
            'objectKey': 'tingji/' + 'x' * 8 + '/audio.wav', 'bucket': 'test-bucket', 'region': 'cn-beijing'}})
        # Replace the synthetic key with this note's actual id while keeping the
        # provider transport mocked and therefore fully offline.
        task = storage.get_note(note['id'])['asrTask']
        task['cloudObject']['objectKey'] = f"tingji/{note['id']}/audio.wav"
        storage.update_note(note['id'], asrTask=task)
        with patch.object(tos, 'delete_note_objects', return_value=1) as cloud:
            status, saved = self.delete(note, 'cloud')
            self.assertEqual(status, 200)
            self.assertTrue(saved['cloudAudioDeletedAt'])
            self.assertFalse(saved.get('audioDeletedAt'))
            self.assertTrue((self.root / 'audio' / 'original.wav').exists())
            status, repeated = self.delete(note, 'cloud')
        self.assertEqual(status, 200)
        self.assertEqual(repeated, saved)
        cloud.assert_called_once()


class CloudAudioDeleteTests(unittest.TestCase):
    def note(self, key='tingji/note-test-one-attempt-one/audio.mp3', **cloud):
        return {'id': 'note-test-one', 'asrComplete': True, 'asrTask': {'attemptId': 'attempt-one',
                'cloudObject': {'objectKey': key, 'bucket': 'original-bucket', 'region': 'cn-shanghai', **cloud}}}

    def test_scope_checks_entire_target_set_before_first_delete(self):
        invalid = ['tingji/other-note-attempt-one/audio.mp3',
                   'tingji/note-test-one-other-attempt/audio.mp3',
                   'tingji/note-test-one-attempt-one/../other/audio.mp3',
                   'tingji/note-test-one-attempt-one//audio.mp3',
                   'tingji/note-test-one-attempt-one/audio\\other.mp3',
                   'tingji/note-test-one-attempt-one/audio\n.mp3',
                   'elsewhere/note-test-one/audio.mp3']
        with patch.object(tos, '_delete_object') as delete:
            for key in invalid:
                with self.subTest(key=key):
                    note = self.note()
                    note['asrTaskHistory'] = [self.note(key)['asrTask']]
                    with self.assertRaises(tos.ObjectStorageError):
                        tos.delete_note_objects(note, CONFIG)
                    delete.assert_not_called()

    def test_exact_recorded_versions_are_deduplicated_and_use_original_bucket(self):
        note = self.note(versionId='version-one')
        note['asrTaskHistory'] = [dict(note['asrTask']), self.note(versionId='version-two')['asrTask'],
                                 self.note('tingji/note-test-one/legacy.mp3')['asrTask']]
        with patch.object(tos, '_delete_object') as delete:
            self.assertEqual(tos.delete_note_objects(note, CONFIG), 3)
        self.assertEqual(delete.call_count, 3)
        calls = delete.call_args_list
        self.assertEqual([item.args[2] for item in calls], ['version-one', 'version-two', ''])
        self.assertTrue(all(item.args[0]['tosBucket'] == 'original-bucket' for item in calls))
        self.assertTrue(all(item.args[0]['tosRegion'] == 'cn-shanghai' for item in calls))

    def test_uncompleted_note_never_sends_delete(self):
        note = self.note()
        note['asrComplete'] = False
        with patch.object(tos, '_delete_object') as delete, self.assertRaises(tos.ObjectStorageError):
            tos.delete_note_objects(note, CONFIG)
        delete.assert_not_called()

    def test_version_is_in_both_signed_query_and_delete_url_and_redirects_are_disabled(self):
        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): return False

        captured = {}
        class Opener:
            def open(self, request, timeout):
                captured.update(request=request, timeout=timeout)
                return Response()

        version = 'version +/=&中文'
        with patch.object(tos.urllib.request, 'build_opener', return_value=Opener()) as build, \
                patch.object(tos, '_signature', wraps=tos._signature) as sign:
            tos._delete_object(CONFIG, 'tingji/note-test-one-attempt-one/audio.mp3', version)
        self.assertIsInstance(build.call_args.args[0], tos._NoRedirect)
        self.assertEqual(captured['request'].method, 'DELETE')
        self.assertEqual(captured['timeout'], 30)
        parsed = urllib.parse.urlsplit(captured['request'].full_url)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'test-bucket.tos-cn-beijing.volces.com')
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {'versionId': [version]})
        self.assertEqual(sign.call_args.args[0], 'DELETE')
        self.assertEqual(sign.call_args.args[2], {'versionId': version})

    def test_delete_errors_do_not_expose_credentials_response_body_or_url_and_404_is_idempotent(self):
        for status in (401, 403, 500, 404):
            with self.subTest(status=status):
                error = urllib.error.HTTPError('https://secret.example/?Signature=private-secret', status,
                                               'private-secret', {}, io.BytesIO(b'private-audio-secret'))
                with patch.object(tos.urllib.request.OpenerDirector, 'open', side_effect=error):
                    if status == 404:
                        self.assertIsNone(tos._delete_object(CONFIG, 'tingji/note-test-one/audio.mp3'))
                    else:
                        with self.assertRaises(tos.ObjectStorageError) as caught:
                            tos._delete_object(CONFIG, 'tingji/note-test-one/audio.mp3')
                        for sensitive in ('secret', 'mock-only-ak', 'mock-only-sk', 'private-audio', 'https://'):
                            self.assertNotIn(sensitive, str(caught.exception))
        with patch.object(tos.urllib.request.OpenerDirector, 'open', side_effect=urllib.error.URLError('secret')):
            with self.assertRaises(tos.ObjectStorageError) as caught:
                tos._delete_object(CONFIG, 'tingji/note-test-one/audio.mp3')
        self.assertNotIn('secret', str(caught.exception))

    def test_upload_records_version_without_modifying_source(self):
        with tempfile.TemporaryDirectory(prefix='tingji-version-test-') as temp:
            source = Path(temp) / 'source.wav'
            source.write_bytes(b'source-recording')
            with patch.object(tos, '_upload', return_value={'X-Tos-Version-Id': 'persisted-version'}):
                result = tos.prepare_and_upload(source, CONFIG, 'note-test-one-attempt-one')
            self.assertEqual(result['versionId'], 'persisted-version')
            self.assertEqual(source.read_bytes(), b'source-recording')


if __name__ == '__main__':
    unittest.main()
