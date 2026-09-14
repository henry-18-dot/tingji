"""Course names inferred locally from recording time, the timetable and saved notes."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEDULE = Path(__file__).resolve().parent.parent / 'config' / 'course-schedule.json'
EXAMPLE_SCHEDULE = SCHEDULE.with_name('course-schedule.example.json')
SHANGHAI = timezone(timedelta(hours=8), 'Asia/Shanghai')
WEEKDAYS = '一二三四五六日'
FIELDS = ('title', 'courseName', 'courseColor', 'topic', 'lessonSlot', 'recordedAt',
          'nameSource', 'namingConfidence')
_STAMP = re.compile(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?[ T_]+(\d{1,2})[:：-](\d{1,2})(?:[:：-](\d{1,2}))?')
_DEFAULT_TITLE = re.compile(r'^(?:(?:实时)?录音\s+20\d{2}|未命名(?:记录|录音|笔记)?$|录音$)')


def load_schedule():
    try:
        raw = SCHEDULE.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        raw = EXAMPLE_SCHEDULE.read_text(encoding='utf-8-sig')
    return json.loads(raw)


def recorded_time(note):
    """Never substitute createdAt/updatedAt: an upload can happen days later."""
    value = note.get('recordedAt')
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed).astimezone(SHANGHAI)
        except ValueError:
            pass
    values = [note.get('sourceName', '')]
    if _DEFAULT_TITLE.match(note.get('title') or ''):
        values.append(note.get('title', ''))
    for value in values:
        match = _STAMP.search(str(value))
        if match:
            try:
                return datetime(*(int(part or 0) for part in match.groups()), tzinfo=SHANGHAI)
            except ValueError:
                pass
    return None


def is_manual(note):
    if note.get('nameSource') in ('manual', 'auto'):
        return note['nameSource'] == 'manual'
    title = str(note.get('title') or '').strip()
    return bool(title and not _DEFAULT_TITLE.match(title))


def _in_season(recorded, schedule):
    if not recorded:
        return False
    try:
        return date.fromisoformat(schedule['seasonStart']) <= recorded.date() <= date.fromisoformat(schedule['seasonEnd'])
    except (KeyError, TypeError, ValueError):
        return False


def _week_allowed(lesson, recorded, schedule):
    override = (schedule.get('dayOverrides') or {}).get(recorded.date().isoformat(), {})
    if override.get('parity') and lesson.get('parity'):
        return override['parity'] == lesson['parity']
    if not schedule.get('week1Start'):
        return True
    try:
        week = (recorded.date() - date.fromisoformat(schedule['week1Start'])).days // 7 + 1
    except (TypeError, ValueError):
        return True
    lower, upper = lesson.get('weeks', [1, 30])
    return lower <= week <= upper and (not lesson.get('parity') or
        week % 2 == (1 if lesson['parity'] == 'odd' else 0))


def _minutes(value):
    h, m = str(value).split(':')
    return int(h) * 60 + int(m)


def _time_matches(lesson, recorded, note, schedule):
    periods = schedule.get('periods') or {}
    try:
        first = periods[str(lesson['periods'][0])]
        last = periods[str(lesson['periods'][-1])]
        start, end = _minutes(first['start']), _minutes(last['end'])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    minute = recorded.hour * 60 + recorded.minute + recorded.second / 60
    # Late starts and a short setup before class are normal. A long recording
    # does not turn a start from another period into evidence for this class.
    return start - 15 <= minute <= end


def _content_score(course, text):
    text = text.casefold()
    aliases = [value.casefold() for value in course.get('aliases', [])]
    explicit = any(re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', text)
                   if re.fullmatch('[a-z ]+', alias) else alias in text for alias in aliases)
    hits = sum(1 for word in course.get('keywords', []) if word.casefold() in text)
    return (8 if explicit else 0) + min(hits, 7), explicit


def _topic(summary, course=None):
    """Reuse an existing substantive heading; never invent an extra AI answer."""
    generic = {'课堂笔记', '课程笔记', '笔记', '核心要点', '主要内容', '内容概要', '概述', '总结', '整理', '概览'}
    headings = re.findall(r'^\s*#{1,6}\s+(.+?)\s*#*\s*$', summary, re.M)
    if not headings:
        headings = [line.strip() for line in summary.splitlines() if line.strip()][:1]
    for candidate in headings:
        candidate = re.sub(r'[*_`]', '', candidate).strip()
        candidate = re.sub(r'^(?:第[一二三四五六七八九十\d]+[章节讲]|\d+[.、]|[一二三四五六七八九十]+、)\s*', '', candidate)
        candidate = re.sub(r'^(?:课堂笔记|课程笔记|笔记|课程内容|核心内容)\s*[:：·—-]\s*', '', candidate)
        if course:
            for name in [course['name'], course.get('shortName', '')]:
                if name and candidate.startswith(name):
                    candidate = candidate[len(name):].lstrip(' ：:·—-')
        if candidate and candidate not in generic and len(candidate) <= 32:
            return candidate
    # Only select a keyword which actually occurs, rather than cropping a
    # sentence into a misleading title.
    if course:
        for word in course.get('keywords', []):
            if word.casefold() in summary.casefold():
                return word
    return '知识笔记'


def lesson_label(lesson):
    periods = lesson['periods']
    section = ''.join(map(str, periods)) if len(periods) == 2 and max(periods) < 10 else f'{periods[0]}–{periods[-1]}'
    return f'周{WEEKDAYS[lesson["weekday"] - 1]} {section}节'


def infer_fields(note, schedule=None):
    """Return fields only; caller persists under the same lock as a title edit."""
    schedule = load_schedule() if schedule is None else schedule
    recorded = recorded_time(note)
    manual = is_manual(note)
    result = {'nameSource': 'manual' if manual else 'auto'}
    if recorded:
        result['recordedAt'] = recorded.isoformat(timespec='seconds')
    summary = str(note.get('summary') or '').strip()
    if not summary or note.get('summaryStale') or note.get('isDemo'):
        return result

    in_season = _in_season(recorded, schedule)
    candidates = []
    # A known out-of-season date must never borrow this season's courses.
    courses = schedule.get('courses', []) if in_season or recorded is None else []
    for course in courses:
        score, explicit = _content_score(course, summary)
        original_names = {str(note.get('title') or '').strip().casefold(),
                          Path(str(note.get('sourceName') or '')).stem.strip().casefold()}
        if not explicit and any(alias.casefold() in original_names for alias in course.get('aliases', [])):
            score += 8
            explicit = True
        weekday = ((schedule.get('dayOverrides') or {}).get(recorded.date().isoformat(), {}).get('weekday', recorded.isoweekday())
                   if recorded else None)
        lessons = [lesson for lesson in course.get('lessons', []) if in_season and
                   lesson.get('weekday') == weekday and _week_allowed(lesson, recorded, schedule)]
        timed = [lesson for lesson in lessons if _time_matches(lesson, recorded, note, schedule) is True]
        score += 4 if timed else 1 if lessons else 0
        candidates.append((score, explicit, course, timed or lessons))
    candidates.sort(key=lambda item: item[0], reverse=True)
    chosen = None
    if candidates:
        best = candidates[0]
        second_score = candidates[1][0] if len(candidates) > 1 else 0
        # Time alone needs a verified period table; one generic keyword is weak.
        if best[0] >= 4 and best[0] - second_score >= 2:
            chosen = best
    course = chosen[2] if chosen else None
    lesson = None
    undated_slot = False
    if chosen and len(chosen[3]) == 1:
        candidate = chosen[3][0]
        # Explicit time disagreement should not label an outside-class recording
        # with a slot merely because its topic resembles a scheduled course.
        if _time_matches(candidate, recorded, note, schedule) is not False:
            lesson = candidate
    elif chosen and recorded is None and chosen[1] and len(course.get('lessons', [])) == 1:
        # A uniquely scheduled, explicitly identified course supplies a recurring
        # slot, but still supplies no evidence for a particular recording date.
        lesson = course['lessons'][0]
        undated_slot = True
    topic = _topic(summary, course)
    confidence = (0.95 if chosen[1] and lesson else 0.9 if lesson and chosen[0] >= 6 else
                  0.8 if chosen[1] else 0.7) if chosen else 0.0
    if undated_slot:
        confidence = 0.6
    elif lesson and lesson.get('parity') and not schedule.get('week1Start') and not (
            (schedule.get('dayOverrides') or {}).get(recorded.date().isoformat(), {}).get('parity')):
        confidence = min(confidence, 0.7)
    result.update(courseName=course['name'] if course else '', courseColor=course['color'] if course else '',
                  topic=topic, lessonSlot=lesson_label(lesson) if lesson else '', namingConfidence=confidence)
    if not manual:
        # Course and capture-time metadata are separate from the content title.
        # Never append a timetable or date to a rewritten knowledge heading.
        result['title'] = topic
    return result


def apply_auto_name(note_id, schedule=None):
    """Backfill a completed note without using a cloud service or touching audio."""
    from . import storage
    with storage.LOCK:
        note = storage.get_note(note_id)
        values = infer_fields(note, schedule)
        changed = {key: value for key, value in values.items() if note.get(key) != value}
        return storage.update_note(note_id, **changed) if changed else note


def download_name(note, extension='.txt'):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(note.get('title') or '')).strip('. ')[:160]
    title = title or str(note.get('id') or '课堂笔记')
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', title, re.I):
        title = '_' + title
    extension = extension if re.fullmatch(r'\.[a-zA-Z0-9]{1,8}', extension) else '.txt'
    return title + extension.lower()
