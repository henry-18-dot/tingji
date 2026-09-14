"""Local structural checks. Passing does not establish factual correctness."""
from __future__ import annotations
import json
import re
from urllib.parse import urlsplit


MAX_READING_HANZI = 1800
_HANZI = re.compile(r'[\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af]')
_MATH = re.compile(
    r'(?<!\\)\$\$.*?(?<!\\)\$\$|\\\[.*?\\\]|\\\(.*?\\\)|'
    r'(?<![\\$])\$(?!\$)(?:\\.|[^$\\])*?\$(?!\$)', re.DOTALL)
_CONCEPT_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]*\Z')


def _split_fences(text):
    """Keep line positions while separating hidden fenced content."""
    visible, study_blocks, payload = [], [], []
    code_fence, is_study = None, False
    for line in str(text).splitlines():
        fence = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if code_fence:
            visible.append('')
            if (fence and fence[1][0] == code_fence[0]
                    and len(fence[1]) >= len(code_fence) and not fence[2].strip()):
                if is_study:
                    study_blocks.append('\n'.join(payload))
                code_fence, payload = None, []
            else:
                payload.append(line)
        elif fence:
            code_fence = fence[1]
            is_study = fence[2].strip().lower() == 'study'
            visible.append('')
        else:
            visible.append(line)
    if code_fence and is_study:
        study_blocks.append('\n'.join(payload))
    return '\n'.join(visible), study_blocks, bool(code_fence)


def _without_math(text):
    return _MATH.sub(lambda match: '\n' * match[0].count('\n'), text)


def _without_link_destinations(text):
    # Link destinations can contain balanced parentheses or an optional title.
    pattern = re.compile(r'!?\[([^\]\n]*)\]\(')
    parts, cursor = [], 0
    for match in pattern.finditer(text):
        if match.start() < cursor:
            continue
        depth, end = 1, match.end()
        while end < len(text) and depth:
            char = text[end]
            if char == '\\':
                end += 2
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            end += 1
        if depth:
            continue
        parts.extend((text[cursor:match.start()], match[1]))
        cursor = end
    parts.append(text[cursor:])
    visible = ''.join(parts)
    visible = re.sub(r'^ {0,3}\[[^\]\n]+\]:\s*\S+.*$', '', visible, flags=re.MULTILINE)
    visible = re.sub(r'\[([^\]\n]+)\]\[[^\]\n]*\]', r'\1', visible)
    return re.sub(r'<https?://[^>\s]+>', '', visible, flags=re.IGNORECASE)


def count_reading_hanzi(text) -> int:
    """Count readable Hanzi, excluding fenced metadata/code, math, and link URLs."""
    visible, _, _ = _split_fences(text)
    return len(_HANZI.findall(_without_link_destinations(_without_math(visible))))


def _valid_resource_url(value):
    if not isinstance(value, str) or not value or re.search(r'[\s\x00-\x1f\x7f\\]', value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme.lower() in {'http', 'https'} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        return False


def _inspect_study(blocks, visible, errors, require_study):
    documents = []
    for block in blocks:
        try:
            value = json.loads(block)
        except (ValueError, TypeError):
            errors.append('study 元数据不是有效 JSON')
            continue
        if not isinstance(value, dict):
            errors.append('study 元数据必须是 JSON 对象')
        elif type(value.get('version')) is int and value['version'] == 5:
            documents.append(value)
    if require_study and not documents:
        errors.append('缺少 version 5 的 study 元数据')
    if not documents:
        return False
    if len(blocks) != 1:
        errors.append('v5 只能包含一个 study 元数据块')
    declared = set()
    for document in documents:
        concepts = document.get('concepts')
        if not isinstance(concepts, list):
            errors.append('study concepts 必须是列表')
            continue
        for concept in concepts:
            if not isinstance(concept, dict):
                errors.append('study 概念必须是对象')
                continue
            identifier = concept.get('id')
            if not isinstance(identifier, str) or not _CONCEPT_ID.fullmatch(identifier):
                errors.append('study 概念 id 必须是安全的 ASCII 标识符')
            elif identifier in declared:
                errors.append('study 概念 id 重复：' + identifier)
            else:
                declared.add(identifier)
            term = concept.get('term')
            if not isinstance(term, str) or not term.strip():
                errors.append('study 概念 term 不能为空')
            question = concept.get('question')
            if (not isinstance(question, str) or not question.strip() or 'questions' in concept
                    or '\n' in question or '\r' in question or len(re.findall(r'[?？]', question)) > 1):
                errors.append('study 每个概念只能有一个 question 字符串')
            elif len(_HANZI.findall(question)) > 45:
                errors.append('study question 不能超过 45 个汉字')
            links = concept.get('links')
            if not isinstance(links, list) or len(links) > 3:
                errors.append('study links 必须是最多 3 项的列表')
                continue
            for link in links:
                if (not isinstance(link, dict) or not isinstance(link.get('title'), str)
                        or not link['title'].strip() or not _valid_resource_url(link.get('url'))):
                    errors.append('study 资源必须有标题和不含账号密码的有效 HTTP(S) 链接')
    marker_text = re.sub(r'(`+).*?\1', '', visible, flags=re.DOTALL)
    marked = set(re.findall(r'concept:([^\s)\]>"\'`]+)', marker_text))
    for identifier in sorted(marked - declared):
        errors.append('正文概念缺少元数据：' + identifier)
    for identifier in sorted(declared - marked):
        errors.append('study 概念未在正文标记：' + identifier)
    if re.search(r'^\s*:::details\b', visible, flags=re.MULTILINE):
        errors.append('v5 不允许使用 :::details')
    sections = re.findall(r'^ {0,3}##\s+继续探索\s*#*\s*$([\s\S]*?)(?=^ {0,3}#{1,2}\s|\Z)',
                          visible, flags=re.MULTILINE)
    if len(sections) != 1:
        errors.append('v5 必须有一个继续探索栏目，包含恰好 3 项')
    else:
        items = re.findall(r'^( *)(?:[-+*]|\d+[.)])\s+\S', sections[0], flags=re.MULTILINE)
        min_indent = min(map(len, items), default=0)
        if sum(len(indent) == min_indent for indent in items) != 3:
            errors.append('继续探索必须恰好包含 3 项')
    return True


def inspect_document(text, require_study=False):
    errors, warnings = [], []
    lines = str(text).strip().splitlines()
    if not lines or not re.match(r'^#\s+\S', lines[0]):
        errors.append('首行缺少知识标题')
    titles = []
    directive = None
    visible, study_blocks, unclosed_fence = _split_fences(str(text).strip())
    without_math = _without_math(visible)
    for original, line in zip(visible.split('\n'), without_math.split('\n')):
        if re.match(r'^#\s+', line):
            titles.append(original[2:].strip())
        opening = re.match(r'^:::(details|examples)\s+\S', line)
        if opening:
            if directive:
                errors.append('组件不允许嵌套')
            directive = opening[1]
        elif line.strip() == ':::':
            if not directive:
                errors.append('组件结束标记没有对应开始')
            directive = None
        elif line.startswith(':::'):
            errors.append('未知或缺少标题的组件')
        if re.match(r'^#{1,6}\s+.*(?:人员安排|课程安排|课程进度|教师介绍|老师要求|复习计划|课后作业|考核方式|参考答案)', line):
            errors.append('含不属于知识正文的栏目')
        if re.search(r'</?[A-Za-z][\w-]*(?:\s[^<>]*?)?\s*/?>', line):
            errors.append('输出包含不支持的HTML')
    if len(titles) != 1:
        errors.append('全文必须只有一个一级标题')
    if directive:
        errors.append('组件未闭合')
    if unclosed_fence:
        errors.append('代码块未闭合')
    has_study = _inspect_study(study_blocks, without_math, errors, require_study)
    reading_hanzi = count_reading_hanzi(text)
    if (has_study or require_study) and reading_hanzi > MAX_READING_HANZI:
        errors.append(f'正文为 {reading_hanzi} 个汉字，超过 {MAX_READING_HANZI} 字上限')
    title = titles[0] if titles else ''
    if re.search(r'概览|课程导论|本次课程|周[一二三四五六日].*节', title):
        warnings.append('标题含空泛或课程安排措辞')
    if len(title) > 32:
        warnings.append('标题偏长，人工检查是否可以缩短')
    return {'title': title, 'errors': list(dict.fromkeys(errors)), 'warnings': warnings,
            'readingHanzi': reading_hanzi, 'structuralPass': not errors, 'factualReview': 'required'}
