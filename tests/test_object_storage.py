"""TOS signing vectors and mocked transport. No real credentials or cloud calls."""
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import urllib.error
import urllib.parse

from tingji import jobs, object_storage as tos, storage


CONFIG = {'tosRegion': 'cn-beijing', 'tosBucket': 'test-bucket', 'tosAccessKeyId': 'test-ak',
          'tosSecretAccessKey': 'test-sk', 'tosPrivateConfirmed': True}
FIXED = datetime(2021, 1, 1, tzinfo=timezone.utc)


class ObjectStorageTests(unittest.TestCase):
    def test_delete_cloud_audio_updates_cloud_state_only_and_keeps_local_original(self):
        with tempfile.TemporaryDirectory(prefix='tingji-cloud-delete-test-') as temp:
            root = Path(temp)
            with patch.object(storage, 'DATA', root), patch.object(storage, 'DB', root / 'notes.sqlite3'), \
                    patch.object(jobs, 'ACTIVE', {}):
                storage.initialize()
                source = root / 'audio' / 'original.wav'
                source.write_bytes(b'local-original')
                note = storage.create_note('云端删除', '', audioFile=source.name, asrComplete=True,
                                           status='ready', asrTask={'attemptId': 'attempt-one',
                                             'state': 'completed', 'cloudObject': {
                                                 'objectKey': '', 'bucket': 'test-bucket', 'region': 'cn-beijing'}})
                saved = storage.get_note(note['id'])
                saved['asrTask']['cloudObject']['objectKey'] = f"tingji/{note['id']}/audio.wav"
                storage.update_note(note['id'], asrTask=saved['asrTask'])
                with patch.object(tos, '_delete_object') as delete:
                    result = tos.delete_cloud_audio(note['id'], CONFIG)
                self.assertTrue(result['cloudAudioDeletedAt'])
                self.assertFalse(result.get('audioDeletedAt'))
                self.assertEqual(result['audioFile'], source.name)
                self.assertEqual(source.read_bytes(), b'local-original')
                delete.assert_called_once()
                with patch.object(tos, '_delete_object') as retry:
                    self.assertEqual(tos.delete_cloud_audio(note['id'], CONFIG), result)
                retry.assert_not_called()

    def test_delete_cloud_audio_blocks_processing_without_touching_local_or_cloud(self):
        with tempfile.TemporaryDirectory(prefix='tingji-cloud-delete-test-') as temp:
            root = Path(temp)
            with patch.object(storage, 'DATA', root), patch.object(storage, 'DB', root / 'notes.sqlite3'), \
                    patch.object(jobs, 'ACTIVE', {}):
                storage.initialize()
                source = root / 'audio' / 'original.wav'
                source.write_bytes(b'local-original')
                note = storage.create_note('处理中', '', audioFile=source.name, asrComplete=True,
                                           status='transcribing', asrTask={'requestId': 'pending', 'state': 'queued'})
                with patch.object(tos, '_delete_object') as delete:
                    with self.assertRaisesRegex(tos.ObjectStorageError, '正在处理'):
                        tos.delete_cloud_audio(note['id'], CONFIG)
                delete.assert_not_called()
                self.assertTrue(source.exists())
                self.assertFalse(storage.get_note(note['id']).get('cloudAudioDeletedAt'))

    def test_get_signature_matches_official_python_sdk_test_vector(self):
        # Source: volcengine/ve-tos-python-sdk tests/test_auth.py test_generate_presigned_url.
        config = {'tosRegion': 'beijing', 'tosAccessKeyId': 'ak', 'tosSecretAccessKey': 'sk'}
        url = tos._presigned_url('key', config, 'bkt.tos-cn-beijing.volces.com', 86400, FIXED)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query['X-Tos-Signature'][0], 'b87788cb98d1a5a91a046d20eb212ffc22cf7cd4c1d4e9bd2d15a989afb97d2f')

    def test_sts_put_signature_matches_official_python_sdk_test_vector(self):
        config = {'tosRegion': 'beijing', 'tosAccessKeyId': 'ak', 'tosSecretAccessKey': 'sk', 'tosSessionToken': 'sts'}
        url = tos._presigned_url('key', config, 'bkt.tos-cn-beijing.volces.com', 3600, FIXED, 'PUT')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query['X-Tos-Security-Token'], ['sts'])
        self.assertEqual(query['X-Tos-Signature'][0], '3041fb481e31ec25fe7a1be44cafe25caf1cd97228710ee440b2ca1bd49bc563')

    def test_private_confirmation_invalid_hosts_and_header_injection_are_rejected_locally(self):
        with patch.object(tos, '_upload') as upload:
            for bad in ({'tosPrivateConfirmed': False}, {'tosBucket': 'test.evil.example'},
                        {'tosRegion': 'cn-beijing.evil'}, {'tosAccessKeyId': 'key\ninjected:secret'},
                        {'tosSessionToken': 'token\nsecondline'}):
                with self.subTest(bad=next(iter(bad))):
                    with self.assertRaises(tos.ObjectStorageError):
                        tos.validate_config({**CONFIG, **bad})
            self.assertFalse(upload.called)
        self.assertFalse(tos.configured({}))
        self.assertTrue(tos.configured(CONFIG))

    def test_download_signing_is_https_get_bounded_and_scoped_to_tingji_prefix(self):
        url = tos.presign_download(CONFIG, 'tingji/note/中文 & test.wav', now=FIXED)
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.hostname, 'test-bucket.tos-cn-beijing.volces.com')
        self.assertEqual(urllib.parse.unquote(parsed.path), '/tingji/note/中文 & test.wav')
        self.assertEqual(query['X-Tos-Expires'], ['86400'])
        for key in ('other/secret.wav', 'tingji/../secret.wav', 'tingji//bad.wav', 'tingji/evil\\name.wav'):
            with self.assertRaises(tos.ObjectStorageError):
                tos.presign_download(CONFIG, key)
        with self.assertRaises(tos.ObjectStorageError):
            tos.presign_download(CONFIG, 'tingji/note/a.wav', expires=86401)

    def test_upload_uses_private_acl_standard_storage_sha256_and_opaque_id(self):
        with tempfile.TemporaryDirectory() as temp:
            audio = Path(temp) / '姓名与原始会议标题.wav'
            content = b'readable-test-audio-bytes'
            audio.write_bytes(content)
            with patch.object(tos, '_upload', return_value={'ETag': 'mock-etag'}) as upload:
                result = tos.prepare_and_upload(audio, CONFIG, 'note-123')
            path, host, key, headers = upload.call_args.args
            self.assertEqual(path, audio.resolve())
            self.assertEqual(host, 'test-bucket.tos-cn-beijing.volces.com')
            self.assertNotIn('姓名', key)
            self.assertTrue(key.startswith('tingji/note-123/'))
            self.assertEqual(headers['x-tos-acl'], 'private')
            self.assertEqual(headers['x-tos-storage-class'], 'STANDARD')
            self.assertEqual(headers['x-tos-content-sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(headers['Content-Length'], str(len(content)))
            self.assertIn('x-tos-acl', headers['Authorization'])
            self.assertEqual(result['fileSize'], len(content))
            self.assertEqual(result['etag'], 'mock-etag')
            self.assertEqual(audio.read_bytes(), content)
            self.assertNotIn('tosSecretAccessKey', result)

    def test_put_transport_streams_file_without_following_redirects(self):
        class Response:
            status = 200
            headers = {'ETag': 'stream-verified'}
            def __enter__(self): return self
            def __exit__(self, *args): return False

        captured = {}
        class Opener:
            def open(self, request, timeout):
                captured['method'] = request.method
                captured['length'] = request.get_header('Content-length')
                captured['is_stream'] = hasattr(request.data, 'read')
                captured['bytes'] = request.data.read()
                return Response()

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'test.wav'
            source.write_bytes(b'12345678')
            with patch.object(tos.urllib.request, 'build_opener', return_value=Opener()) as build:
                tos._upload(source, 'test-bucket.tos-cn-beijing.volces.com', 'tingji/x/a.wav', {'Content-Length': '8'})
                self.assertIsInstance(build.call_args.args[0], tos._NoRedirect)
        self.assertEqual(captured, {'method': 'PUT', 'length': '8', 'is_stream': True, 'bytes': b'12345678'})
        with self.assertRaises(tos.ObjectStorageError):
            tos._NoRedirect().redirect_request(None, None, 307, '', {}, 'https://unrelated.example')

    def test_failures_do_not_expose_cloud_response_credentials_or_signed_url(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'test.wav'
            source.write_bytes(b'bytes')
            error = urllib.error.HTTPError('https://secret-url.example?Signature=secret', 403, 'test', {}, io.BytesIO(b'private-audio-and-secret-key'))
            with patch.object(tos.urllib.request.OpenerDirector, 'open', side_effect=error):
                with self.assertRaises(tos.ObjectStorageError) as caught:
                    tos._upload(source, 'test-bucket.tos-cn-beijing.volces.com', 'tingji/x/a.wav', {})
            self.assertIn('403', str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('private-audio', str(caught.exception))

    def test_empty_and_oversized_audio_never_reach_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'test.wav'
            source.write_bytes(b'')
            with patch.object(tos, '_upload') as upload:
                with self.assertRaises(tos.ObjectStorageError):
                    tos.prepare_and_upload(source, CONFIG, 'note')
                source.write_bytes(b'123')
                with patch.object(tos, 'MAX_AUDIO_BYTES', 2):
                    with self.assertRaises(tos.ObjectStorageError):
                        tos.prepare_and_upload(source, CONFIG, 'note')
                self.assertFalse(upload.called)


if __name__ == '__main__':
    unittest.main()
