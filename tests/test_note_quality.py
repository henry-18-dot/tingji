"""The local checker validates structure only; factual review remains separate."""
import json
import unittest

from tingji import note_quality


class NoteQualityTests(unittest.TestCase):
    def test_valid_document_accepts_closed_top_level_components(self):
        text = """# 减速器约束电机输出

## 转矩关系
核心公式为 `τ_out = η i τ_m`。

:::details 来源定位
公式见 [S1 00:01]。
:::

:::examples 原文示例
### 示例一
原文计算得到 3.2 N·m。
### 示例二
原文计算得到 900 rpm。
:::
"""
        result = note_quality.inspect_document(text)
        self.assertTrue(result['structuralPass'])
        self.assertEqual(result['title'], '减速器约束电机输出')
        self.assertEqual(result['factualReview'], 'required')

    def test_unclosed_and_unmatched_directives_are_rejected(self):
        unclosed = note_quality.inspect_document('# 标题\n\n:::details 来源\n内容')
        unmatched = note_quality.inspect_document('# 标题\n\n:::')
        self.assertIn('组件未闭合', unclosed['errors'])
        self.assertIn('组件结束标记没有对应开始', unmatched['errors'])

    def test_nested_directives_are_rejected(self):
        text = '# 标题\n\n:::details 外层\n:::examples 内层\n### 例一\n内容\n:::\n:::'
        result = note_quality.inspect_document(text)
        self.assertIn('组件不允许嵌套', result['errors'])
        self.assertFalse(result['structuralPass'])

    def test_directive_and_heading_like_text_inside_code_is_ignored(self):
        text = """# 控制器代码边界

```text
# 伪标题
:::details 不应解析
```
"""
        result = note_quality.inspect_document(text)
        self.assertTrue(result['structuralPass'])
        self.assertEqual(result['title'], '控制器代码边界')

    def test_missing_or_multiple_h1_is_rejected(self):
        missing = note_quality.inspect_document('## 只有二级标题\n内容')
        multiple = note_quality.inspect_document('# 标题一\n内容\n# 标题二')
        self.assertIn('首行缺少知识标题', missing['errors'])
        self.assertIn('全文必须只有一个一级标题', missing['errors'])
        self.assertIn('全文必须只有一个一级标题', multiple['errors'])

    def test_unsupported_html_is_rejected(self):
        for html in ('<table><tr><td>内容</td></tr></table>', '<div>内容</div>', '<img src="x">', '<a href="x">链接</a>'):
            result = note_quality.inspect_document('# 标题\n\n' + html)
            self.assertIn('输出包含不支持的HTML', result['errors'])
            self.assertFalse(result['structuralPass'])

    def test_html_code_sample_is_preserved_inside_fence(self):
        result = note_quality.inspect_document('# 代码示例\n\n```html\n<div>原有代码</div>\n```')
        self.assertTrue(result['structuralPass'])


class StudyQualityTests(unittest.TestCase):
    def document(self, *, body='[关节角](concept:joint) 决定连杆的位置。', concepts=None,
                 exploration='- 自动控制起源于什么需求？\n- 机器怎样改变劳动？\n- 怎样看待效率？'):
        if concepts is None:
            concepts = [{'id': 'joint', 'term': '关节角', 'question': '关节角是什么？',
                         'links': [{'title': '关节角讲解', 'url': 'https://example.org/joint'}]}]
        metadata = json.dumps({'version': 5, 'concepts': concepts}, ensure_ascii=False)
        return '# 机器人运动\n\n' + body + '\n\n## 继续探索\n' + exploration + '\n\n```study\n' + metadata + '\n```'

    def test_valid_study_document_is_accepted(self):
        result = note_quality.inspect_document(self.document(), require_study=True)
        self.assertTrue(result['structuralPass'], result['errors'])
        self.assertGreater(result['readingHanzi'], 0)

    def test_reading_count_excludes_code_math_and_link_destinations(self):
        text = r'''甲[乙](https://example.org/隐藏(网址)?q=汉字 "链接标题")丙
$\text{数学汉字}$ $$\text{块公式}$$ \(\text{行内公式}\) \[\text{块公式}\]
```study
{"version":5,"term":"元数据"}
```
~~~mermaid
graph TD; 隐藏 --> 图形
~~~
```tree
隐藏树节点
```
丁[戊][资料]
[资料]: https://example.org/隐藏网址
<https://example.org/隐藏网址>
'''
        self.assertEqual(note_quality.count_reading_hanzi(text), 5)
        self.assertEqual(note_quality.count_reading_hanzi('甲𠮷〇，乙abc123'), 4)

    def test_visible_link_labels_and_escaped_dollars_are_counted(self):
        self.assertEqual(note_quality.count_reading_hanzi(r'[坐标](concept:frame)，价格 \$10 元。'), 5)

    def test_hard_limit_rejects_long_body_without_changing_it(self):
        text = self.document(body='[关节角](concept:joint)' + '字' * 1800)
        original = text
        result = note_quality.inspect_document(text)
        self.assertFalse(result['structuralPass'])
        self.assertGreater(result['readingHanzi'], 1800)
        self.assertTrue(any('1800 字上限' in error for error in result['errors']))
        self.assertEqual(text, original)

    def test_long_hidden_metadata_and_latex_do_not_use_reading_budget(self):
        concepts = [{'id': 'joint', 'term': '隐藏资料' * 600, 'question': '关节角是什么？', 'links': []}]
        ordinary = self.document(concepts=concepts)
        text = self.document(body='[关节角](concept:joint) 决定连杆的位置。\n$$\\text{' + '公式' * 2000 + '}$$',
                             concepts=concepts)
        result = note_quality.inspect_document(text, require_study=True)
        self.assertTrue(result['structuralPass'], result['errors'])
        self.assertEqual(result['readingHanzi'], note_quality.count_reading_hanzi(ordinary))

    def test_bad_json_and_nonobject_metadata_are_rejected(self):
        for payload, expected in (('{"version":5,}', '有效 JSON'), ('[]', 'JSON 对象')):
            with self.subTest(payload=payload):
                result = note_quality.inspect_document('# 标题\n```study\n' + payload + '\n```', require_study=True)
                self.assertFalse(result['structuralPass'])
                self.assertTrue(any(expected in error for error in result['errors']))

    def test_metadata_and_body_markers_must_match(self):
        result = note_quality.inspect_document(self.document(body='[位置](concept:position)'))
        self.assertIn('正文概念缺少元数据：position', result['errors'])
        self.assertIn('study 概念未在正文标记：joint', result['errors'])
        hidden_only = note_quality.inspect_document(self.document(body='```text\n[关节角](concept:joint)\n```'))
        self.assertIn('study 概念未在正文标记：joint', hidden_only['errors'])

    def test_concept_ids_are_unique_and_ascii_safe(self):
        for identifiers in (('joint', 'joint'), ('joint', '关节'), ('joint', 'bad/id')):
            concepts = [{'id': identifier, 'term': '关节角', 'question': '是什么？', 'links': []}
                        for identifier in identifiers]
            with self.subTest(identifiers=identifiers):
                self.assertFalse(note_quality.inspect_document(self.document(concepts=concepts))['structuralPass'])

    def test_single_short_question_and_required_term_are_enforced(self):
        base = {'id': 'joint', 'term': '关节角', 'question': '关节角是什么？', 'links': []}
        variants = ({'question': ['第一问？', '第二问？']}, {'question': ''},
                    {'question': '是什么？为什么？'}, {'question': '第一问\n第二问'},
                    {'question': '问' * 46}, {'questions': ['额外问题？']}, {'term': ''})
        for update in variants:
            with self.subTest(update=update):
                self.assertFalse(note_quality.inspect_document(self.document(concepts=[dict(base, **update)]))['structuralPass'])

    def test_resource_urls_reject_script_credentials_and_malformed_hosts(self):
        urls = ('javascript:alert(1)', 'data:text/html,hello', 'https://user:pass@example.org',
                'https://user@example.org', 'https://@example.org', 'https:///missing',
                'https://example.org:bad', 'https://exam ple.org', 'https://example.org\\evil')
        for url in urls:
            concepts = [{'id': 'joint', 'term': '关节角', 'question': '是什么？',
                         'links': [{'title': '讲解', 'url': url}]}]
            with self.subTest(url=url):
                self.assertFalse(note_quality.inspect_document(self.document(concepts=concepts))['structuralPass'])

    def test_resource_list_has_at_most_three_titled_links(self):
        for links in ([{'title': '资料', 'url': 'https://example.org'}] * 4,
                      [{'title': '', 'url': 'https://example.org'}], 'https://example.org'):
            concepts = [{'id': 'joint', 'term': '关节角', 'question': '是什么？', 'links': links}]
            with self.subTest(links=links):
                self.assertFalse(note_quality.inspect_document(self.document(concepts=concepts))['structuralPass'])

    def test_exploration_has_exactly_three_items(self):
        for count in (0, 2, 4):
            with self.subTest(count=count):
                result = note_quality.inspect_document(self.document(exploration='\n'.join('- 为什么？' for _ in range(count))))
                self.assertIn('继续探索必须恰好包含 3 项', result['errors'])
        ordered = self.document(exploration='1. 为什么？\n2. 如何发生？\n3. 怎样影响生活？')
        self.assertTrue(note_quality.inspect_document(ordered)['structuralPass'])

    def test_v5_rejects_old_details_while_legacy_documents_remain_compatible(self):
        result = note_quality.inspect_document(self.document(body='[关节角](concept:joint)\n:::details 补充\n解释\n:::'))
        self.assertIn('v5 不允许使用 :::details', result['errors'])
        legacy = '# 旧笔记\n:::details 补充\n' + '字' * 1801 + '\n:::'
        self.assertTrue(note_quality.inspect_document(legacy)['structuralPass'])
        self.assertIn('缺少 version 5 的 study 元数据', note_quality.inspect_document(legacy, require_study=True)['errors'])

    def test_math_in_title_and_fake_headings_inside_latex_remain_valid(self):
        text = '# 关节角 $q$ 与位置\n\n$$\n# 公式中的符号\nx < y > z\n$$'
        result = note_quality.inspect_document(text)
        self.assertTrue(result['structuralPass'], result['errors'])
        self.assertEqual(result['title'], '关节角 $q$ 与位置')


if __name__ == '__main__':
    unittest.main()
