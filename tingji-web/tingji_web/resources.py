"""Select contextual visual explanations from a source-checked web catalog.

No external search request or provider charge is made here. Entries retain their
original titles and known metadata so popularity and duration are never guessed.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


CATALOG_PATH = Path(__file__).with_name("visual_resources.json")
PUBLIC_PATH = Path(__file__).resolve().parents[1] / "public"
IMAGE_PATH = re.compile(r"^/assets/note-resources/[a-z0-9-]+\.(?:webp|png|jpg)$")
EPISODE = re.compile(r"\b(?:lecture|lesson)\s*\d*|\bpart\s*\d+|第[一二三四五六七八九十百\d]+[讲课节]|\b\d+\.\d+\s+", re.I)


def bundled_image_paths() -> set[str]:
    """Canonical paths that may be preserved by course-link regeneration."""
    entries = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {entry["imagePath"] for entry in entries if IMAGE_PATH.fullmatch(entry.get("imagePath", ""))
            and (PUBLIC_PATH / entry["imagePath"].lstrip("/")).is_file()}


def _match(tag: str, text: str) -> bool:
    # Keep short Latin terms such as PID from matching an unrelated longer word.
    if tag.isascii():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(tag.casefold()) + r"(?![a-z0-9])", text))
    return tag.casefold() in text


def select_resources(material: str, *, catalog: list[dict] | None = None, limit: int = 8) -> list[dict]:
    if catalog is None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    text = material.casefold()
    candidates = []
    for entry in catalog:
        try:
            url = urlsplit(entry.get("url", ""))
            safe = (url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
                    and (url.port is None or 0 < url.port < 65536)
                    and not re.search(r"[\s\\\x00-\x1f]", entry.get("url", "")))
        except ValueError:
            safe = False
        if not safe:
            continue
        if not entry.get("checkedAt") or (entry.get("kind") != "course-page" and entry.get("courseEpisode") is not False):
            continue
        if entry.get("kind") == "video" and EPISODE.search(entry.get("sourceTitle", "") + " " + entry.get("title", "")):
            continue
        matches = sum(_match(tag, text) for tag in entry.get("tags", []) if tag)
        if not matches:
            continue
        seconds = entry.get("durationSeconds")
        seconds = seconds if isinstance(seconds, (int, float)) and seconds > 0 else None
        # Among equally relevant resources, prefer an immediate visual explanation;
        # among videos, prefer shorter known lengths, then observed popularity.
        form = {"photo": 0, "practice": 0, "industry": 0, "course-page": 1,
                "interactive": 1, "animation": 1, "diagram": 1, "video": 2,
                "tool": 2, "product": 2, "history": 2, "article": 3}.get(entry.get("kind"), 4)
        duration_band = 0 if seconds is not None and seconds <= 300 else 1 if seconds is not None and seconds <= 600 else 2 if seconds is not None else 3
        views = entry.get("views")
        views = views if isinstance(views, int) and views >= 0 else -1
        candidates.append(((-matches, form, duration_band, -views, seconds or float("inf"), entry.get("id", "")), entry))
    ranked = sorted(candidates, key=lambda pair: pair[0])
    # A long lecture should have a real object, course page and useful exploration
    # where relevant, rather than fill every slot with the same kind of diagram.
    diverse, kinds = [], set()
    for pair in ranked:
        kind = pair[1].get("kind")
        if kind not in kinds:
            diverse.append(pair)
            kinds.add(kind)
    selected, seen, counts = [], set(), {}
    for _, entry in diverse + ranked:
        topic = entry.get("topic", entry.get("id"))
        if entry["url"] in seen or counts.get(topic, 0) >= 2:
            continue
        if len(selected) >= max(0, min(limit, 12)):
            break
        selected.append({key: value for key, value in entry.items() if key != "tags"})
        seen.add(entry["url"])
        counts[topic] = counts.get(topic, 0) + 1
    return selected


def resource_context(material: str) -> str:
    entries = select_resources(material)
    if not entries:
        return ""
    # Attribution and resource checks belong to the application. Send the model
    # only the fields it needs to select a relevant link or local image.
    fields = {"id", "title", "url", "kind", "imagePath", "explanation", "sourceYear", "page"}
    return "\n\n<resources>\n" + json.dumps(
        [{key: value for key, value in entry.items() if key in fields} for entry in entries],
        ensure_ascii=False) + "\n</resources>"


# These broad catalog tags are useful for discovery, but do not establish that
# an illustration explains the lecture itself (for example, any robot photo).
_BROAD_IMAGE_TAGS = {"机器人", "机械臂", "robot", "控制", "动力学", "装配", "连杆",
                     "kinematics", "工业机器人", "industrial robot", "自动化", "制造业",
                     "机械设计", "机构", "3d", "ppt", "动画"}
_COURSE_NUMBER = re.compile(r"(?<![\d.])\d+\.\d+(?![\d.])")
_PAGE_NUMBER = re.compile(r"第\s*(\d+)\s*页|\b(?:page|p\.)\s*(\d+)\b", re.I)
_EXPLORATION = re.compile(r"(?mi)^ {0,3}#{1,6}\s+(?:\*\*)?(?:额外探索|拓展探索|extra explorations?|further exploration)\b[^\n]*")
_IMAGE_START = re.compile(r"(?<!\\)!\[((?:\\.|[^\]\\\n])*)\]")
_REFERENCE = re.compile(r"(?m)^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:<([^>\n]*)>|(\S+))")


def _plain_markdown(text: str) -> str:
    """Mask code without moving offsets into the original Markdown."""
    def mask(match):
        return re.sub(r"[^\n]", " ", match[0])

    text = re.sub(r"(?ms)^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}(?P=fence)[ \t]*(?:\n|$)|\Z)", mask, text)
    return re.sub(r"(`+)(?!`)[^\n]*?\1(?!`)", mask, text)


def _image_spans(text: str):
    """Yield rendered inline/reference Markdown images and their destinations."""
    visible = _plain_markdown(text)
    references = {match[1].casefold().strip(): match[2] or match[3] for match in _REFERENCE.finditer(visible)}
    for match in _IMAGE_START.finditer(visible):
        end, destination = match.end(), ""
        if visible[end:end + 1] == "(":
            cursor, depth, quote = end + 1, 1, ""
            while cursor < len(visible) and depth:
                char = visible[cursor]
                if char == "\\":
                    cursor += 2
                    continue
                if quote:
                    if char == quote:
                        quote = ""
                elif char in "\"'" and visible[cursor - 1].isspace():
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                cursor += 1
            if depth:
                continue  # Incomplete syntax is ordinary text, not an image.
            target = visible[end + 1:cursor - 1].strip()
            if target.startswith("<") and ">" in target:
                destination = target[1:target.index(">")]
            else:
                destination = target.split(maxsplit=1)[0] if target else ""
            end = cursor
        elif visible[end:end + 1] == "[":
            close = visible.find("]", end + 1)
            if close < 0:
                continue
            label = visible[end + 1:close] or match[1]
            destination = references.get(label.casefold().strip(), "")
            end = close + 1
        else:
            destination = references.get(match[1].casefold().strip())
            if destination is None:
                continue
        yield match.start(), end, match[1], destination


def _specific_image_matches(entry: dict, material: str) -> int:
    return sum(_match(tag, material.casefold()) for tag in entry.get("tags", [])
               if tag and tag.casefold() not in _BROAD_IMAGE_TAGS)


def _resolve_image_alt(alt: str, entries: list[dict]) -> dict | None:
    """Require unique catalog evidence; never repair from an arbitrary URL."""
    text = unicodedata.normalize("NFKC", alt).casefold()
    pages = {int(value) for pair in _PAGE_NUMBER.findall(text) for value in pair if value}
    courses = set(_COURSE_NUMBER.findall(text))
    authorities = {token.casefold() for entry in entries
                   for token in re.findall(r"\b[A-Z]{2,8}\b", entry.get("author", ""))
                   if _match(token, text)}
    candidates = []
    for entry in entries:
        if pages and pages != {entry.get("page")}:
            continue
        if courses and not courses.issubset(set(_COURSE_NUMBER.findall(entry.get("title", "")))):
            continue
        if authorities and not all(_match(token, entry.get("author", "").casefold()) for token in authorities):
            continue
        topic = _specific_image_matches(entry, text)
        title = unicodedata.normalize("NFKC", entry.get("title", "")).casefold().removesuffix("(英)").strip()
        exact_title = bool(title and title in text)
        evidence = bool(pages) + bool(courses) + bool(authorities) + bool(topic)
        if exact_title or evidence >= 2:
            candidates.append((bool(topic) or exact_title, entry))
    # A supplied topic disambiguates two pages sharing a school/course/page.
    if any(topic for topic, _ in candidates):
        candidates = [(topic, entry) for topic, entry in candidates if topic]
    unique = {entry["imagePath"]: entry for _, entry in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _catalog_figure(entry: dict) -> str:
    def label(value):
        return str(value).replace("[", r"\[").replace("]", r"\]").replace("\n", " ")

    page = f"，第 {entry['page']} 页" if entry.get("page") else ""
    caption = f"![{label(entry['title'])}{page}]({entry['imagePath']})"
    source = f"来源：[{label(entry['sourceTitle'])}{page}]({entry['url']})；{label(entry['author'])}"
    if entry.get("sourceYear"):
        source += f"；{entry['sourceYear']} 年"
    if entry.get("license"):
        source += f"；[{label(entry['license'])}]({entry['licenseUrl']})"
    return caption + "\n\n" + entry["explanation"] + "\n\n" + source + "。"


def repair_note_images(summary: str, material: str) -> str:
    """Repair one final draft using the exact material passed to resource_context.

    No provider request, historical-note rewrite or transcript mutation occurs.
    Only this invocation's selected, reviewed, existing local assets are usable.
    """
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    selected_ids = {entry["id"] for entry in select_resources(material, catalog=catalog)}
    entries = [entry for entry in catalog if entry.get("id") in selected_ids
               and IMAGE_PATH.fullmatch(entry.get("imagePath", ""))
               and (PUBLIC_PATH / entry["imagePath"].lstrip("/")).is_file()]
    from .image_search import note_image_resources
    allowed = {entry["imagePath"] for entry in entries}
    allowed.update(entry["imagePath"] for entry in note_image_resources(summary))
    for start, end, alt, destination in reversed(list(_image_spans(summary))):
        if destination in allowed:
            continue
        entry = _resolve_image_alt(alt, entries)
        replacement = f"![{alt}]({entry['imagePath']})" if entry else ""
        summary = summary[:start] + replacement + summary[end:]

    visible = _plain_markdown(summary)
    exploration = _EXPLORATION.search(visible)
    body_end = exploration.start() if exploration else len(summary)
    if any(start < body_end and destination in allowed for start, _, _, destination in _image_spans(summary)):
        return summary
    ranked = sorted(((_specific_image_matches(entry, material), index, entry)
                     for index, entry in enumerate(entries)), key=lambda row: (-row[0], row[1]))
    if not ranked or ranked[0][0] == 0 or not summary[:body_end].strip():
        return summary
    entry = ranked[0][2]
    # End of the best matching section, or immediately before exploration. Do
    # not insert inside a disclosure block when a model has put headings there.
    headings = [match.start() for match in re.finditer(r"(?m)^ {0,3}#{1,6}\s+[^\n]+", visible[:body_end])
                if visible[:match.start()].count(":::details") == visible[:match.start()].count("\n:::\n")]
    boundaries = sorted({0, *headings, body_end})
    sections = [(_specific_image_matches(entry, visible[start:end]), end)
                for start, end in zip(boundaries, boundaries[1:])]
    best = max(sections, key=lambda row: (row[0], -row[1])) if sections else (0, body_end)
    position = best[1] if best[0] else body_end
    return summary[:position].rstrip() + "\n\n" + _catalog_figure(entry) + "\n\n" + summary[position:].lstrip()
