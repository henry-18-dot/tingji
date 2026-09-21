"""Confirmed SUSTech 2026 autumn dates; no extrapolation to other terms."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone


AUTUMN_2026 = {
    "sourceUrl": "https://sustech.edu.cn/uploads/files/2025/11/25155108_33786.pdf",
    "semesterStart": "2026-09-07",
    "teachingEnd": "2026-12-27",
    "end": "2027-01-08",
    "holidays": [
        {"start": "2026-09-25", "end": "2026-09-27", "label": "中秋节"},
        {"start": "2026-10-01", "end": "2026-10-07", "label": "国庆节"},
        {"start": "2026-11-20", "end": "2026-11-20", "label": "校运会停课"},
    ],
    "adjustments": [
        {"date": "2026-09-20", "weekday": 5, "weekParity": "odd", "label": "上单周周五的课"},
        {"date": "2026-10-10", "weekday": 3, "weekParity": "odd", "label": "上单周周三的课"},
    ],
}


def today() -> date:
    return datetime.now(timezone(timedelta(hours=8))).date()


def calendar_for(semester_start: str | None, *, on: date | None = None) -> dict | None:
    """An explicit different start wins; empty starts default only in this term."""
    if semester_start:
        return deepcopy(AUTUMN_2026) if semester_start == AUTUMN_2026["semesterStart"] else None
    current = (on or today()).isoformat()
    if AUTUMN_2026["semesterStart"] <= current <= AUTUMN_2026["end"]:
        return deepcopy(AUTUMN_2026)
    return None


def default_semester_start(*, on: date | None = None) -> str | None:
    calendar = calendar_for(None, on=on)
    return calendar["semesterStart"] if calendar else None
