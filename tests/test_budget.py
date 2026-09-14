"""Local cost-control boundary tests; no real account or network is accessed."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tingji import budget, group_jobs, jobs, providers, standard_jobs, storage


class BudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tingji-budget-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current = datetime(2026, 9, 12, 23, 59, 59, tzinfo=budget.LOCAL_ZONE)
        for patcher in (patch.object(storage, 'DATA', self.root),
                        patch.object(storage, 'DB', self.root / 'notes.sqlite3'),
                        patch.object(budget, '_clock', side_effect=lambda: self.current),
                        patch.object(jobs, 'ACTIVE', set())):
            patcher.start()
            self.addCleanup(patcher.stop)
        storage.initialize()
        group_jobs.GROUP_ACTIVE.clear()
        self.addCleanup(group_jobs.GROUP_ACTIVE.clear)
        # Forbid accidental network; individual tests replace only the exact
        # provider operation whose response they exercise.
        for target, method in ((providers, '_asr_post'), (providers, 'post_json')):
            patcher = patch.object(target, method, side_effect=AssertionError('unexpected network'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def limit(self, value):
        with storage.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('dailyBudgetYuan',?)", (json.dumps(value),))

    def test_reservation_is_idempotent_persistent_and_resets_at_shanghai_midnight(self):
        self.assertEqual(budget.status()['dailyBudgetYuan'], 10)
        budget.reserve('one', 8)
        budget.reserve('one', 8)
        with self.assertRaises(budget.BudgetExceeded) as caught:
            budget.reserve('two', 3)
        self.assertEqual(caught.exception.resetsAt, '2026-09-13T00:00:00+08:00')
        storage.initialize()
        self.assertEqual(budget.status()['usedYuan'], 8)
        self.current += timedelta(seconds=1)
        self.assertEqual(budget.status()['usedYuan'], 0)
        budget.reserve('two', 3)
        self.assertEqual(budget.status()['remainingYuan'], 7)

    def test_parallel_reservations_cannot_exceed_limit(self):
        self.limit(1)
        def allocate(index):
            try:
                budget.reserve('parallel:' + str(index), 0.6)
                return True
            except budget.BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(allocate, range(6)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(budget.status()['usedYuan'], 0.6)

    def test_only_unsent_reservations_can_be_released(self):
        budget.reserve('unsent', 2)
        budget.reserve('unknown', 3)
        budget.dispatch('unknown')
        budget.release('unsent')
        budget.release('unknown')
        self.assertEqual(budget.status()['usedYuan'], 3)
        with self.assertRaises(budget.RequestUncertain):
            budget.dispatch('unknown')
        self.assertEqual(budget.get('unknown')['state'], 'dispatched')

    def test_old_unsent_reservation_cannot_bypass_next_day_limit(self):
        budget.reserve('late', 7)
        self.current += timedelta(seconds=1)
        budget.reserve('early', 5)
        with self.assertRaises(budget.BudgetExceeded):
            budget.dispatch('late')
        self.assertEqual(budget.get('late')['state'], 'reserved')
        self.assertEqual(budget.status()['usedYuan'], 5)

    def test_twenty_yuan_single_request_is_blocked_even_with_large_daily_limit(self):
        self.limit(100)
        for amount in (20, 100):
            with self.assertRaisesRegex(budget.BudgetExceeded, '单次预留'):
                budget.reserve('large:' + str(amount), amount)
        self.assertEqual(budget.status()['usedYuan'], 0)

    def test_lowered_limit_stops_reserved_but_unsent_request(self):
        budget.reserve('not-yet-sent', 2)
        self.limit(1)
        with self.assertRaises(budget.BudgetExceeded):
            budget.dispatch('not-yet-sent')
        self.assertEqual(budget.get('not-yet-sent')['state'], 'reserved')

    def test_estimators_round_audio_up_and_reject_unknown_models_and_multimodal_inputs(self):
        self.assertEqual(budget.estimate_asr(3600), 1)
        self.assertGreater(budget.estimate_asr(3600.01), 1)
        for duration in (0, -1, float('inf'), None):
            with self.assertRaises(ValueError):
                budget.estimate_asr(duration)
        for model in ('future-model', 'deepseek-chat'):
            with self.assertRaises(ValueError):
                budget.estimate_deepseek([], model, 10)
        with self.assertRaises(ValueError):
            budget.estimate_deepseek([{'content': [{'type': 'image_url'}]}], 'deepseek-flash', 10)
        self.assertGreater(budget.estimate_deepseek([{'role': 'user', 'content': '中文'}], 'deepseek-flash', 8192), 8192 * 8 / 1e6)

    def response(self, text='已整理'):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110,
                          'secret': 'do-not-save'}}

    def test_deepseek_result_and_usage_replay_without_a_second_network_call(self):
        config = {'deepseekApiKey': 'SECRET-KEY', '_budgetScope': 'same-note'}
        with patch.object(providers, 'post_json', return_value=(self.response(), {})) as send:
            first = providers.deepseek([{'role': 'user', 'content': 'PRIVATE-INPUT'}], config)
            self.current += timedelta(seconds=1)
            second = providers.deepseek([{'role': 'user', 'content': 'PRIVATE-INPUT'}], config)
        self.assertEqual(first, second)
        send.assert_called_once()
        with storage.connect() as db:
            row = db.execute('SELECT usage,result FROM budget_requests').fetchone()
            serialized = '\n'.join(str(value) for value in db.execute('SELECT * FROM budget_requests').fetchone())
        self.assertEqual(json.loads(row[0])['total_tokens'], 110)
        for secret in ('SECRET-KEY', 'PRIVATE-INPUT', 'do-not-save'):
            self.assertNotIn(secret, serialized)

    def test_unknown_deepseek_result_keeps_allowance_and_never_repeats_same_request(self):
        config = {'deepseekApiKey': 'mock', '_budgetScope': 'same-note'}
        with patch.object(providers, 'post_json', side_effect=TimeoutError) as send:
            with self.assertRaises(TimeoutError):
                providers.deepseek([], config)
            with self.assertRaises(budget.RequestUncertain):
                providers.deepseek([], config)
        send.assert_called_once()
        self.assertGreater(budget.status()['usedYuan'], 0)

    def test_exhausted_deepseek_substep_runs_next_day_and_reuses_prior_substep(self):
        config = {'deepseekApiKey': 'mock', '_budgetScope': 'same-note'}
        first = [{'role': 'user', 'content': '第一段'}]
        second = [{'role': 'user', 'content': '第二段'}]
        amount = budget.estimate_deepseek(first, 'deepseek-flash', 100)
        self.limit(amount)
        with patch.object(providers, 'post_json', return_value=(self.response(), {})) as send:
            providers.deepseek(first, config, 100)
            with self.assertRaises(budget.BudgetExceeded):
                providers.deepseek(second, config, 100)
            self.assertEqual(send.call_count, 1)
            self.current += timedelta(seconds=1)
            providers.deepseek(first, config, 100)
            providers.deepseek(second, config, 100)
            self.assertEqual(send.call_count, 2)

    def test_zero_budget_blocks_asr_submission_but_not_query(self):
        self.limit(0)
        config = {'asrApiKey': 'mock', '_budgetDurationSeconds': 90}
        with patch.object(providers, '_asr_post', return_value=(200, {'X-Api-Status-Code': '20000002'}, {})) as send:
            with self.assertRaises(budget.BudgetExceeded):
                providers.submit_transcription('https://example.com/audio.mp3', config, 'original-id')
            result = providers.query_transcription(config, 'old-paid-task')
        self.assertEqual(result['state'], 'queued')
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], providers.ASR_QUERY_URL)

    def test_standard_worker_budget_pause_never_uploads_and_sets_resumable_note(self):
        from tingji import object_storage
        self.limit(0)
        audio = self.root / 'audio' / 'sample.mp3'
        audio.write_bytes(b'mocked audio')
        note = storage.create_note('录音', '', audioFile=audio.name, duration=90,
                                   asrTask={'attemptId': 'attempt', 'state': 'preparing'})
        with patch.object(standard_jobs, 'prepare_audio', return_value=audio), \
                patch.object(object_storage, 'prepare_and_upload') as upload:
            try:
                standard_jobs.submit(note['id'], {})
            except budget.BudgetExceeded as exc:
                standard_jobs.handle_unexpected(note['id'], exc)
        saved = storage.get_note(note['id'])
        self.assertEqual(saved['asrTask']['state'], 'budget_wait')
        self.assertEqual(saved['status'], 'idle')
        self.assertEqual(saved['autoProcessState'], 'waiting')
        self.assertEqual(saved['budgetWaitUntil'], '2026-09-13T00:00:00+08:00')
        self.assertFalse(saved['asrTask'].get('requestId'))
        upload.assert_not_called()

    def test_group_budget_pause_resumes_next_day_without_manual_retry(self):
        source = storage.create_note('第一段', '', asrComplete=False)
        parent = group_jobs.create_group([source['id']], '整课')
        def fake_start(note_id, *_):
            return storage.update_note(note_id, asrTask={'state': 'budget_wait'}, status='idle',
                                       budgetWaitUntil='2026-09-13T00:00:00+08:00')
        with patch.object(jobs, 'start', side_effect=fake_start):
            result = group_jobs.process_group(parent['id'])
        self.assertEqual(result['groupTask']['state'], 'waiting_budget')
        with patch.object(standard_jobs, 'elapsed', return_value=-1), patch.object(jobs, 'start') as send:
            self.assertEqual(group_jobs.tick(parent['id']), [])
            send.assert_not_called()
        self.current += timedelta(seconds=1)
        with patch.object(standard_jobs, 'elapsed', return_value=1), \
                patch.object(jobs, 'start', side_effect=lambda note_id, *_: storage.update_note(
                    note_id, asrComplete=True, transcript='完整原文', status='ready')) as start, \
                patch.object(jobs, 'summarize', side_effect=lambda note_id, *_: storage.update_note(
                    note_id, summary='统一成稿', status='ready')), \
                patch.object(group_jobs.lifecycle, 'purge_group_sources'):
            result = group_jobs.tick(parent['id'])
        self.assertEqual(result[0]['groupTask']['state'], 'completed')
        start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
