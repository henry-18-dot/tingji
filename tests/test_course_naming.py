"""Naming tests use synthetic recordings and an isolated database."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tingji import course_naming as naming, jobs, providers, storage


DRIVE_SUMMARY = '# 驱动器与机器人\n\n执行器将能量转换为机械能。电机、气动肌肉和液压驱动可按任务选择。'

# A synthetic schedule keeps these cases independent of the user's local files.
TEST_SCHEDULE = {
    'seasonStart': '2026-09-01', 'seasonEnd': '2027-01-31', 'week1Start': None,
    'periods': {
        '1': {'start': '08:00'}, '2': {'end': '09:50'},
        '5': {'start': '14:00'}, '6': {'end': '15:50'},
        '7': {'start': '16:20'}, '8': {'end': '18:10'},
        '9': {'start': '19:00'}, '10': {'end': '20:50'},
    },
    'dayOverrides': {'2026-09-20': {'weekday': 5}},
    'courses': [
        {'name': '机器人驱动系统', 'color': '#546e62', 'aliases': ['机器人驱动系统'],
         'keywords': ['驱动器', '执行器', '电机', '气动肌肉', '液压驱动'],
         'lessons': [{'weekday': 3, 'periods': [7, 8]}, {'weekday': 4, 'periods': [7, 8]}]},
        {'name': '机械制造基础', 'color': '#546e62', 'aliases': ['机械制造基础'],
         'keywords': ['切削', '车削', '铣削', '刀具'],
         'lessons': [{'weekday': 3, 'periods': [1, 2]},
                     {'weekday': 5, 'periods': [5, 6], 'parity': 'odd'}]},
        {'name': '机器人建模与控制', 'color': '#546e62', 'aliases': ['机器人建模与控制'],
         'keywords': ['逆运动学', '雅可比', 'DH参数'],
         'lessons': [{'weekday': 3, 'periods': [5, 6], 'parity': 'even'},
                     {'weekday': 5, 'periods': [7, 8]}]},
        {'name': '环境工程原理', 'color': '#546e62', 'aliases': ['环境工程原理'],
         'keywords': ['流体静力学', '静水压力'],
         'lessons': [{'weekday': 3, 'periods': [9, 10]}]},
    ],
}


class CourseNamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tingji-naming-config-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.schedule_path = self.root / 'course-schedule.json'
        self.schedule_path.write_text(json.dumps(TEST_SCHEDULE, ensure_ascii=False), encoding='utf-8')
        self.schedule_patch = patch.object(naming, 'SCHEDULE', self.schedule_path)
        self.schedule_patch.start()
        self.addCleanup(self.schedule_patch.stop)

    def test_missing_personal_schedule_uses_empty_example(self):
        missing = self.root / 'missing.json'
        with patch.object(naming, 'SCHEDULE', missing):
            self.assertEqual(naming.load_schedule()['courses'], [])
            result = naming.infer_fields(self.note())
        self.assertEqual(result['courseName'], '')
        self.assertEqual(result['title'], '驱动器与机器人')
        self.assertFalse(missing.exists())

    def test_bad_personal_schedule_is_not_silently_replaced(self):
        self.schedule_path.write_text('broken', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            naming.load_schedule()

    def note(self, **values):
        return {'title': '录音 2026-09-09 16-35-38', 'sourceName': '录音 2026-09-09 16-35-38.webm',
                'summary': DRIVE_SUMMARY, 'createdAt': '2026-09-10T00:10:00Z', **values}

    def test_source_time_beats_upload_date_and_complete_name_matches_course(self):
        result = naming.infer_fields(self.note())
        self.assertEqual(result['recordedAt'], '2026-09-09T16:35:38+08:00')
        self.assertEqual(result['title'], '驱动器与机器人')
        self.assertEqual(result['courseName'], '机器人驱动系统')
        self.assertEqual(result['topic'], '驱动器与机器人')
        self.assertEqual(result['lessonSlot'], '周三 78节')
        self.assertGreaterEqual(result['namingConfidence'], 0.8)

    def test_explicit_recorded_at_uses_shanghai_timezone_and_takes_precedence(self):
        result = naming.infer_fields(self.note(recordedAt='2026-09-10T08:30:00Z'))
        self.assertEqual(result['recordedAt'], '2026-09-10T16:30:00+08:00')
        self.assertEqual(result['lessonSlot'], '周四 78节')

    def test_never_uses_upload_date_when_capture_time_unknown(self):
        result = naming.infer_fields(self.note(title='未命名录音', sourceName='lecture.webm'))
        self.assertNotIn('recordedAt', result)
        self.assertEqual(result['lessonSlot'], '')
        self.assertNotIn('周四', result['title'])

    def test_old_semester_cannot_inherit_current_timetable(self):
        result = naming.infer_fields(self.note(recordedAt='2025-09-10T16:35:38+08:00'))
        self.assertEqual(result['courseName'], '')
        self.assertEqual(result['lessonSlot'], '')
        self.assertEqual(result['title'], '驱动器与机器人')

    def test_manual_name_is_preserved_but_course_tag_can_be_added(self):
        result = naming.infer_fields(self.note(title='我自己的复习重点', nameSource='manual'))
        self.assertNotIn('title', result)
        self.assertEqual(result['nameSource'], 'manual')
        self.assertEqual(result['courseName'], '机器人驱动系统')
        self.assertNotIn('title', naming.infer_fields(self.note(title='历史自定义课名')))

    def test_initial_name_explicitly_auto_is_replaced(self):
        result = naming.infer_fields(self.note(title='今天的机器人课', nameSource='auto'))
        self.assertEqual(result['title'], '驱动器与机器人')

    def test_unfinished_or_stale_notes_are_not_automatically_retitled(self):
        for values in ({'summary': ''}, {'summaryStale': True}, {'isDemo': True}):
            result = naming.infer_fields(self.note(**values))
            self.assertNotIn('title', result)
            self.assertNotIn('courseColor', result)

    def test_topic_reuses_actual_heading_and_omits_generic_heading(self):
        summary = '# 课堂笔记\n\n## 机械制造基础：绪论\n切削加工包括车削和铣削。'
        result = naming.infer_fields(self.note(summary=summary, recordedAt='2026-09-09T08:05:00+08:00'))
        self.assertEqual(result['title'], '绪论')
        self.assertEqual(result['courseName'], '机械制造基础')
        self.assertEqual(result['lessonSlot'], '周三 12节')

    def test_period_conflict_does_not_assign_slot_from_topic_alone(self):
        result = naming.infer_fields(self.note(recordedAt='2026-09-09T21:00:00+08:00'))
        self.assertEqual(result['courseName'], '机器人驱动系统')
        self.assertEqual(result['lessonSlot'], '')

    def test_weak_content_without_a_period_match_does_not_invent_course(self):
        result = naming.infer_fields(self.note(summary='# 明天的计划\n准备买菜并整理房间。', recordedAt='2026-09-09T21:00:00+08:00'))
        self.assertEqual(result['courseName'], '')
        self.assertEqual(result['lessonSlot'], '')

    def test_confirmed_week_parity_is_respected(self):
        schedule = naming.load_schedule()
        schedule['week1Start'] = '2026-09-07'
        note = self.note(recordedAt='2026-09-09T14:05:00+08:00', summary='# 机器人建模与控制：逆运动学\n雅可比与DH参数。')
        self.assertEqual(naming.infer_fields(note, schedule)['lessonSlot'], '')
        note['recordedAt'] = '2026-09-16T14:05:00+08:00'
        self.assertEqual(naming.infer_fields(note, schedule)['lessonSlot'], '周三 56节')

    def test_calendar_makeup_day_uses_published_friday_timetable(self):
        note = self.note(recordedAt='2026-09-20T16:30:00+08:00', summary='# 机器人建模与控制：逆运动学\n雅可比与DH参数。')
        self.assertEqual(naming.infer_fields(note)['lessonSlot'], '周五 78节')

    def test_unknown_teaching_week_keeps_odd_week_inference_tentative(self):
        note = self.note(recordedAt='2026-09-11T14:00:00+08:00', summary='# 机械制造基础：切削加工\n车削、铣削与刀具。')
        inferred = naming.infer_fields(note)
        self.assertEqual(inferred['lessonSlot'], '周五 56节')
        self.assertLessEqual(inferred['namingConfidence'], 0.7)

    def test_undated_explicit_course_uses_its_only_weekly_slot_without_inventing_date(self):
        cases = [dict(title='环境工程原理', sourceName='环境工程原理.webm', summary='# 流体静力学\n压强和静水压力。'),
                 dict(title='未命名录音', sourceName='lecture.webm', summary='# 环境工程原理：流体静力学\n压强和静水压力。')]
        for values in cases:
            with self.subTest(values=values):
                result = naming.infer_fields(self.note(**values, nameSource='auto'))
                self.assertNotIn('recordedAt', result)
                self.assertEqual(result['title'], '流体静力学')
                self.assertEqual(result['courseName'], '环境工程原理')
                self.assertEqual(result['lessonSlot'], '周三 9–10节')
                self.assertEqual(result['namingConfidence'], 0.6)

    def test_download_names_match_title_and_stay_valid_on_windows(self):
        note = {'title': '驱动器与机器人'}
        self.assertEqual(naming.download_name(note, '.WAV'), note['title'] + '.wav')
        self.assertEqual(naming.download_name({'title': 'CON'}, '.txt'), '_CON.txt')
        self.assertEqual(naming.download_name({'title': '课:题/名'}, '/../wav'), '课_题_名.txt')

    def test_summary_completion_does_not_overwrite_a_concurrent_manual_rename(self):
        with tempfile.TemporaryDirectory(prefix='tingji-naming-') as temporary:
            folder = Path(temporary)
            with patch.object(storage, 'DATA', folder), patch.object(storage, 'DB', folder / 'notes.sqlite3'):
                storage.initialize()
                note = storage.create_note('录音 2026-09-09 16-35-38', '完整的原文',
                                           sourceName='录音 2026-09-09 16-35-38.webm', asrComplete=True)

                def summarize(*_args):
                    storage.update_note(note['id'], title='我的考前复习', nameSource='manual')
                    return DRIVE_SUMMARY

                with patch.object(providers, 'summarize', side_effect=summarize):
                    jobs.summarize(note['id'], {}, 'lecture', 'zh')
                saved = storage.get_note(note['id'])
                self.assertEqual(saved['title'], '我的考前复习')
                self.assertEqual(saved['courseName'], '机器人驱动系统')
                self.assertTrue(saved['transcriptPurgedAt'])
                revision = saved['revision']
                self.assertEqual(naming.apply_auto_name(note['id'])['revision'], revision)


if __name__ == '__main__':
    unittest.main()
