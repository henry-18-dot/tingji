"""Provider contract and failure tests, with all network calls replaced locally."""

import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from tingji import providers, storage


def study_document(title, body, term):
    """A complete v5 reply for tests concerned with the provider request contract."""
    body = body.replace(term, f'[{term}](concept:topic)', 1)
    metadata = {'version': 5, 'concepts': [{
        'id': 'topic', 'term': term, 'question': f'{term}是什么？', 'links': []}]}
    return (f'# {title}\n\n{body}\n\n## 继续探索\n'
            '- 这个概念最初解决什么问题？\n'
            '- 哪些人推动了它的发展？\n'
            '- 它怎样影响日常生活？\n\n```study\n'
            + json.dumps(metadata, ensure_ascii=False) + '\n```')


class ProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tingji-provider-budget-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for target, value in (('DATA', root), ('DB', root / 'notes.sqlite3')):
            patcher = patch.object(storage, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        storage.initialize()

    def test_asr_auth_uses_selected_key_without_mixing_legacy_credentials(self):
        api = providers.asr_headers({"asrApiKey": "test-key", "asrAppId": "app", "asrAccessToken": "token"}, "test-request")
        self.assertEqual(api["X-Api-Key"], "test-key")
        self.assertNotIn("X-Api-App-Key", api)
        self.assertNotIn("X-Api-Access-Key", api)
        legacy = providers.asr_headers({"asrAppId": "app", "asrAccessToken": "token"})
        self.assertEqual(legacy["X-Api-App-Key"], "app")
        self.assertNotIn("X-Api-Key", legacy)
        with self.assertRaises(providers.ProviderError):
            providers.asr_headers({})

    def test_legacy_direct_file_call_cannot_invoke_flash(self):
        with patch.object(providers, "_asr_post") as send:
            with self.assertRaises(providers.ProviderError):
                providers.transcribe_file(Path("unused.wav"), {"asrApiKey": "test-key"})
            send.assert_not_called()

    def test_http_errors_do_not_expose_provider_body_or_credentials(self):
        import io
        error = urllib.error.HTTPError(providers.DEEPSEEK_URL, 401, "Unauthorized", {}, io.BytesIO(b"secret-user-recording-and-api-key"))
        with patch.object(providers.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.post_json(providers.DEEPSEEK_URL, {}, {"Authorization": "Bearer secret"})
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_truncated_ai_output_is_not_returned_as_completed_summary(self):
        output = {"choices": [{"finish_reason": "length", "message": {"content": "incomplete"}}]}
        with patch.object(providers, "post_json", return_value=(output, {})):
            with self.assertRaisesRegex(providers.ProviderError, "长度上限"):
                providers.deepseek([], {"deepseekApiKey": "test-key"})

    def test_knowledge_request_carries_current_shared_prompt(self):
        source = "力矩 τ = F × r，单位 N·m；此处只讨论平面静力问题。"
        document = study_document('平面静力', '力矩 τ = F × r，单位 N·m。', '力矩')
        output = {"choices": [{"finish_reason": "stop", "message": {"content": document}}]}
        approved = Path(__file__).resolve().parent.parent / 'docs' / 'note-standard-v5' / 'deepseek-system.md'
        self.assertEqual(providers.LECTURE_PROMPT_PATH, approved)
        standard = approved.read_text(encoding="utf-8-sig").strip()
        self.assertIn('阅读协议：study-v5', standard)
        for template in ("general", "lecture"):
            with self.subTest(template=template), patch.object(
                    providers, "post_json", return_value=(output, {})) as send:
                result = providers.summarize(source, {"deepseekApiKey": "test-key"}, template, context={"note_id": template})
            send.assert_called_once()
            url, body, _ = send.call_args.args
            self.assertEqual(url, providers.DEEPSEEK_URL)
            system, material = body["messages"]
            self.assertTrue(system["content"].startswith(standard))
            self.assertTrue(material["content"].endswith("<source>\n" + source + "\n</source>"))
            self.assertIn("来源S1", material["content"])
            self.assertNotIn(providers.BASE_PROMPT, system["content"])
            self.assertEqual(result, output["choices"][0]["message"]["content"])

    def test_missing_or_empty_knowledge_standard_never_calls_paid_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            prompt = Path(temporary) / 'standard.md'
            for template in ('general', 'lecture'):
                for value in (None, ''):
                    if prompt.exists():
                        prompt.unlink()
                    if value is not None:
                        prompt.write_text(value, encoding='utf-8')
                    with self.subTest(template=template, value=value), \
                            patch.object(providers, 'LECTURE_PROMPT_PATH', prompt), \
                            patch.object(providers, 'deepseek') as send:
                        with self.assertRaisesRegex(providers.ProviderError, '课堂整理规范'):
                            providers.summarize('原文', {}, template)
                        send.assert_not_called()

    def test_lecture_context_is_allowlisted_and_not_used_as_instructions(self):
        document = study_document('知识标题', '控制根据反馈调整动作。', '控制')
        with patch.object(providers, 'deepseek', return_value=document) as send:
            providers.summarize('[00:03] 原有题目：为什么？老师没有回答。', {}, 'lecture', 'en',
                                context={'note_id': 'note-1', 'recording_date': '2026-09-10',
                                         'course_candidate': '环境工程原理', 'deepseekApiKey': 'SECRET',
                                         'source_name': 'lecture.wav', 'createdAt': '2099-01-01'})
        messages = send.call_args.args[0]
        self.assertIn('输出语言：英文', messages[0]['content'])
        self.assertIn('课程候选未经确认', messages[1]['content'])
        self.assertIn('note-1', messages[1]['content'])
        self.assertIn('[00:03]', messages[1]['content'])
        self.assertNotIn('SECRET', str(messages))
        self.assertNotIn('2099-01-01', str(messages))

    def test_meeting_and_interview_keep_their_existing_contract(self):
        for template in ('meeting', 'interview'):
            with patch.object(providers, 'LECTURE_PROMPT_PATH', Path('missing-prompt')), \
                    patch.object(providers, 'deepseek', return_value='正文') as send:
                providers.summarize('原有讨论', {}, template)
            system, material = send.call_args.args[0]
            self.assertEqual(system['content'], providers.BASE_PROMPT +
                             '\n输出语言：中文（原文中的英文专名和术语保留）。\n' + providers.TEMPLATES[template])
            self.assertEqual(material['content'], '<source>\n原有讨论\n</source>')

    def test_knowledge_templates_reject_an_invalid_final_document(self):
        for template in ('general', 'lecture'):
            with self.subTest(template=template), patch.object(
                    providers, 'deepseek', return_value='缺少一级标题'):
                with self.assertRaisesRegex(providers.ProviderError, '知识笔记格式未通过检查'):
                    providers.summarize('原有知识', {}, template)

    def test_active_v5_prompt_enforces_length_and_concept_metadata(self):
        malformed = (
            ('# 知识标题\n\n正文', '缺少 version 5'),
            (study_document('控制', '控制根据反馈调整动作。', '控制').replace(
                '(concept:topic)', '(concept:missing)'), '正文概念缺少元数据'),
            (study_document('控制', '控制' + '原' * 1801, '控制'), '超过 1800 字上限'),
        )
        for template in ('general', 'lecture'):
            for document, error in malformed:
                with self.subTest(template=template, error=error), patch.object(
                        providers, 'deepseek', return_value=document) as send:
                    with self.assertRaisesRegex(providers.ProviderError, error):
                        providers.summarize('完整的原文', {}, template)
                    send.assert_called_once()

    def test_lecture_standard_applies_to_extraction_reduction_and_final_merge(self):
        calls = []
        def fake_deepseek(messages, config, max_tokens=8192):
            calls.append(messages)
            if '<source_part ' in messages[-1]['content']:
                return '[S1 00:03] 条件与推导。' + ('中' * 21000)
            return study_document('条件与推导', '[S1 00:03] 合并后的条件与推导。', '条件')
        with patch.object(providers, 'deepseek', side_effect=fake_deepseek):
            providers.summarize('[00:03] 开头' + ('原' * 48000) + '末尾教师原题', {}, 'lecture')
        self.assertGreater(len(calls), 4)  # Includes the >60k intermediate reduction.
        for messages in calls:
            self.assertTrue(messages[0]['content'].startswith(providers.LECTURE_PROMPT_PATH.read_text(encoding='utf-8-sig').strip()))
        self.assertIn('末尾教师原题', ''.join(m[-1]['content'] for m in calls[:3]))
        self.assertIn('[S1 00:03]', calls[-1][-1]['content'])

    def test_deepseek_uses_current_flash_id_for_default_and_legacy_alias(self):
        output = {"choices": [{"finish_reason": "stop", "message": {"content": "正文"}}]}
        for configured in (None, 'deepseek-v4-flash', 'deepseek-flash'):
            config = {'deepseekApiKey': 'test-key'}
            if configured:
                config['deepseekModel'] = configured
            with self.subTest(configured=configured), patch.object(
                    providers, 'post_json', return_value=(output, {})) as send:
                providers.deepseek([{'role': 'user', 'content': '本地测试'}], config)
            self.assertEqual(send.call_args.args[1]['model'], 'deepseek-flash')

    def test_long_text_split_preserves_every_character_and_tail(self):
        source = ("控制系统。\n" * 12000) + "最后的关键决定：扭矩上限 2 N·m。"
        parts = providers.split_text(source)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), source)
        self.assertTrue(parts[-1].endswith("2 N·m。"))

    def test_long_summary_sends_every_source_part_then_combines(self):
        source = "开头" + ("中" * 48000) + "结尾唯一证据"
        source_calls = []

        def fake_deepseek(messages, config, max_tokens=8192):
            text = messages[-1]["content"]
            source_calls.append(text)
            if '<source_part ' in text:
                return f"分段笔记{len(source_calls)}：已保留事实。"
            return study_document('合并笔记', '已保留全部分段事实。', '分段事实')

        with patch.object(providers, "deepseek", side_effect=fake_deepseek):
            result = providers.summarize(source, {}, "lecture")
        self.assertIn("结尾唯一证据", "".join(source_calls[:-1]))
        self.assertIn("开头", source_calls[0])
        self.assertEqual(len(source_calls), 4)
        self.assertIn("分段笔记1", source_calls[-1])
        self.assertIn("分段笔记3", source_calls[-1])
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
