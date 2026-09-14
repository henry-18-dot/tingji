"""Local group orchestration tests; provider calls are always mocked."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tingji import group_jobs, jobs, lifecycle, storage


class GroupJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tingji-group-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data_patch = patch.object(storage, 'DATA', self.root)
        self.db_patch = patch.object(storage, 'DB', self.root / 'notes.sqlite3')
        self.data_patch.start(); self.db_patch.start()
        self.addCleanup(self.db_patch.stop); self.addCleanup(self.data_patch.stop)
        storage.initialize()
        self.active_patch = patch.object(jobs, 'ACTIVE', set())
        self.active_patch.start(); self.addCleanup(self.active_patch.stop)
        # The lifecycle agent supplies the production helper. Keep these tests
        # runnable against the isolated module while that helper is staged.
        self.old_purge = getattr(lifecycle, 'purge_group_sources', None)
        self.purged = []
        lifecycle.purge_group_sources = lambda parent_id: self.purged.append(parent_id)
        self.addCleanup(self.restore_purge)
        group_jobs.GROUP_ACTIVE.clear()

    def restore_purge(self):
        if self.old_purge is None:
            delattr(lifecycle, 'purge_group_sources')
        else:
            lifecycle.purge_group_sources = self.old_purge

    def note(self, title, text='', **extra):
        values = {'status': 'ready' if text else 'idle', 'asrComplete': bool(text)}
        values.update(extra)
        return storage.create_note(title, text, **values)

    def test_create_group_preserves_order_links_and_operation_idempotency(self):
        first = self.note('第一段', '第一段原文')
        second = self.note('第二段', '第二段原文')
        created = group_jobs.create_group([first['id'], second['id']], '统一课堂', 'lesson-1', 'group-op-1')
        replay = group_jobs.create_group([first['id'], second['id']], '统一课堂', 'lesson-1', 'group-op-1')
        self.assertEqual(created['id'], replay['id'])
        self.assertEqual(storage.get_note(created['id'])['sourceNoteIds'], [first['id'], second['id']])
        self.assertEqual(storage.get_note(first['id'])['groupParentId'], created['id'])
        self.assertEqual(storage.get_note(second['id'])['groupParentId'], created['id'])
        with self.assertRaises(ValueError):
            group_jobs.create_group([second['id'], first['id']], '不同内容', operation_id='group-op-1')

    def test_manual_merge_is_deterministic_and_keeps_original_sources(self):
        first = self.note('A', '', summary='A 原文', status='ready', asrComplete=True)
        second = self.note('B', '', summary='B 原文', status='ready', asrComplete=True)
        merged = group_jobs.merge_notes([second['id'], first['id']], '手动合并', 'merge-op-1')
        self.assertEqual(merged['title'], '手动合并')
        self.assertEqual(merged['mergedNoteIds'], [second['id'], first['id']])
        self.assertLess(merged['summary'].index('B 原文'), merged['summary'].index('A 原文'))
        self.assertEqual(merged['transcript'], '')
        self.assertEqual(storage.get_note(first['id'])['summary'], 'A 原文')
        self.assertEqual(storage.get_note(second['id'])['summary'], 'B 原文')
        self.assertNotIn('groupParentId', storage.get_note(first['id']))
        self.assertNotIn('groupParentId', storage.get_note(second['id']))
        self.assertEqual(storage.get_note(merged['id'])['groupTask']['state'], 'merged')

    def test_process_transcribes_only_pending_source_then_requires_summary(self):
        first = self.note('已有', '已有原文')
        second = self.note('待转写', '', audioFile='second.wav', asrComplete=False)
        (self.root / 'audio' / 'second.wav').write_bytes(b'fixture')
        parent = group_jobs.create_group([first['id'], second['id']], '课堂')
        calls = []

        def fake_start(note_id, action, options):
            calls.append((note_id, action, options.copy()))
            storage.update_note(note_id, transcript='新来源原文', asrComplete=True, status='ready',
                                segments=[{'start': 0, 'end': 1, 'text': '新来源原文', 'speaker': ''}])
            return storage.get_note(note_id)

        def fake_summary(note_id, *_args):
            return storage.update_note(note_id, summary='统一成稿', status='ready')

        with patch.object(jobs, 'start', side_effect=fake_start), patch.object(jobs, 'summarize', side_effect=fake_summary):
            result = group_jobs.process_group(parent['id'])
        self.assertEqual([call[0] for call in calls], [second['id']])
        self.assertFalse(calls[0][2]['autoSummarize'])
        self.assertIn('已有原文', result['transcript'])
        self.assertIn('新来源原文', result['transcript'])
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['groupTask']['state'], 'completed')
        self.assertEqual(self.purged, [parent['id']])

    def test_unknown_charge_state_is_never_resubmitted(self):
        source = self.note('状态不明', '', audioFile='unknown.wav', asrComplete=False,
                           asrTask={'requestId': 'request-unknown', 'state': 'submit_unknown'})
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(jobs, 'start') as start:
            with self.assertRaises(ValueError):
                group_jobs.process_group(parent['id'], {'autoSummarize': False})
            result = group_jobs.process_group(parent['id'])
        start.assert_called_once_with(source['id'], 'query', {})
        self.assertEqual(result['status'], 'transcribing')
        self.assertEqual(result['groupTask']['sources'][0]['state'], 'waiting_query')

    def test_standard_query_backoff_and_pause_are_not_reset_by_group_tick(self):
        for state in ('queued', 'paused', 'not_found'):
            source = self.note(state, '', asrComplete=False, error='原任务需要检查',
                               asrTask={'requestId': 'original-id', 'state': state,
                                        'nextCheckAt': '2099-01-01T00:00:00+00:00' if state == 'queued' else None})
            parent = group_jobs.create_group([source['id']], '课堂')
            with patch.object(jobs, 'start') as start:
                result = group_jobs.process_group(parent['id'])
            start.assert_not_called()
            self.assertEqual(result['status'], 'transcribing' if state == 'queued' else 'error')
            if state != 'queued':
                self.assertIn('原任务需要检查', result['error'])
                self.assertEqual(group_jobs.group_public(result)['sources'][0]['nextAction'],
                                 'check-task' if state == 'not_found' else 'query')

    def test_failed_parent_automatically_finishes_after_source_completed_elsewhere(self):
        source = self.note('第一段', '', asrComplete=False)
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(jobs, 'start', side_effect=ValueError('缺少配置')):
            group_jobs.process_group(parent['id'])
        storage.update_note(source['id'], transcript='完整文字', asrComplete=True, status='ready')
        with patch.object(jobs, 'start') as start, patch.object(jobs, 'summarize', side_effect=
                lambda note_id, *_: storage.update_note(note_id, summary='统一整理', status='ready')) as summarize:
            result = group_jobs.tick(parent['id'])
            group_jobs.tick(parent['id'])
        self.assertEqual(result[0]['groupTask']['state'], 'completed')
        summarize.assert_called_once()
        start.assert_not_called()

    def test_upload_permission_survives_snapshot_and_source_error_is_actionable(self):
        self.assertTrue(group_jobs._options_snapshot({'cloudUploadConsent': True})['cloudUploadConsent'])
        source = self.note('第一段', '', asrComplete=False, sourceName='机器人建模与控制.m4a')
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(jobs, 'start', side_effect=ValueError('尚未允许自动上传到私有 TOS')):
            result = group_jobs.process_group(parent['id'])
        self.assertIn('机器人建模与控制.m4a：尚未允许自动上传', result['error'])
        self.assertIn('0/1', result['stage'])
        public = group_jobs.group_public(result)['sources'][0]
        self.assertEqual(public['state'], 'failed')
        self.assertEqual(public['nextAction'], 'configure')
        self.assertIn('自动上传', public['error'])

    def test_local_preparation_failure_and_empty_result_do_not_loop_submissions(self):
        for state in ('local_error', 'completed'):
            source = self.note('不可用音频', '', asrComplete=False, status='error',
                               asrTask={'state': state}, error='没有可用语音')
            parent = group_jobs.create_group([source['id']], '课堂')
            with patch.object(jobs, 'start') as start:
                result = group_jobs.process_group(parent['id'])
                group_jobs.tick(parent['id'])
            self.assertEqual(result['status'], 'error')
            self.assertIn('没有可用语音', result['error'])
            start.assert_not_called()

    def test_retry_failed_restarts_only_failed_source(self):
        good = self.note('成功', '成功原文')
        failed = self.note('失败', '', audioFile='failed.wav', asrComplete=False,
                           asrTask={'requestId': 'failed-request', 'state': 'failed'})
        parent = group_jobs.create_group([good['id'], failed['id']], '课堂')
        first = group_jobs.process_group(parent['id'])
        self.assertEqual(first['status'], 'error')
        calls = []

        def fake_start(note_id, action, options):
            calls.append((note_id, action, options.copy()))
            storage.update_note(note_id, transcript='补回原文', asrComplete=True, status='ready')
            return storage.get_note(note_id)

        def fake_summary(note_id, *_args):
            return storage.update_note(note_id, summary='统一成稿', status='ready')

        with patch.object(jobs, 'start', side_effect=fake_start), patch.object(jobs, 'summarize', side_effect=fake_summary):
            done = group_jobs.process_group(parent['id'], {'retryFailed': True})
        self.assertEqual([item[0] for item in calls], [failed['id']])
        self.assertTrue(calls[0][2]['resubmit'])
        self.assertEqual(calls[0][2]['replaceRequestId'], 'failed-request')
        self.assertEqual(done['groupTask']['state'], 'completed')

    def test_public_view_redacts_operation_id_and_exposes_source_states(self):
        source = self.note('来源', '原文')
        parent = group_jobs.create_group([source['id']], '课堂', operation_id='public-op-1')
        public = group_jobs.group_public(parent)
        self.assertEqual(public['sourceNoteIds'], [source['id']])
        self.assertEqual(public['sources'][0]['state'], 'completed')
        self.assertNotIn('operationId', public['groupTask'])
        self.assertNotIn('requestId', public['groupTask']['sources'][0])
        for key in ('transcript', 'segments', 'summary', 'chat'):
            self.assertNotIn(key, public['sources'][0])

    def test_recover_marks_interrupted_group_without_provider_call(self):
        source = self.note('来源', '')
        parent = group_jobs.create_group([source['id']], '课堂')
        storage.update_note(parent['id'], status='transcribing', groupTask={**parent['groupTask'], 'state': 'transcribing'})
        recovered = group_jobs.recover()
        self.assertEqual([item['id'] for item in recovered], [parent['id']])
        saved = storage.get_note(parent['id'])
        self.assertEqual(saved['groupTask']['state'], 'recovering')
        self.assertIn('可恢复', saved['stage'])

    def test_start_group_is_durable_and_duplicate_queue_is_idempotent(self):
        source = self.note('来源', '原文')
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(group_jobs.GROUP_POOL, 'submit', return_value=Mock()) as submit:
            queued = group_jobs.start_group(parent['id'])
            replay = group_jobs.start_group(parent['id'])
        self.assertEqual(queued['status'], 'transcribing')
        self.assertEqual(replay['id'], parent['id'])
        submit.assert_called_once()
        self.assertEqual(storage.get_note(parent['id'])['groupTask']['state'], 'transcribing')
        group_jobs.GROUP_ACTIVE.discard(parent['id'])

    def test_scheduler_does_not_activate_an_idle_group(self):
        source = self.note('尚未启动', '', asrComplete=False)
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(jobs, 'start') as start:
            self.assertEqual(group_jobs.tick(), [])
        start.assert_not_called()
        self.assertEqual(storage.get_note(parent['id'])['groupTask']['state'], 'idle')

    def test_public_group_rejects_text_only(self):
        source = self.note('待转写', '', asrComplete=False)
        parent = group_jobs.create_group([source['id']], '课堂')
        with self.assertRaises(ValueError):
            group_jobs.start_group(parent['id'], {'autoSummarize': False})
        with self.assertRaises(ValueError):
            group_jobs.process_group(parent['id'], {'autoSummarize': False})

    def test_config_start_failure_is_terminal_and_retry_is_one_explicit_pass(self):
        source = self.note('需配置', '', asrComplete=False)
        parent = group_jobs.create_group([source['id']], '课堂')
        with patch.object(jobs, 'start', side_effect=ValueError('缺少配置')) as start:
            failed = group_jobs.process_group(parent['id'])
            self.assertEqual(failed['status'], 'error')
            self.assertEqual(failed['groupTask']['sources'][0]['state'], 'failed')
            self.assertEqual(failed['groupTask']['state'], 'error')
            # Scheduler recovery without explicit retry must not resubmit.
            self.assertEqual(group_jobs.tick(parent['id']), [])
            self.assertEqual(start.call_count, 1)
            retried = group_jobs.process_group(parent['id'], {'retryFailed': True})
            self.assertEqual(retried['groupTask']['sources'][0]['retryCount'], 1)
            self.assertTrue(retried['groupTask']['processOptions']['retryFailed'])
            self.assertEqual(start.call_count, 2)
            # The next tick sees the failed entry and stops again.
            self.assertEqual(group_jobs.tick(parent['id']), [])
            self.assertEqual(start.call_count, 2)

    def test_group_public_only_exposes_audio_source_fields_when_file_exists(self):
        source = self.note('音频来源', '原文', audioFile='source.wav')
        audio_dir = self.root / 'audio'
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / 'source.wav').write_bytes(b'fixture')
        parent = group_jobs.create_group([source['id']], '课堂')
        item = group_jobs.group_public(parent)['sources'][0]
        self.assertTrue(item['audioAvailable'])
        self.assertEqual(item['sourceName'], source['sourceName'])
        self.assertEqual(item['audioUrl'], '/api/audio/' + source['id'])
        self.assertNotIn('text', item)

    def test_summary_commit_then_cleanup_failure_recovers_without_resummarizing(self):
        source = self.note('来源', '来源原文')
        parent = group_jobs.create_group([source['id']], '课堂')
        summary = Mock(side_effect=lambda note_id, *_args: storage.update_note(note_id, summary='统一成稿', status='ready'))
        purge = Mock(side_effect=[RuntimeError('暂时无法清理'), None])
        with patch.object(jobs, 'summarize', side_effect=summary), patch.object(lifecycle, 'purge_group_sources', purge):
            failed = group_jobs.process_group(parent['id'])
            self.assertEqual(failed['groupTask']['state'], 'summary_cleanup_failed')
            recovered = group_jobs.recover()
            self.assertEqual(recovered[0]['groupTask']['state'], 'summary_cleanup_failed')
            done = group_jobs.tick(parent['id'])
        self.assertEqual(done[0]['groupTask']['state'], 'completed')
        summary.assert_called_once()
        self.assertEqual(purge.call_count, 2)

    def test_recover_summary_phase_without_commit_waits_for_explicit_retry(self):
        source = self.note('来源', '来源原文')
        parent = group_jobs.create_group([source['id']], '课堂')
        storage.update_note(parent['id'], status='summarizing',
                            groupTask={**parent['groupTask'], 'state': 'summarizing'})
        with patch.object(jobs, 'summarize') as summarize:
            recovered = group_jobs.recover()
            self.assertEqual(recovered[0]['groupTask']['state'], 'summary_failed')
            self.assertEqual(group_jobs.tick(parent['id']), [])
            summarize.assert_not_called()

    def test_manual_merge_requires_fresh_nonbusy_summaries(self):
        stale = self.note('旧稿', '', summary='旧成稿', status='ready', asrComplete=True,
                          summaryStale=True)
        busy = self.note('处理中', '', summary='临时成稿', status='summarizing', asrComplete=True)
        for source in (stale, busy):
            with self.assertRaises(ValueError):
                group_jobs.merge_notes([source['id']], '非法合并')

    def test_manual_merge_inherits_only_consistent_course_metadata_and_rejects_nested_parent(self):
        first = self.note('A', '', summary='A 原文', status='ready', asrComplete=True,
                          lessonId='L1', courseId='C1', classDate='2026-09-10', courseName='机器人')
        second = self.note('B', '', summary='B 原文', status='ready', asrComplete=True,
                           lessonId='L1', courseId='C1', classDate='2026-09-11', courseName='机器人')
        merged = group_jobs.merge_notes([first['id'], second['id']], '合并')
        self.assertEqual(merged['lessonId'], 'L1')
        self.assertEqual(merged['courseId'], 'C1')
        self.assertEqual(merged['courseName'], '机器人')
        self.assertEqual(merged['classDate'], '')
        with self.assertRaises(ValueError):
            group_jobs.merge_notes([merged['id'], first['id']], '套叠')

    def test_completed_audio_group_can_be_manual_merge_source(self):
        first = self.note('组一段一', '一段原文')
        second = self.note('组一段二', '二段原文')
        grouped = group_jobs.create_group([first['id'], second['id']], '整课')

        def fake_summary(note_id, *_args):
            return storage.update_note(note_id, summary='整课已成稿', status='ready')

        with patch.object(jobs, 'summarize', side_effect=fake_summary):
            completed = group_jobs.process_group(grouped['id'])
        other = self.note('补充笔记', '', summary='补充原文', status='ready', asrComplete=True)
        merged = group_jobs.merge_notes([completed['id'], other['id']], '手动排序')
        self.assertEqual(merged['mergedNoteIds'], [completed['id'], other['id']])
        self.assertIn('整课已成稿', merged['summary'])
        self.assertIn('补充原文', merged['summary'])
        self.assertEqual(merged['transcript'], '')
        self.assertEqual(storage.get_note(completed['id'])['groupTask']['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
