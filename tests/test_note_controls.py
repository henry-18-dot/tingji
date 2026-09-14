"""Note controls exercise temporary storage and loopback HTTP, with cloud calls forbidden."""
from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import uuid

import server
from tingji import audio_details, jobs, object_storage, providers, storage, sync


class NoteControlsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='tingji-note-controls-')))
        self.stack.enter_context(patch.object(storage, 'DATA', self.root))
        self.stack.enter_context(patch.object(storage, 'DB', self.root / 'notes.sqlite3'))
        self.stack.enter_context(patch.object(jobs, 'ACTIVE', set()))
        self.stack.enter_context(patch.object(storage, 'settings', return_value={}))
        self.stack.enter_context(patch.object(audio_details, 'ensure_duration'))
        for target, name in ((providers, 'submit_transcription'), (providers, 'query_transcription'),
                             (providers, 'deepseek'), (object_storage, 'prepare_and_upload')):
            self.stack.enter_context(patch.object(target, name, side_effect=AssertionError('Unexpected cloud call')))
        storage.initialize()
        self.service = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.service.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.service.shutdown()
        self.service.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, data=None, *, content=None, headers=None):
        request_headers = {'X-App-Token': server.TOKEN}
        if data is not None:
            content = json.dumps(data, ensure_ascii=False).encode('utf-8')
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        client = http.client.HTTPConnection('127.0.0.1', self.service.server_port, timeout=3)
        try:
            client.request(method, path, body=content, headers=request_headers)
            response = client.getresponse()
            body = response.read()
            values = {key.lower(): value for key, value in response.getheaders()}
            parsed = json.loads(body) if 'application/json' in values.get('content-type', '') else body
            return response.status, parsed, values
        finally:
            client.close()

    def note(self, **values):
        name = str(uuid.uuid4()) + '.webm'
        (self.root / 'audio' / name).write_bytes(b'original-audio-recording')
        defaults = dict(audioFile=name, sourceName='original-recording.webm', summary='保留完整笔记',
                        segments=[{'start': 1, 'text': '保留段落'}], chat=[{'role': 'user', 'content': '保留问答'}],
                        nameSource='auto', duration=129.25, revision=0,
                        asrTask={'requestId': 'existing-request', 'state': 'completed', 'cloudObject': {'objectKey': 'original-object'}})
        defaults.update(values)
        note = storage.create_note('原录音名称', '保留原文', **defaults)
        return storage.get_note(note['id'])

    def metadata_payload(self, note, changes, **extra):
        return {'operationId': str(uuid.uuid4()), 'baseRevision': note.get('revision', 0), 'note': dict(note),
                'metadataPatch': changes, 'metadataBase': {key: note.get(key) for key in changes}, **extra}

    def test_title_only_patch_during_transcription_and_summary_keeps_task_and_content(self):
        for status in ('transcribing', 'summarizing'):
            with self.subTest(status=status):
                note = self.note(status=status, asrComplete=False, asrTask={'requestId': 'active-request', 'state': 'processing', 'queryId': 'same-query'})
                jobs.ACTIVE.add(note['id'])
                response, body, _ = self.request('PATCH', '/api/notes/' + note['id'],
                                                {'baseRevision': note['revision'], 'title': '课堂重点', 'nameSource': 'manual'})
                self.assertEqual(response, 200, body)
                saved = storage.get_note(note['id'])
                self.assertEqual(saved['title'], '课堂重点')
                self.assertEqual(saved['nameSource'], 'manual')
                for key in ('status', 'asrTask', 'transcript', 'summary', 'segments', 'chat', 'duration', 'audioFile'):
                    self.assertEqual(saved[key], note[key], key)

    def test_content_patch_is_still_blocked_while_processing(self):
        note = self.note(status='summarizing')
        status, _, _ = self.request('PATCH', '/api/notes/' + note['id'],
                                    {'baseRevision': note['revision'], 'title': '附带改名', 'summary': '覆盖正在生成的笔记'})
        self.assertEqual(status, 400)
        self.assertEqual(storage.get_note(note['id']), note)

    def test_archive_delete_and_restore_only_change_note_metadata(self):
        note = self.note()
        other = self.note()
        path = '/api/notes/' + note['id']
        for action, data, field, active in (
                ('archive', {'archived': True}, 'archivedAt', True),
                ('archive', {'archived': False}, 'archivedAt', False),
                ('delete', {}, 'trashedAt', True),
                ('restore', {}, 'trashedAt', False)):
            with self.subTest(action=action, data=data):
                status, body, _ = self.request('POST', path + '/' + action, data)
                self.assertEqual(status, 200, body)
                saved = storage.get_note(note['id'])
                self.assertEqual(bool(saved.get(field)), active)
                if not active:
                    self.assertIsNone(saved.get(field))
                for key in ('title', 'summary', 'transcript', 'segments', 'asrTask', 'audioFile'):
                    self.assertEqual(saved[key], note[key], key)
                self.assertEqual((self.root / 'audio' / note['audioFile']).read_bytes(), b'original-audio-recording')
                self.assertEqual(storage.get_note(other['id']), other)

    def test_archive_requires_boolean_before_changing_any_note(self):
        note = self.note()
        for value in ('false', 0, 1, None):
            with self.subTest(value=value):
                status, _, _ = self.request('POST', '/api/notes/' + note['id'] + '/archive', {'archived': value})
                self.assertEqual(status, 400)
                self.assertEqual(storage.get_note(note['id']), note)

    def test_metadata_rename_survives_stage_revision_and_preserves_current_raw_and_summary(self):
        base = self.note(status='transcribing', asrComplete=False,
                         asrTask={'requestId': 'active-request', 'state': 'processing'})
        current = storage.update_note(base['id'], stage='正在转写', transcript='服务返回的新原文', summary='当前笔记')
        payload = self.metadata_payload(base, {'title': '离线改的名称', 'nameSource': 'manual'})
        payload['note'].update(transcript='设备上的旧原文', summary='设备上的旧笔记', asrTask={'state': 'failed'})
        status, body, _ = self.request('POST', '/api/sync/push', payload)
        self.assertEqual(status, 200, body)
        self.assertFalse(body['conflict'])
        saved = storage.get_note(base['id'])
        self.assertEqual(saved['title'], '离线改的名称')
        self.assertEqual(saved['nameSource'], 'manual')
        for key in ('status', 'stage', 'transcript', 'summary', 'asrTask', 'segments', 'chat', 'audioFile'):
            self.assertEqual(saved[key], current[key], key)

    def test_metadata_sync_cannot_resurrect_purged_transcript(self):
        base = self.note()
        storage.update_note(base['id'], transcript='', segments=[], chat=[], transcriptPurgedAt='2026-09-10T01:00:00Z')
        payload = self.metadata_payload(base, {'archivedAt': '2026-09-10T02:00:00Z'})
        status, body, _ = self.request('POST', '/api/sync/push', payload)
        self.assertEqual(status, 200, body)
        saved = storage.get_note(base['id'])
        self.assertEqual((saved['transcript'], saved['segments'], saved['chat']), ('', [], []))
        self.assertEqual(saved['summary'], base['summary'])
        self.assertEqual(saved['archivedAt'], '2026-09-10T02:00:00Z')

    def test_metadata_rename_rejects_conflicting_manual_title_without_new_copy(self):
        base = self.note()
        current = storage.update_note(base['id'], title='另一设备手动起名', nameSource='manual')
        status, _, _ = self.request('POST', '/api/sync/push', self.metadata_payload(base, {'title': '旧设备的起名'}))
        self.assertEqual(status, 400)
        self.assertEqual(storage.get_note(base['id']), current)
        self.assertEqual(len(storage.all_notes()), 1)

    def test_metadata_rename_can_replace_auto_title_produced_since_last_sync(self):
        base = self.note()
        current = storage.update_note(base['id'], title='机器人驱动系统-绪论-周三 78节', nameSource='auto', summary='最新笔记')
        status, body, _ = self.request('POST', '/api/sync/push', self.metadata_payload(base, {'title': '我的复习重点'}))
        self.assertEqual(status, 200, body)
        self.assertEqual(body['note']['title'], '我的复习重点')
        self.assertEqual(body['note']['summary'], current['summary'])

    def test_metadata_whitelist_and_malformed_base_fail_before_mutation(self):
        note = self.note()
        for changes in ({}, {'transcript': '旧原文'}, {'asrTask': {'state': 'completed'}}, {'audioFile': '../foreign.wav'}):
            with self.subTest(changes=changes):
                status, _, _ = self.request('POST', '/api/sync/push', self.metadata_payload(note, changes))
                self.assertEqual(status, 400)
                self.assertEqual(storage.get_note(note['id']), note)
        stale = storage.update_note(note['id'], stage='阶段已变')
        payload = self.metadata_payload(note, {'title': '改名'}, metadataBase='not-a-mapping')
        status, _, _ = self.request('POST', '/api/sync/push', payload)
        self.assertEqual(status, 400)
        self.assertEqual(storage.get_note(note['id']), stale)

    def test_metadata_for_unknown_note_does_not_create_a_raw_text_copy(self):
        unknown = {'id': str(uuid.uuid4()), 'title': '设备残留记录', 'transcript': '不应复活的原文', 'summary': '旧笔记'}
        status, _, _ = self.request('POST', '/api/sync/push', self.metadata_payload(unknown, {'title': '尝试改名'}))
        self.assertIn(status, (400, 404))
        self.assertEqual(storage.all_notes(), [])

    def test_sync_validates_duration_and_recorded_at(self):
        note = self.note()
        for value in (-1, True, '180', float('nan'), float('inf'), 18001):
            with self.subTest(duration=value):
                raw = {**note, 'duration': value}
                status, _, _ = self.request('POST', '/api/sync/push',
                                            {'operationId': str(uuid.uuid4()), 'baseRevision': 0, 'note': raw})
                self.assertEqual(status, 400)
                self.assertEqual(storage.get_note(note['id']), note)
        for value in ('not-a-time', '2026-09-09T16:35:38'):
            with self.subTest(recordedAt=value):
                status, _, _ = self.request('POST', '/api/sync/push',
                    {'operationId': str(uuid.uuid4()), 'baseRevision': 0, 'note': {**note, 'recordedAt': value}})
                self.assertEqual(status, 400)
                self.assertEqual(storage.get_note(note['id']), note)

    def test_completed_upload_preserves_capture_metadata_separate_from_upload_time(self):
        audio = b'whole-recording-fixture'
        captured = '2026-09-09T16:35:38+08:00'
        uploaded = '2026-09-12T02:00:00+00:00'
        payload = {'localId': str(uuid.uuid4()), 'audioId': str(uuid.uuid4()), 'name': '自定义录音.webm',
                   'size': len(audio), 'title': '驱动系统第一讲', 'nameSource': 'auto',
                   'recordedAt': captured, 'duration': 135.5}
        with patch.object(storage, 'now', return_value=uploaded), patch.object(jobs, 'duration', return_value=135.5):
            status, started, _ = self.request('POST', '/api/sync/uploads', payload)
            self.assertEqual(status, 200, started)
            base = '/api/sync/uploads/' + started['uploadId']
            status, body, _ = self.request('PUT', base + '/0', content=audio,
                headers={'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': hashlib.sha256(audio).hexdigest()})
            self.assertEqual(status, 200, body)
            status, body, _ = self.request('POST', base + '/complete', {})
            self.assertEqual(status, 200, body)
        saved = storage.get_note(body['note']['id'])
        self.assertEqual(saved['recordedAt'], captured)
        self.assertEqual(saved['uploadedAt'], uploaded)
        self.assertEqual(saved['duration'], 135.5)
        self.assertEqual(saved['title'], '驱动系统第一讲')
        self.assertEqual(saved['nameSource'], 'auto')
        self.assertEqual((self.root / 'audio' / saved['audioFile']).read_bytes(), audio)

    def test_audio_download_name_follows_renamed_title_without_moving_recording(self):
        note = self.note()
        title = '机器人驱动系统-驱动器与机器人-周三 78节'
        saved = storage.update_note(note['id'], title=title, nameSource='manual')
        status, content, headers = self.request('GET', '/api/audio/' + note['id'])
        self.assertEqual(status, 200)
        self.assertEqual(content, b'original-audio-recording')
        disposition = urllib.parse.unquote(headers.get('content-disposition', ''))
        self.assertIn(title + '.webm', disposition)
        self.assertEqual(saved['audioFile'], note['audioFile'])
        status, content, headers = self.request('GET', '/api/audio/' + note['id'], headers={'Range': 'bytes=0-7'})
        self.assertEqual(status, 206)
        self.assertEqual(content, b'original')
        self.assertIn(title + '.webm', urllib.parse.unquote(headers.get('content-disposition', '')))


if __name__ == '__main__':
    unittest.main()
