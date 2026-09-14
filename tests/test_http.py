"""Real local HTTP service, isolated process/data, no keys and no paid requests."""

import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import uuid


ROOT = Path(__file__).resolve().parents[1]


class HTTPTests(unittest.TestCase):
    def test_automatic_queue_status_pause_and_file_arrival(self):
        status, state = self.json_request('GET', '/api/automation')
        self.assertEqual(status, 200)
        self.assertTrue(state['enabled'])
        self.assertIn('配置', state['blockedReason'])
        status, state = self.json_request('POST', '/api/automation', {'enabled':False})
        self.assertFalse(state['enabled'])
        status, _, body = self.request('POST', '/api/upload', b'fixture-audio',
                                      {'X-App-Token':self.token, 'X-File-Name':'lecture.m4a', 'Content-Type':'audio/mp4'})
        self.assertEqual(status, 201)
        note = json.loads(body)
        self.assertEqual(note['autoProcessState'], 'waiting')
        status, state = self.json_request('GET', '/api/automation')
        self.assertEqual(state['queuedCount'], 1)
        self.assertEqual(self.json_request('POST', '/api/live/start', {})[0], 400)

    def test_stale_desktop_edit_cannot_overwrite_device_sync(self):
        note = self.create_note('原文')
        payload = {'operationId':'device-new-revision', 'baseRevision':note.get('revision',0),
                   'note':dict(note,transcript='iPad 最新原文')}
        status, synced = self.json_request('POST','/api/sync/push',payload)
        self.assertEqual(status,200)
        path = '/api/notes/'+note['id']
        status, _ = self.json_request('PATCH',path,{'baseRevision':note.get('revision',0),'transcript':'电脑旧页面修改'})
        self.assertEqual(status,400)
        self.assertEqual(self.json_request('GET',path)[1]['transcript'],'iPad 最新原文')

    def test_private_owner_and_sync_routes_use_real_http(self):
        host = 'pc.example.ts.net'
        (self.data / 'remote.json').write_text(json.dumps({'url':'https://' + host, 'ownerLogin':'owner@example.test'}))
        headers = {'Host':host, 'Tailscale-User-Login':'owner@example.test', 'Origin':'https://' + host}
        status, boot = self.json_request('GET', '/api/bootstrap', headers=headers)
        self.assertEqual(status, 200)
        self.assertFalse(boot['localBrowser'])
        self.assertTrue(boot['accountId'])
        status, _ = self.json_request('GET', '/api/notes', headers={'Host':host, 'Tailscale-User-Login':'other@example.test'})
        self.assertEqual(status, 403)
        status, _ = self.json_request('POST', '/api/live/start', {'audioUploadConsent':False}, headers=headers)
        self.assertEqual(status, 400)
        payload = {'operationId':'test-http-sync-op', 'baseRevision':0,
                   'note':{'id':'local-test-http', 'title':'Phone', 'transcript':'Content', 'summary':''}}
        status, accepted = self.json_request('POST','/api/sync/push',payload,headers=headers)
        self.assertEqual(status,200)
        status, repeated = self.json_request('POST','/api/sync/push',payload,headers=headers)
        self.assertEqual(repeated['note']['id'],accepted['note']['id'])
        status, result = self.json_request('GET','/api/notes',headers=headers)
        self.assertEqual(len(result['notes']),1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-http-test-")
        self.data = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.log = (self.data / "test-server.log").open("ab")
        self.process = None
        self.start_server()

    def start_server(self, fixture=False):
        command = [sys.executable, str(ROOT / "server.py"), "--port", str(self.port)]
        if fixture:
            code = '''import os
from pathlib import Path
from tingji import providers, object_storage
import server
root = Path(os.environ['TINGJI_DATA_DIR'])
def query(config, request_id):
    with (root / 'fixture-query-ids.txt').open('a', encoding='utf-8') as file:
        file.write(request_id + '\\n')
    return {'state': 'completed', 'result': {'text': '重启后查询得到的完整结论'}}
def forbid_submit(*args, **kwargs):
    (root / 'unexpected-submit.txt').write_text('attempted', encoding='utf-8')
    raise AssertionError('Unexpected submission in query-only fixture')
def forbid_upload(*args, **kwargs):
    (root / 'unexpected-upload.txt').write_text('attempted', encoding='utf-8')
    raise AssertionError('Unexpected upload in query-only fixture')
providers.query_transcription = query
providers.submit_transcription = forbid_submit
object_storage.prepare_and_upload = forbid_upload
server.main()
'''
            command = [sys.executable, '-c', code, '--port', str(self.port)]
        self.process = subprocess.Popen(
            command, cwd=ROOT,
            env=dict(os.environ, TINGJI_DATA_DIR=str(self.data)), stdout=self.log, stderr=self.log,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                status, _, body = self.request("GET", "/api/health")
                if status == 200 and json.loads(body).get("app") == "tingji":
                    self.token = self.json_request("GET", "/api/bootstrap")[1]["token"]
                    return
            except (OSError, http.client.HTTPException):
                time.sleep(0.08)
        self.fail("Local service failed to become healthy; inspect isolated test log")

    def stop_server(self):
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=5)
            self.process = None

    def tearDown(self):
        self.stop_server()
        self.log.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def json_request(self, method, path, data=None, headers=None):
        request_headers = {"Content-Type": "application/json"}
        if method != "GET":
            request_headers["X-App-Token"] = self.token
            request_headers['X-Action-Id'] = str(uuid.uuid4())
        request_headers.update(headers or {})
        status, _, body = self.request(method, path, json.dumps(data).encode() if data is not None else None, request_headers)
        return status, json.loads(body)

    def create_note(self, text="保留原始文稿", **extra):
        status, note = self.json_request("POST", "/api/notes", {"title": "测试记录", "text": text, **extra})
        self.assertEqual(status, 201)
        return note

    def seed_task(self, note, state='queued', status='transcribing'):
        task = {'attemptId': 'fixture-attempt', 'requestId': 'durable-request-id', 'queryId': 'durable-query-id',
                'state': state, 'nextCheckAt': '2099-01-01T00:00:00+00:00', 'monitorUntil': '2099-01-02T00:00:00+00:00',
                'cloudObject': {'objectKey': 'private-object-do-not-expose'},
                'downloadUrl': 'https://private.invalid/audio?X-Tos-Signature=secret-signature',
                'tosAccessKeyId': 'never-expose-ak', 'tosSecretAccessKey': 'never-expose-sk'}
        note.update(asrTask=task, asrTaskHistory=[task], status=status, asrComplete=False)
        with closing(sqlite3.connect(self.data / 'notes.sqlite3')) as db:
            db.execute('UPDATE notes SET body=? WHERE id=?', (json.dumps(note, ensure_ascii=False), note['id']))
            db.commit()
        return note

    def test_foreign_origin_rebound_host_and_missing_token_are_rejected(self):
        status, _, _ = self.request("GET", "/api/bootstrap", headers={"Origin": "https://unrelated.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/bootstrap", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/demo", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/bootstrap", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_expired_token_is_explicit_and_rejected_before_mutation(self):
        old_token = self.token
        self.stop_server()
        self.start_server()
        status, payload = self.json_request('POST', '/api/notes', {'title': 'rejected', 'text': 'must not save'},
                                           headers={'X-App-Token': old_token})
        self.assertEqual(status, 403)
        self.assertEqual(payload['code'], 'session_expired')
        self.assertEqual(self.json_request('GET', '/api/notes')[1]['notes'], [])
        status, payload = self.json_request('POST', '/api/notes', {'text': 'foreign'},
                                           headers={'Origin': 'https://unrelated.example'})
        self.assertEqual(status, 403)
        self.assertNotIn('code', payload)
        self.assertEqual(self.json_request('POST', '/api/notes', {'text': 'accepted'})[0], 201)

    def test_stale_settings_keep_mode_but_explicit_mode_change_is_saved(self):
        status, saved = self.json_request('POST', '/api/settings', {'asrMode': 'standard', 'asrModeChanged': True})
        self.assertEqual(status, 200)
        self.assertEqual(saved['asrMode'], 'standard')
        status, saved = self.json_request('POST', '/api/settings', {'asrMode': 'fast', 'hotwords': '机器人'})
        self.assertEqual(saved['asrMode'], 'standard')
        self.assertEqual(saved['hotwords'], '机器人')
        status, saved = self.json_request('POST', '/api/settings', {'asrMode': 'fast', 'asrModeChanged': True})
        self.assertEqual(saved['asrMode'], 'fast')

    def test_original_tail_edit_and_committed_summary_retention_after_restart(self):
        text = "开始\n" + "课程内容" * 10000 + "\n最后决定：电流限制 1.5 A。"
        note = self.create_note(text)
        path = f'/api/notes/{note["id"]}'
        status, edited = self.json_request("PATCH", path, {"transcript": text + "\n更正说明", "baseRevision":note.get('revision',0)})
        self.assertEqual(status, 200)
        self.assertFalse(edited["summaryStale"])
        self.stop_server()
        self.start_server()
        status, recovered = self.json_request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(recovered["transcript"], text + "\n更正说明")
        self.assertEqual(recovered["summary"], "")
        status, saved = self.json_request("PATCH", path, {"summary": "初稿", "title": "修改后的标题", "baseRevision":recovered['revision']})
        self.assertEqual(status, 200)
        # Existing retention policy purges the transcript only after the summary commit.
        self.assertTrue(saved['transcriptPurgedAt'])
        self.assertEqual(saved['transcript'], '')
        self.stop_server()
        self.start_server()
        status, recovered = self.json_request("GET", path)
        self.assertEqual(recovered['transcript'], '')
        self.assertEqual(recovered["summary"], "初稿")
        self.assertEqual(recovered["title"], "修改后的标题")

    def test_missing_deepseek_key_returns_actionable_error_and_preserves_original(self):
        note = self.create_note("独立保存的原文，不能因未填密钥丢失。")
        path = f'/api/notes/{note["id"]}'
        status, result = self.json_request("POST", path + "/summarize", {})
        self.assertEqual(status, 400)
        self.assertIn("DeepSeek API Key", result["error"])
        status, saved = self.json_request("GET", path)
        self.assertEqual(saved["transcript"], note["transcript"])
        self.assertEqual(saved["status"], "idle")

    def test_upload_and_range_replay_keep_exact_original_and_reject_no_key_transcribe(self):
        audio = b"RIFF-test-audio-original-bytes-0123456789"
        headers = {"X-App-Token": self.token, "X-File-Name": urllib.parse.quote("../中文 & 录音.wav"), "Content-Type": "application/octet-stream"}
        status, _, body = self.request("POST", "/api/upload", body=audio, headers=headers)
        self.assertEqual(status, 201)
        note = json.loads(body)
        self.assertEqual(note["sourceName"], "中文 & 录音.wav")
        status, _, body = self.request("GET", note["audioUrl"])
        self.assertEqual(body, audio)
        status, response_headers, body = self.request("GET", note["audioUrl"], headers={"Range": "bytes=5-12"})
        self.assertEqual(status, 206)
        self.assertEqual(body, audio[5:13])
        self.assertEqual(response_headers["Content-Range"], f"bytes 5-12/{len(audio)}")
        status, _, body = self.request("GET", note["audioUrl"], headers={"Range": "bytes=-4"})
        self.assertEqual(body, audio[-4:])
        status, _, _ = self.request("GET", note["audioUrl"], headers={"Range": "bytes=999-"})
        self.assertEqual(status, 416)
        status, result = self.json_request("POST", f'/api/notes/{note["id"]}/transcribe', {})
        self.assertEqual(status, 400)
        self.assertIn("豆包语音 API Key", result["error"])
        self.assertEqual(list((self.data / "audio").iterdir())[0].read_bytes(), audio)

    def test_demo_is_marked_and_reused_separately_from_imported_text(self):
        imported = self.create_note("我自己的资料")
        status, demo = self.json_request("POST", "/api/demo", {})
        status, second = self.json_request("POST", "/api/demo", {})
        self.assertTrue(demo["isDemo"])
        self.assertFalse(imported["isDemo"])
        self.assertEqual(demo["id"], second["id"])
        self.assertNotEqual(demo["id"], imported["id"])
        self.assertTrue(demo["summary"])

    def test_static_path_traversal_and_unsupported_upload_are_rejected(self):
        status, _, _ = self.request("GET", "/%2e%2e/server.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/api/upload", body=b"example", headers={"X-App-Token": self.token, "X-File-Name": "script.exe"})
        self.assertEqual(status, 400)
        self.assertEqual(list((self.data / "audio").iterdir()), [])

    def test_waiting_task_cannot_edit_chat_summarize_or_repeat_transcribe(self):
        note = self.seed_task(self.create_note('已有内容必须保留'))
        path = f'/api/notes/{note["id"]}'
        for method, target, data in (
            ('PATCH', path, {'transcript': 'cannot overwrite', 'baseRevision':note.get('revision',0)}),
            ('POST', path + '/chat', {'message': '现在总结'}),
            ('POST', path + '/summarize', {}),
            ('POST', path + '/transcribe', {'cloudUploadConsent': True}),
        ):
            status, response = self.json_request(method, target, data)
            self.assertEqual(status, 400, response)
        self.assertEqual(self.json_request('GET', path)[1]['transcript'], '已有内容必须保留')

    def test_public_api_never_returns_task_secrets_object_metadata_or_history(self):
        note = self.seed_task(self.create_note(), state='paused', status='error')
        for path in (f'/api/notes/{note["id"]}', '/api/notes', '/api/bootstrap'):
            status, payload = self.json_request('GET', path)
            self.assertEqual(status, 200)
            raw = json.dumps(payload)
            for text in ('X-Tos-Signature', 'downloadUrl', 'cloudObject', 'asrTaskHistory', 'never-expose', 'private-object-do-not-expose'):
                self.assertNotIn(text, raw)

    def test_paused_and_not_found_tasks_reject_edit_with_conflict_until_resolved(self):
        for state in ('paused', 'not_found'):
            with self.subTest(state=state):
                note = self.seed_task(self.create_note('暂停前原稿'), state=state, status='error')
                path = f'/api/notes/{note["id"]}'
                status, response = self.json_request('PATCH', path, {'transcript': 'would be overwritten', 'summary': 'unsafe edit', 'baseRevision':note.get('revision',0)})
                self.assertEqual(status, 400, response)
                saved = self.json_request('GET', path)[1]
                self.assertEqual(saved['transcript'], '暂停前原稿')
                self.assertEqual(saved['asrTask']['requestId'], 'durable-request-id')

    def test_process_restart_scheduler_queries_persisted_id_without_upload_or_submit(self):
        note = self.create_note('重启前原稿')
        self.stop_server()
        self.seed_task(note, state='submitting')
        # Actual process restart runs real migration, scheduler, HTTP and persistence;
        # only the outbound query/submit/upload functions are replaced by fixtures.
        self.start_server(fixture=True)
        deadline = time.monotonic() + 5
        saved = None
        while time.monotonic() < deadline:
            saved = self.json_request('GET', f'/api/notes/{note["id"]}')[1]
            if saved.get('asrTask', {}).get('state') == 'completed': break
            time.sleep(0.15)
        self.assertEqual(saved['asrTask']['state'], 'completed')
        self.assertIn('重启后查询得到的完整结论', saved['transcript'])
        self.assertEqual((self.data / 'fixture-query-ids.txt').read_text(encoding='utf-8').splitlines(), ['durable-query-id'])
        self.assertFalse((self.data / 'unexpected-submit.txt').exists())
        self.assertFalse((self.data / 'unexpected-upload.txt').exists())


if __name__ == "__main__":
    unittest.main()
