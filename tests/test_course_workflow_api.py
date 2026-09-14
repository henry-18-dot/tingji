"""Real Handler integration for course workflows; all data and providers are isolated."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
import uuid

import server
from tingji import group_jobs, jobs, providers, storage, timetable, workflow


class CourseWorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-course-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for replacement in (
            patch.object(storage, "DATA", self.root),
            patch.object(storage, "DB", self.root / "notes.sqlite3"),
            patch.object(providers, "deepseek", side_effect=AssertionError("unexpected provider request")),
            patch.object(providers, "submit_transcription", side_effect=AssertionError("unexpected provider request")),
            patch.object(providers, "query_transcription", side_effect=AssertionError("unexpected provider request")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        storage.initialize()
        jobs.ACTIVE.clear()
        group_jobs.GROUP_ACTIVE.clear()
        self.addCleanup(jobs.ACTIVE.clear)
        self.addCleanup(group_jobs.GROUP_ACTIVE.clear)
        self.service = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.service.daemon_threads = True
        self.thread = threading.Thread(
            target=self.service.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.service.shutdown()
        self.service.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.service.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def json_request(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers.update({"X-App-Token": server.TOKEN, "X-Action-Id": str(uuid.uuid4())})
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        return self.request(method, path, body, headers)

    def note(self, title, text="", **extra):
        return storage.create_note(title, text, status="ready" if text else "idle",
                                   asrComplete=bool(text), **extra)

    def test_group_create_replay_and_process_rejects_only_text_mode(self):
        first = self.note("第一段", "第一段原文")
        second = self.note("第二段", "第二段原文")
        operation_id = str(uuid.uuid4())
        payload = {"sourceIds": [first["id"], second["id"]], "title": "统一课堂",
                   "lessonId": "lesson-weekly", "operationId": operation_id}
        status, created = self.json_request("POST", "/api/groups", payload)
        self.assertEqual(status, 201, created)
        status, replay = self.json_request("POST", "/api/groups", payload)
        self.assertEqual(status, 201, replay)
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(replay["sourceNoteIds"], [first["id"], second["id"]])
        status, rejected = self.json_request(
            "POST", f'/api/groups/{created["id"]}/process', {"autoSummarize": False}
        )
        self.assertEqual(status, 400, rejected)
        self.assertIn("不提供只转文字", rejected["error"])
        self.assertEqual(storage.get_note(created["id"])["groupTask"]["state"], "idle")

    def test_batch_create_replay_and_list_stays_queued_without_dispatch(self):
        source = self.note("待处理录音", "已保存原文")
        operation_id = str(uuid.uuid4())
        payload = {
            "noteIds": [source["id"]],
            "options": {"audioUploadConsent": True, "autoSummarize": True, "template": "lecture"},
            "operationId": operation_id,
        }
        status, created = self.json_request("POST", "/api/batches", payload)
        self.assertEqual(status, 202, created)
        self.assertEqual(created["id"], operation_id)
        self.assertEqual(created["state"], "queued")
        self.assertTrue(created["options"]["autoSummarize"])
        status, replay = self.json_request("POST", "/api/batches", payload)
        self.assertEqual(status, 202, replay)
        self.assertEqual(replay, created)
        status, listing = self.json_request("GET", "/api/batches")
        self.assertEqual(status, 200, listing)
        self.assertEqual([item["id"] for item in listing["batches"]], [operation_id])

    def test_manual_merge_preserves_order_and_sources_over_http(self):
        first = self.note("A", summary="A 成稿")
        second = self.note("B", summary="B 成稿")
        payload = {"noteIds": [second["id"], first["id"]], "title": "手工合并",
                   "operationId": str(uuid.uuid4())}
        status, merged = self.json_request("POST", "/api/notes/merge", payload)
        self.assertEqual(status, 201, merged)
        self.assertEqual(merged["mergedNoteIds"], [second["id"], first["id"]])
        self.assertEqual(merged["transcript"], "")
        self.assertLess(merged["summary"].index("B 成稿"), merged["summary"].index("A 成稿"))
        self.assertEqual(storage.get_note(first["id"])["summary"], "A 成稿")
        self.assertEqual(storage.get_note(second["id"])["summary"], "B 成稿")
        self.assertFalse(storage.get_note(first["id"]).get("groupParentId"))

    def test_timetable_save_week_and_exact_lesson_note_link(self):
        schedule = {
            "schemaVersion": 1, "term": "2026秋季", "timezone": "Asia/Shanghai",
            "seasonStart": "2026-09-01", "seasonEnd": "2027-01-31", "week1Start": "2026-09-07",
            "periods": {}, "dayOverrides": {},
            "courses": [{"id": "robot-control-2026", "name": "机器人控制", "lessons": [
                {"id": "robot-control-weekly", "weekday": 1, "periods": [1, 2], "weeks": [1, 16]}
            ]}], "exceptions": [],
        }
        status, saved = self.json_request("POST", "/api/timetable", {"schedule": schedule, "baseRevision": 0})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["revision"], 1)
        linked = self.note("第一课", "只由笔记接口读取的原文", lessonId="robot-control-weekly",
                           courseId="robot-control-2026", classDate="2026-09-07")
        self.note("日期不匹配", "另一天", lessonId="robot-control-weekly",
                  courseId="robot-control-2026", classDate="2026-09-08")
        status, result = self.json_request("GET", "/api/timetable/week?start=2026-09-09")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["schedule"]["startDate"], "2026-09-07T00:00:00+08:00")
        self.assertEqual(len(result["lessons"]), 1)
        lesson = result["lessons"][0]
        self.assertEqual((lesson["lessonId"], lesson["courseId"], lesson["classDate"], lesson["date"]),
                         ("robot-control-weekly", "robot-control-2026", "2026-09-07", "2026-09-07"))
        self.assertEqual(lesson["noteIds"], [linked["id"]])
        self.assertEqual(lesson["notes"][0]["title"], "第一课")
        self.assertNotIn("transcript", lesson["notes"][0])

    def test_timetable_image_and_pdf_import_cleanup_temporary_files(self):
        captured = []

        def fake_import(path, filename, config=None):
            path = Path(path)
            self.assertTrue(path.is_file())
            captured.append((path, filename, path.read_bytes(), dict(config or {})))
            return {"status": "needs_review", "editable": True,
                    "schedule": {"schemaVersion": 1, "term": "", "timezone": "Asia/Shanghai",
                                 "seasonStart": None, "seasonEnd": None, "week1Start": None,
                                 "periods": {}, "dayOverrides": {}, "courses": [], "exceptions": []},
                    "source": {"filename": filename, "rawText": "OCR 原文", "error": ""}}

        fixtures = [("课表截图.png", b"synthetic-png"), ("扫描课表.pdf", b"synthetic-pdf")]
        with patch.object(timetable, "import_file", side_effect=fake_import) as imported:
            for filename, content in fixtures:
                headers = {"X-App-Token": server.TOKEN,
                           "X-File-Name": urllib.parse.quote(filename),
                           "Content-Type": "application/octet-stream"}
                status, result = self.request("POST", "/api/timetable/import", content, headers)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["source"]["rawText"], "OCR 原文")
        self.assertEqual(imported.call_count, 2)
        self.assertEqual([(item[1], item[2]) for item in captured], fixtures)
        deadline = time.monotonic() + 1
        while any(item[0].exists() for item in captured) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(all(not item[0].exists() for item in captured))
        imports = self.root / "imports"
        self.assertTrue(imports.is_dir())
        self.assertEqual(list(imports.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
