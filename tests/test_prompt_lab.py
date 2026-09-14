import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tingji import storage, providers, prompt_lab, lifecycle


class PromptLabTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in [('DATA', root), ('DB', root / 'notes.sqlite3')]:
            p = patch.object(storage, name, value); p.start(); self.addCleanup(p.stop)
        storage.initialize()
        prompt = root / 'prompt.md'; prompt.write_text('系统提示词。' * 40, encoding='utf-8')
        p = patch.object(providers, 'LECTURE_PROMPT_PATH', prompt); p.start(); self.addCleanup(p.stop)
        self.note = storage.create_note('测试课程', '第一行\r\n第二行', template='lecture', asrComplete=True)

    def data(self, **kwargs):
        return {'noteId': self.note['id'], 'operationId': 'test-operation-123456',
                'revision': prompt_lab.current()['revision'], **kwargs}

    def test_first_raw_snapshot_survives_retention_and_later_edits(self):
        prompt_lab.preserve(self.note)
        storage.update_note(self.note['id'], transcript='后来编辑', summary='# 笔记\n\n正文')
        lifecycle.purge_transcript(self.note['id'])
        self.assertEqual(prompt_lab.source(self.note['id']), '第一行\r\n第二行')

    def test_partial_transcript_never_becomes_original(self):
        prompt_lab.preserve({**self.note, 'asrComplete': False})
        self.assertFalse((prompt_lab.folder(self.note['id']) / 'original.txt').exists())

    def test_source_tamper_fails_before_network(self):
        prompt_lab.preserve(self.note)
        (prompt_lab.folder(self.note['id']) / 'original.txt').write_text('篡改', encoding='utf-8')
        with patch.object(providers, 'deepseek') as send:
            with self.assertRaisesRegex(ValueError, '校验值'):
                prompt_lab.generate(self.data())
            send.assert_not_called()

    def test_update_persists_and_same_operation_does_not_charge_twice(self):
        data = self.data(prompt=prompt_lab.current()['prompt'], intent='自然解释')
        with patch.object(providers, 'deepseek', return_value='新提示词。' * 40) as send:
            result = prompt_lab.update(data)
            self.assertEqual(prompt_lab.update(data), result)
            self.assertEqual(prompt_lab.current()['prompt'], result['prompt'])
            self.assertEqual(send.call_count, 1)
        self.assertTrue(list(prompt_lab.folder(self.note['id']).glob('*.previous-prompt.md')))
        for template in ('general', 'lecture'):
            with self.subTest(template=template), patch.object(
                    providers, 'deepseek', return_value='# 新笔记\n\n解释') as send:
                providers.summarize('新录音原文', {}, template)
            self.assertTrue(send.call_args.args[0][0]['content'].startswith(result['prompt']))

    def test_v5_generation_preserves_interaction_data_and_original(self):
        approved = Path(__file__).resolve().parent.parent / 'docs' / 'note-standard-v5' / 'deepseek-system.md'
        providers.LECTURE_PROMPT_PATH.write_bytes(approved.read_bytes())
        prompt = prompt_lab.current()
        self.assertEqual(prompt['prompt'], approved.read_text(encoding='utf-8-sig').strip())
        metadata = {'version': 5, 'concepts': [
            {'id': 'torque', 'term': '力矩', 'question': '力矩是什么？', 'links': []}],
            'terms': [{'text': '力矩', 'group': 'force'}], 'symbols': ['tau']}
        document = ('# 力矩\n\n[力矩](concept:torque)描述力的转动作用，$\\tau=Fr$。\n\n'
                    '## 继续探索\n- 杠杆怎样改变劳动？\n- 力矩概念怎样形成？\n- 古人怎样建造机械？\n\n'
                    '```study\n' + json.dumps(metadata, ensure_ascii=False) + '\n```')
        with patch.object(providers, 'deepseek', return_value=document) as send:
            result = prompt_lab.generate(self.data())
        messages, config, _ = send.call_args.args
        self.assertTrue(messages[0]['content'].startswith(prompt['prompt']))
        self.assertEqual(config['deepseekModel'], 'deepseek-flash')
        self.assertEqual(result['summary'], document)
        self.assertEqual(storage.get_note(self.note['id'])['summary'], document)
        self.assertEqual(prompt_lab.source(self.note['id']), '第一行\r\n第二行')
        self.assertLess(result['readingHanzi'], 1800)

    def test_v5_generation_rejects_missing_metadata_and_keeps_candidate(self):
        providers.LECTURE_PROMPT_PATH.write_text('阅读协议：study-v5。', encoding='utf-8')
        original_summary = '# 原笔记\n\n保留'
        storage.update_note(self.note['id'], summary=original_summary)
        data = self.data()
        with patch.object(providers, 'deepseek', return_value='# 候选笔记\n\n正文'):
            with self.assertRaisesRegex(ValueError, '缺少 version 5'):
                prompt_lab.generate(data)
        self.assertEqual(storage.get_note(self.note['id'])['summary'], original_summary)
        self.assertEqual(prompt_lab.source(self.note['id']), '第一行\r\n第二行')
        self.assertEqual((prompt_lab.folder(self.note['id']) / (data['operationId'] + '.md')).read_text(
            encoding='utf-8'), '# 候选笔记\n\n正文')

    def test_generate_uses_raw_and_does_not_overwrite_concurrent_manual_edit(self):
        def answer(messages, config, max_tokens):
            self.assertIn('第一行\r\n第二行', messages[1]['content'])
            storage.update_note(self.note['id'], summary='# 用户刚编辑\n\n保留')
            return '# 候选笔记\n\n新内容'
        with patch.object(providers, 'deepseek', side_effect=answer):
            result = prompt_lab.generate(self.data())
        self.assertFalse(result['applied'])
        self.assertIn('用户刚编辑', storage.get_note(self.note['id'])['summary'])
        self.assertEqual(prompt_lab.source(self.note['id']), '第一行\r\n第二行')

    def test_successful_generate_is_idempotent_and_keeps_old_note(self):
        data = self.data()
        with patch.object(providers, 'deepseek', return_value='# 新笔记\n\n解释') as send:
            first = prompt_lab.generate(data)
            self.assertEqual(first, prompt_lab.generate(data))
            self.assertEqual(send.call_count, 1)
        self.assertTrue(first['applied'])
        self.assertEqual(prompt_lab.source(self.note['id']), '第一行\r\n第二行')


if __name__ == '__main__':
    unittest.main()
