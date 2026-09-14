"""Isolated workflow queue and deletion orchestration tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tingji import jobs, storage, timetable, workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-workflow-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_patch = patch.object(storage, 'DATA', self.root)
        self.db_patch = patch.object(storage, 'DB', self.root / 'notes.sqlite3')
        self.active_patch = patch.object(jobs, 'ACTIVE', set())
        self.data_patch.start(); self.db_patch.start(); self.active_patch.start()
        self.addCleanup(self.active_patch.stop); self.addCleanup(self.db_patch.stop); self.addCleanup(self.data_patch.stop)
        storage.initialize()
        workflow.initialize()

    def note(self, title='录音', text='', **extra):
        values = {'status': 'ready' if text else 'idle', 'asrComplete': bool(text),
                  'audioFile': title + '.wav'}
        values.update(extra)
        (self.root / 'audio').mkdir(exist_ok=True)
        (self.root / 'audio' / values['audioFile']).write_bytes(b'audio')
        return storage.create_note(title, text, **values)

    def options(self, **extra):
        values = {'audioUploadConsent': True, 'template': 'lecture', 'language': 'zh'}
        values.update(extra)
        return values

    def test_batch_creation_is_idempotent_and_rejects_changed_replay(self):
        note = self.note('待处理')
        first = workflow.create_batch([note['id']], self.options(), 'batch-op-001')
        replay = workflow.create_batch([note['id']], self.options(), 'batch-op-001')
        self.assertEqual(replay, first)
        with self.assertRaisesRegex(ValueError, '不同内容'):
            workflow.create_batch([note['id']], self.options(template='meeting'), 'batch-op-001')

    def test_batch_options_reject_text_only_and_missing_consent(self):
        note = self.note('待处理')
        with self.assertRaisesRegex(ValueError, '只转文字'):
            workflow.create_batch([note['id']], self.options(autoSummarize=False), 'batch-op-002')
        with self.assertRaisesRegex(ValueError, '确认整理'):
            workflow.create_batch([note['id']], {}, 'batch-op-003')

    def test_tick_starts_at_most_limit_and_preserves_waiting_item(self):
        notes = [self.note(f'第{i}') for i in range(3)]
        batch = workflow.create_batch([item['id'] for item in notes], self.options(), 'batch-op-004')
        with patch.object(workflow, 'LIMIT', 2), patch.object(workflow, '_start_note', side_effect=lambda note, *_args: note) as start:
            workflow.tick()
        saved = workflow.get_batch(batch['id'])
        self.assertEqual([item['state'] for item in saved['items']], ['running', 'running', 'waiting'])
        self.assertEqual(start.call_count, 2)

    def test_tick_records_one_item_failure_and_continues_other_items(self):
        first, second = self.note('失败项'), self.note('成功项')
        batch = workflow.create_batch([first['id'], second['id']], self.options(), 'batch-op-005')
        with patch.object(workflow, '_start_note', side_effect=[ValueError('模拟提交失败'), second]):
            workflow.tick()
        saved = workflow.get_batch(batch['id'])
        self.assertEqual(saved['items'][0]['state'], 'failed')
        self.assertEqual(saved['items'][0]['reason'], '模拟提交失败')
        self.assertEqual(saved['items'][1]['state'], 'running')
        storage.update_note(second['id'], summary='已完成成稿', status='ready', summaryStale=False)
        workflow.tick()
        self.assertEqual(workflow.get_batch(batch['id'])['state'], 'complete')

    def test_unknown_submission_is_queried_once_and_never_resubmitted(self):
        note = self.note('状态不明', status='transcribing', asrComplete=False,
                         asrTask={'requestId': 'request-unknown', 'state': 'submit_unknown'})
        batch = workflow.create_batch([note['id']], self.options(), 'batch-op-006')
        with patch.object(workflow, '_start_note', wraps=workflow._start_note) as dispatch, \
                patch.object(jobs, 'start', return_value=note) as start:
            workflow.tick()
            workflow.tick()
        dispatch.assert_called_once()
        query_options = start.call_args.args[2]
        self.assertEqual(start.call_args.args[:2], (note['id'], 'query'))
        self.assertTrue(query_options['autoSummarize'])
        self.assertTrue(query_options['audioUploadConsent'])
        self.assertEqual(workflow.get_batch(batch['id'])['items'][0]['state'], 'running')

    def test_assign_lesson_requires_existing_course_and_persists_exact_occurrence(self):
        schedule = {
            'schemaVersion': 1, 'term': '2026秋季', 'timezone': 'Asia/Shanghai',
            'seasonStart': '2026-09-01', 'seasonEnd': '2027-01-31', 'week1Start': '2026-09-07',
            'periods': {'1': {'start': '08:00', 'end': '08:50'}}, 'dayOverrides': {},
            'courses': [{'id': 'control', 'name': '机器人控制', 'lessons': [
                {'id': 'control-w1', 'weekday': 1, 'periods': [1], 'weeks': [1, 16]}]}],
            'exceptions': []}
        timetable.save(schedule, base_revision=0)
        note = self.note('课次笔记', '课堂原文')
        saved = workflow.assign_lesson(note['id'], {'courseId': 'control', 'lessonId': 'control-w1', 'classDate': '2026-09-07'})
        self.assertEqual((saved['courseId'], saved['lessonId'], saved['classDate'], saved['courseName']),
                         ('control', 'control-w1', '2026-09-07', '机器人控制'))
        with self.assertRaisesRegex(ValueError, '课程已改变'):
            workflow.assign_lesson(note['id'], {'courseId': 'missing', 'lessonId': 'x', 'classDate': '2026-09-07'})
        cleared = workflow.assign_lesson(note['id'], None)
        self.assertEqual((cleared['courseId'], cleared['lessonId'], cleared['classDate']), ('', '', ''))


if __name__ == '__main__':
    unittest.main()
