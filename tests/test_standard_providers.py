"""Standard ASR contract tests: all HTTP operations are replaced locally."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from tingji import providers, storage


class StandardAsrTests(unittest.TestCase):
    config = {'asrApiKey': 'mock-speech-key', 'asrResourceId': 'volc.bigasr.auc_turbo', '_budgetDurationSeconds': 120}
    task_id = '12345678-1234-1234-1234-123456789abc'
    audio_url = 'https://private-audio.tos-cn-beijing.volces.com/audio.mp3?signature=mock'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tingji-asr-provider-budget-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, value in (('DATA', root), ('DB', root / 'notes.sqlite3')):
            patcher = patch.object(storage, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        storage.initialize()

    def reply(self, code, body=None, http=200, message='OK'):
        return http, {'X-Api-Status-Code': code, 'X-Api-Message': message, 'X-Tt-Logid': 'safe-log-id'}, body or {}

    def test_resource_is_forced_to_standard_2_even_with_stale_setting(self):
        headers = providers.asr_headers(self.config, self.task_id)
        self.assertEqual(headers['X-Api-Resource-Id'], 'volc.seedasr.auc')
        self.assertEqual(headers['X-Api-Key'], 'mock-speech-key')
        self.assertNotIn('X-Api-App-Key', headers)
        legacy = providers.asr_headers({'asrAppId': 'app', 'asrAccessToken': 'token'}, self.task_id)
        self.assertEqual(legacy['X-Api-App-Key'], 'app')
        self.assertNotIn('X-Api-Key', legacy)

    def test_empty_ack_keeps_original_id_and_one_request(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('20000000')) as transport:
            ack = providers.submit_transcription(self.audio_url, self.config, self.task_id, audio_format='mp3')
        self.assertEqual(ack['state'], 'accepted')
        self.assertEqual(ack['task_id'], self.task_id)
        self.assertEqual(transport.call_count, 1)
        url, body, headers = transport.call_args.args
        self.assertEqual(url, providers.ASR_SUBMIT_URL)
        self.assertEqual(body['audio'], {'url': self.audio_url, 'format': 'mp3'})
        self.assertEqual(headers['X-Api-Request-Id'], self.task_id)
        self.assertTrue(body['request']['show_utterances'])
        self.assertEqual(body['request']['ssd_version'], '300')
        self.assertFalse(body['request']['enable_ddc'])
        self.assertNotIn('data', body['audio'])

    def test_new_ack_returns_server_task_id_without_losing_original_id(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('20000000', {'task_id': 'server-task'})):
            ack = providers.submit_transcription(self.audio_url, self.config, self.task_id)
        self.assertEqual(ack['request_id'], self.task_id)
        self.assertEqual(ack['task_id'], 'server-task')

    def test_body_status_example_and_case_insensitive_headers_are_supported(self):
        response = (200, {}, {'task_id': 'server-task', 'X-Api-Status-Code': '20000000', 'X-Tt-Logid': 'valid-log'})
        with patch.object(providers, '_asr_post', return_value=response):
            self.assertEqual(providers.submit_transcription(self.audio_url, self.config, self.task_id)['task_id'], 'server-task')
        with patch.object(providers, '_asr_post', return_value=(200, {'x-api-status-code': '20000002'}, {})):
            self.assertEqual(providers.query_transcription(self.config, self.task_id)['state'], 'queued')

    def test_english_disables_unsupported_speaker_mode_and_preserves_hotword_json(self):
        config = {**self.config, 'hotwords': 'PID，机器人;PID'}
        with patch.object(providers, '_asr_post', return_value=self.reply('20000000')) as transport:
            providers.submit_transcription(self.audio_url, config, self.task_id, 'en')
        body = transport.call_args.args[1]
        self.assertEqual(body['audio']['language'], 'en-US')
        self.assertFalse(body['request']['enable_speaker_info'])
        self.assertNotIn('ssd_version', body['request'])
        self.assertNotIn('enable_auto_lang', body['request'])
        self.assertEqual(json.loads(body['request']['corpus']['context']), {'hotwords': [{'word': 'PID'}, {'word': '机器人'}]})

    def test_rejects_nonpublic_url_and_undocumented_flac_before_network(self):
        with patch.object(providers, '_asr_post') as transport:
            for url in ('file:///audio.wav', 'data:audio/wav;base64,mock', 'http://127.0.0.1/a', 'http://192.168.1.1/a', 'http://localhost/a'):
                with self.subTest(url=url), self.assertRaises(providers.ProviderError):
                    providers.submit_transcription(url, self.config, self.task_id)
            with self.assertRaises(providers.ProviderError):
                providers.submit_transcription(self.audio_url, self.config, self.task_id, audio_format='flac')
            transport.assert_not_called()

    def test_network_loss_is_uncertain_and_is_never_retried(self):
        with patch.object(providers, '_asr_post', side_effect=TimeoutError) as transport:
            with self.assertRaises(providers.AsrSubmitUncertain) as caught:
                providers.submit_transcription(self.audio_url, self.config, self.task_id)
        self.assertEqual(caught.exception.request_id, self.task_id)
        self.assertEqual(transport.call_count, 1)

    def test_submit_500_and_unrecognized_status_are_uncertain(self):
        for response in (self.reply('55000031', http=500), self.reply('unexpected'), (200, {}, None), self.reply('20000000', http=503)):
            with self.subTest(response=response), patch.object(providers, '_asr_post', return_value=response):
                with self.assertRaises(providers.AsrSubmitUncertain):
                    providers.submit_transcription(self.audio_url, self.config, self.task_id)

    def test_explicit_rejection_is_distinct_from_duplicate(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('45000151', message='bad format')):
            with self.assertRaises(providers.AsrSubmitRejected) as caught:
                providers.submit_transcription(self.audio_url, self.config, self.task_id)
        self.assertEqual(caught.exception.code, '45000151')
        self.assertEqual(caught.exception.log_id, 'safe-log-id')
        with patch.object(providers, '_asr_post', return_value=self.reply('45000001', message='duplicate request')):
            with self.assertRaises(providers.AsrSubmitUncertain):
                providers.submit_transcription(self.audio_url, self.config, self.task_id)

    def test_query_uses_exact_task_id_and_empty_body(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('20000002')) as transport:
            answer = providers.query_transcription(self.config, 'returned-server-task')
        self.assertEqual(answer['state'], 'queued')
        url, body, headers = transport.call_args.args
        self.assertEqual(url, providers.ASR_QUERY_URL)
        self.assertEqual(body, {})
        self.assertEqual(headers['X-Api-Request-Id'], 'returned-server-task')
        self.assertNotIn('X-Api-Sequence', headers)

    def test_query_distinguishes_processing_and_silence(self):
        for code, state in [('20000001', 'processing'), ('20000003', 'completed')]:
            with self.subTest(code=code), patch.object(providers, '_asr_post', return_value=self.reply(code)):
                answer = providers.query_transcription(self.config, self.task_id)
                self.assertEqual(answer['state'], state)
                if code == '20000003':
                    self.assertEqual(answer['result'], {'text': '', 'utterances': [], 'silent': True})

    def test_complete_preserves_words_speakers_and_audio_duration(self):
        result = {'text': '控制器。', 'utterances': [{'text': '控制器。', 'start_time': 100, 'end_time': 2200, 'additions': {'speaker': '2'}, 'words': [{'text': '控制器', 'start_time': 100, 'end_time': 2200}]}]}
        response = self.reply('20000000', {'result': result, 'audio_info': {'duration': 2500}})
        with patch.object(providers, '_asr_post', return_value=response):
            answer = providers.query_transcription(self.config, self.task_id)
        self.assertEqual(answer['result'], result)
        self.assertEqual(answer['audio_info']['duration'], 2500)

    def test_completed_but_unreadable_result_only_requeries(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('20000000')):
            with self.assertRaises(providers.AsrQueryError) as caught:
                providers.query_transcription(self.config, self.task_id)
        self.assertTrue(caught.exception.retryable)

    def test_query_invalid_parameters_are_not_a_terminal_task_failure(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('45000001', message='invalid request')):
            with self.assertRaises(providers.AsrQueryError) as caught:
                providers.query_transcription(self.config, self.task_id)
        self.assertFalse(caught.exception.retryable)

    def test_only_explicit_missing_task_is_not_found_and_secret_message_is_not_exposed(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('45000001', message='task not found secret-key https://private/audio')):
            answer = providers.query_transcription(self.config, self.task_id)
        self.assertEqual(answer['state'], 'not_found')
        self.assertNotIn('secret-key', str(answer))
        self.assertNotIn('https://private', str(answer))
        with patch.object(providers, '_asr_post', return_value=(404, {}, {})):
            with self.assertRaises(providers.AsrQueryError):
                providers.query_transcription(self.config, self.task_id)

    def test_query_transient_failure_is_not_resubmission(self):
        with patch.object(providers, '_asr_post', return_value=self.reply('55000031')) as transport:
            with self.assertRaises(providers.AsrQueryError) as caught:
                providers.query_transcription(self.config, self.task_id)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], providers.ASR_QUERY_URL)

    def test_actual_cannot_find_task_reply_preserves_query_identity_without_resubmission(self):
        # Read-only diagnostic query observed on 2026-09-09; no recording was submitted.
        response = self.reply('45000000', message='[Client-side generic error] OperatorWrapper Process failed: cannot find task')
        with patch.object(providers, '_asr_post', return_value=response) as transport:
            answer = providers.query_transcription(self.config, self.task_id)
        self.assertEqual(answer['state'], 'not_found')
        self.assertEqual(answer['code'], '45000000')
        self.assertEqual(answer['request_id'], self.task_id)
        self.assertIn('不会自动重新提交', answer['message'])
        self.assertEqual(transport.call_count, 1)
        url, body, headers = transport.call_args.args
        self.assertEqual(url, providers.ASR_QUERY_URL)
        self.assertEqual(body, {})
        self.assertEqual(headers['X-Api-Request-Id'], self.task_id)

    def test_generic_error_code_alone_does_not_establish_missing_task(self):
        response = self.reply('45000000', message='[Client-side generic error] OperatorWrapper Process failed')
        with patch.object(providers, '_asr_post', return_value=response) as transport:
            with self.assertRaises(providers.AsrQueryError) as caught:
                providers.query_transcription(self.config, self.task_id)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], providers.ASR_QUERY_URL)

    def test_http_parser_accepts_empty_ack_without_losing_headers(self):
        class Response(io.BytesIO):
            headers = {'X-Api-Status-Code': '20000000'}
            def getcode(self):
                return 200
        with patch.object(providers.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = Response(b'')
            status, headers, body = providers._asr_post(providers.ASR_SUBMIT_URL, {}, {})
        self.assertEqual((status, body), (200, {}))
        self.assertEqual(headers['X-Api-Status-Code'], '20000000')

    def test_obsolete_flash_entry_cannot_send_audio(self):
        with patch.object(providers, '_asr_post') as transport:
            with self.assertRaises(providers.ProviderError):
                providers.transcribe_file('irrelevant.wav', self.config)
            transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
