"""Timetable tests use an isolated SQLite database and mock provider calls."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from tingji import storage, timetable


class TimetableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tingji-timetable-")
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

    def basic(self, term="2026秋季", course_id="control"):
        return {
            "schemaVersion": 1, "term": term, "timezone": "Asia/Shanghai",
            "seasonStart": "2026-09-01", "seasonEnd": "2027-01-31", "week1Start": "2026-09-07",
            "periods": {"1": {"start": "08:00", "end": "08:50"}}, "dayOverrides": {},
            "courses": [{"id": course_id, "name": "机器人控制", "lessons": [
                {"id": course_id + "-weekly", "weekday": 1, "periods": [1, 2], "weeks": [1, 16]}
            ]}], "exceptions": [],
        }

    def test_load_seed_preserves_unknown_week1_and_save_uses_revision(self):
        seed = self.root / "seed.json"
        data = self.basic()
        data["week1Start"] = None
        seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with patch.object(timetable, "SEED_PATH", seed):
            loaded = timetable.load()
            self.assertIsNone(loaded["week1Start"])
            self.assertEqual(loaded["revision"], 0)
            saved = timetable.save(loaded, base_revision=0)
            self.assertEqual(saved["revision"], 1)
            with self.assertRaises(timetable.RevisionConflict):
                timetable.save(saved, base_revision=0)

    def test_strict_schema_rejects_plans_and_bad_exception(self):
        data = self.basic()
        data["studyPlan"] = ["跨课复习"]
        with self.assertRaisesRegex(timetable.ScheduleValidationError, "studyPlan"):
            timetable.save(data)
        data = self.basic()
        data["exceptions"] = [{"lessonId": "missing", "classDate": "2026-09-07", "action": "cancel"}]
        with self.assertRaisesRegex(timetable.ScheduleValidationError, "找不到"):
            timetable.save(data)

    def test_fresh_checkout_uses_empty_example_without_creating_personal_config(self):
        missing = self.root / "course-schedule.json"
        with patch.object(timetable, "SEED_PATH", missing):
            loaded = timetable.load()
            self.assertEqual(loaded["courses"], [])
            self.assertEqual(loaded["revision"], 0)
            self.assertEqual(timetable.week("2026-09-07")["lessons"], [])
            self.assertFalse(missing.exists())

    def test_bad_personal_seed_is_not_silently_replaced(self):
        broken = self.root / "course-schedule.json"
        broken.write_text("broken", encoding="utf-8")
        with patch.object(timetable, "SEED_PATH", broken):
            with self.assertRaises(timetable.TimetableError):
                timetable.load()
        self.assertEqual(broken.read_text(encoding="utf-8"), "broken")

    def test_same_course_name_in_different_terms_does_not_link_by_name(self):
        first = timetable.save(self.basic(term="2026秋季", course_id="control-2026"))
        lesson_id = first["courses"][0]["lessons"][0]["id"]
        historical = storage.create_note("旧学期同名课程", status="ready", lessonId=lesson_id,
                                         courseId="control-2026", classDate="2026-09-07")
        later = self.basic(term="2027春季", course_id="control-2027")
        later.update(seasonStart="2027-02-01", seasonEnd="2027-07-31", week1Start="2027-02-22")
        timetable.save(later, base_revision=1)
        result = timetable.week("2027-02-22")
        self.assertEqual(result["lessons"][0]["courseName"], "机器人控制")
        self.assertEqual(result["lessons"][0]["noteIds"], [])
        # Saving a newly imported/replacement timetable never rewrites notes.
        self.assertEqual(storage.get_note(historical["id"])["title"], "旧学期同名课程")

    def test_week_links_only_exact_metadata_and_never_returns_note_content(self):
        saved = timetable.save(self.basic())
        lesson_id = saved["courses"][0]["lessons"][0]["id"]
        linked = storage.create_note("第一课", "不得泄露的原文", summary="不得聚合的摘要", status="ready",
                                     lessonId=lesson_id, courseId="control", classDate="2026-09-07")
        storage.create_note("日期不符", status="ready", lessonId=lesson_id,
                            courseId="control", classDate="2026-09-08")
        result = timetable.week(datetime.fromisoformat("2026-09-09T12:00:00+08:00"))
        occurrence = result["lessons"][0]
        self.assertEqual(occurrence["classDate"], "2026-09-07")
        self.assertEqual(occurrence["noteIds"], [linked["id"]])
        self.assertEqual(occurrence["notes"][0]["title"], "第一课")
        self.assertNotIn("transcript", occurrence["notes"][0])
        self.assertNotIn("summary", occurrence["notes"][0])
        self.assertTrue(result["schedule"]["startDate"].endswith("+08:00"))

    def test_stable_lesson_id_survives_move_exception(self):
        data = self.basic()
        data["exceptions"] = [{"lessonId": "control-weekly", "classDate": "2026-09-07",
                               "action": "move", "newDate": "2026-09-09", "periods": [5, 6]}]
        timetable.save(data)
        linked = storage.create_note("调课前建立的笔记", status="ready", lessonId="control-weekly",
                                     courseId="control", classDate="2026-09-07")
        result = timetable.week("2026-09-07")
        self.assertEqual(len(result["lessons"]), 1)
        moved = result["lessons"][0]
        self.assertEqual(moved["lessonId"], "control-weekly")
        self.assertEqual(moved["originalClassDate"], "2026-09-07")
        self.assertEqual(moved["classDate"], "2026-09-07")
        self.assertEqual(moved["date"], "2026-09-09")
        self.assertEqual(moved["periods"], [5, 6])
        self.assertEqual(moved["noteIds"], [linked["id"]])

    def test_unknown_week1_keeps_parity_lesson_visible_but_uncertain(self):
        data = self.basic()
        data["week1Start"] = None
        data["courses"][0]["lessons"][0]["parity"] = "odd"
        timetable.save(data)
        result = timetable.week("2026-09-07")
        self.assertIsNone(result["schedule"]["weekNumber"])
        self.assertTrue(result["lessons"][0]["uncertain"])
        self.assertTrue(result["schedule"]["warnings"])

    def test_import_image_retains_raw_text_and_mocked_ai_json_for_editing(self):
        image = self.root / "schedule.png"
        canvas = Image.new("RGB", (300, 120), "white")
        ImageDraw.Draw(canvas).text((10, 10), "schedule", fill="black")
        canvas.save(image)
        ai = json.dumps({
            "schemaVersion": 1, "term": "2026秋季", "timezone": "Asia/Shanghai",
            "seasonStart": None, "seasonEnd": None, "week1Start": None,
            "periods": {}, "dayOverrides": {}, "courses": [
                {"name": "机器人控制", "lessons": [{"weekday": 3, "periods": [7, 8], "weeks": [1, 16]}]}
            ], "exceptions": [],
        }, ensure_ascii=False)
        with patch.object(timetable, "_run_tesseract", return_value="机器人控制 周三 7-8节 1-16周"), \
             patch.object(timetable.providers, "deepseek", return_value=ai) as mocked:
            result = timetable.import_file(image, "课表.png", {"deepseekApiKey": "mock-only"})
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["editable"])
        self.assertEqual(result["source"]["rawText"], "机器人控制 周三 7-8节 1-16周")
        self.assertTrue(result["source"]["aiStructured"])
        self.assertIsNone(result["schedule"]["week1Start"])
        self.assertTrue(result["schedule"]["courses"][0]["lessons"][0]["id"])
        mocked.assert_called_once()

    def test_import_failure_returns_complete_manual_entry_draft_without_ai(self):
        image = self.root / "broken.png"
        image.write_bytes(b"not-an-image")
        with patch.object(timetable, "_run_tesseract", side_effect=timetable.ImportFileError("OCR 无法读取")), \
             patch.object(timetable.providers, "deepseek") as mocked:
            result = timetable.import_file(image, "broken.png", {})
        self.assertEqual(result["source"]["error"], "OCR 无法读取")
        self.assertEqual(result["source"]["rawText"], "")
        self.assertEqual(result["schedule"]["courses"], [])
        self.assertIn("exceptions", result["schedule"])
        mocked.assert_not_called()
        with self.assertRaises(timetable.ScheduleValidationError):
            timetable.save(result["schedule"])


if __name__ == "__main__":
    unittest.main()
