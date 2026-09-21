"""Small format repairs for generated drafts; transcripts are never inputs to writes."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit


_STUDY = re.compile(r"(?ms)^```study\s*\n(.*?)\n```[ \t]*$")
_IMAGES = re.compile(r"(?ms)^```note-images[ \t]*\r?\n.*?\r?\n```[ \t]*\r?$")
_DETAILS = re.compile(r"(?ms)^:::details\s+(课程信息|来源定位|待核对|待确定)[^\n]*\n(.*?)\n:::[ \t]*$")


def _study(summary: str) -> dict:
    for match in _STUDY.finditer(summary):
        try:
            data = json.loads(match[1])
            if isinstance(data, dict):
                return data
        except (TypeError, ValueError):
            pass
    return {}


def extract_course_info(summary: str) -> list[str]:
    data = _study(summary)
    values = data.get("courseInfo", [])
    values = list(values) if isinstance(values, list) else []
    for match in _DETAILS.finditer(summary):
        if match[1] == "课程信息":
            values.extend(match[2].splitlines())
    result = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", value).strip()
        if value and value not in result:
            result.append(value[:240])
    return result[:24]


def knowledge_indexes(term: str, supplied: list | None = None) -> list[dict]:
    """A known search endpoint fills missing links without inventing articles."""
    result, seen = [], set()
    for item in supplied or []:
        if not isinstance(item, dict):
            continue
        title, url = str(item.get("title", "")).strip(), str(item.get("url", "")).strip()
        try:
            parsed = urlsplit(url)
            safe = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            safe = False
        if safe and title and url not in seen:
            result.append({"title": title[:120], "url": url})
            seen.add(url)
    query = quote(term.strip()[:100])
    for title, url in (
        ("知乎搜索：" + term, "https://www.zhihu.com/search?type=content&q=" + query),
        ("B站搜索：" + term, "https://search.bilibili.com/all?keyword=" + query),
        ("网页搜索：" + term, "https://www.bing.com/search?q=" + query),
    ):
        if url not in seen:
            result.append({"title": title, "url": url})
            seen.add(url)
    return result[:3]


def finalize_note_output(summary: str) -> str:
    data, course_info = _study(summary), extract_course_info(summary)
    # Image provenance travels with every saved/exported revision. Strip it
    # before normalizing exploration, otherwise its last section consumes it.
    images = _IMAGES.findall(summary)
    body = _IMAGES.sub("", _STUDY.sub("", summary))
    body = _DETAILS.sub("", body)
    # Legacy metadata headings may also be plain Markdown sections.
    body = re.sub(r"(?ms)^##\s+(?:来源定位|待核对|待确定)\s*\n.*?(?=^##\s|\Z)", "", body)
    concepts = data.get("concepts", [])
    # Concept questions come from the model's explicit study data, never from section titles.
    if isinstance(concepts, list):
        for concept in concepts:
            if isinstance(concept, dict) and isinstance(concept.get("term"), str):
                concept["links"] = knowledge_indexes(concept["term"], concept.get("links") if isinstance(concept.get("links"), list) else [])
    if course_info:
        data["courseInfo"] = course_info
    # Exploration consists only of clickable titles. Keep model text in the
    # provider receipt, but do not render its extra paragraphs as study material.
    exploration = re.search(r"(?ms)^##\s+(?:额外探索|继续探索)\s*\n(.*?)(?=^##\s|\Z)", body)
    if exploration:
        links = re.findall(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", exploration[1])
        unique = list(dict.fromkeys(links))[:3]
        replacement = "## 额外探索\n\n" + "\n".join(f"- [{title}]({url})" for title, url in unique) if unique else ""
        body = body[:exploration.start()] + replacement + "\n\n" + body[exploration.end():]
    if images:
        body = body.rstrip() + "\n\n" + "\n\n".join(images)
    if data:
        data["version"] = 6
        body = body.rstrip() + "\n\n```study\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n```"
    return body.strip()
