"""Authenticated V2 timetable router. Mount before the static-file catch-all."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import require_csrf, require_user
from .schools import school_calendar, school_json, sync_school_start
from .assignments import assignments_json
from .database import get_db
from .logic import course_json
from .models import Course, User, utcnow
from .timetable import COLORS, EXTRACTOR_VERSION, MAX_FILE_BYTES, course_hotwords, extract_file, short_name
from .timetable_models import CourseAppearance, TimetableImport, TimetableSlot, TimetableState

router = APIRouter(prefix="/api", tags=["timetable"])
_import_capacity = threading.BoundedSemaphore(2)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotBody(Body):
    weekday: StrictInt = Field(ge=1, le=7)
    period: StrictInt = Field(ge=1, le=5)
    weeks: list[StrictInt] = Field(default_factory=lambda: list(range(1, 21)), min_length=1, max_length=20)
    location: str = Field(default="", max_length=120)


class AddSlotBody(SlotBody):
    courseId: str = Field(max_length=36)
    baseRevision: StrictInt | None = None


class BatchBody(Body):
    courseId: str = Field(max_length=36)
    slots: list[SlotBody] = Field(min_length=1, max_length=100)
    baseRevision: StrictInt | None = None


class NewCourseBody(Body):
    name: str = Field(min_length=1, max_length=120)
    slots: list[SlotBody] = Field(min_length=1, max_length=100)
    semesterStart: str | None = None
    baseRevision: StrictInt | None = None


class TimetablePatch(Body):
    semesterStart: str | None = None
    baseRevision: StrictInt | None = None


class AppearanceBody(Body):
    color: str | None = Field(default=None, max_length=7)
    shortName: str | None = Field(default=None, min_length=1, max_length=12)


class ImportBody(Body):
    filename: str = Field(min_length=1, max_length=240)
    contentBase64: str = Field(min_length=1, max_length=((MAX_FILE_BYTES + 2) // 3) * 4)


class PreviewCourse(Body):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    shortName: str = Field(default="", max_length=12)
    color: str = Field(default="", max_length=7)
    hotwords: str = Field(default="", max_length=4000)


class PreviewSlot(SlotBody):
    courseKey: str = Field(min_length=1, max_length=100)


class Preview(Body):
    semesterStart: str | None = None
    weeks: Literal[20] = 20
    courses: list[PreviewCourse] = Field(min_length=1, max_length=100)
    slots: list[PreviewSlot] = Field(min_length=1, max_length=300)


class ApplyBody(Body):
    importId: str = Field(max_length=36)
    preview: Preview | None = None
    mode: Literal["merge", "replace"] = "merge"
    baseRevision: StrictInt | None = None


def _course(db: Session, user: User, course_id: str) -> Course:
    course = db.scalar(select(Course).where(Course.id == course_id, Course.user_id == user.id))
    if course is None:
        raise HTTPException(404, "找不到这门课程。")
    return course


def _lock_state(db: Session, user: User, base_revision=None) -> TimetableState:
    # PostgreSQL serializes all schedule mutations for one account, including
    # collision detection and first-course creation. Other accounts stay free.
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    state = db.get(TimetableState, user.id)
    if state is None:
        state = TimetableState(user_id=user.id, weeks=20, revision=0)
        db.add(state)
        db.flush()
    if base_revision is not None and base_revision != state.revision:
        raise HTTPException(409, "课表已在其他页面更新，请刷新后重试。")
    return state


def _start(value):
    if value is None or value == "":
        return None
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("第一周周一需要填写 YYYY-MM-DD 格式的日期。") from None
    if result.weekday() != 0:
        raise ValueError("请选择第一周的周一。")
    return result.isoformat()


def _color(value: str):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("课程颜色格式不正确。")
    return value.upper()


def ensure_appearance(db: Session, course: Course) -> CourseAppearance:
    appearance = db.get(CourseAppearance, course.id)
    if appearance:
        return appearance
    existing = list(db.scalars(select(CourseAppearance).where(CourseAppearance.user_id == course.user_id)))
    appearance = CourseAppearance(user_id=course.user_id, course_id=course.id,
                                  short_name=short_name(course.name, [x.short_name for x in existing]),
                                  color=COLORS[len(existing) % len(COLORS)])
    db.add(appearance)
    db.flush()
    return appearance


def appearance_json(db: Session, course: Course) -> dict:
    """Optional helper for the existing /courses serializer; does not write."""
    appearance = db.get(CourseAppearance, course.id)
    return {"shortName": appearance.short_name, "color": appearance.color} if appearance else {
        "shortName": short_name(course.name), "color": COLORS[0]}


def timetable_json(db: Session, user: User) -> dict:
    state = db.get(TimetableState, user.id)
    calendar = school_calendar(db, user.id, state.semester_start if state else None)
    semester_start = (state.semester_start if state else None) or (calendar["semesterStart"] if calendar else None)
    courses = list(db.scalars(select(Course).where(Course.user_id == user.id).order_by(Course.created_at, Course.id)))
    appearances = {x.course_id: x for x in db.scalars(select(CourseAppearance).where(CourseAppearance.user_id == user.id))}
    occupied = [x.short_name for x in appearances.values()]
    course_values = []
    for course in courses:
        appearance = appearances.get(course.id)
        abbreviation = appearance.short_name if appearance else short_name(course.name, occupied)
        occupied.append(abbreviation)
        course_values.append({**course_json(course), "shortName": abbreviation,
                              "color": appearance.color if appearance else COLORS[len(course_values) % len(COLORS)]})
    slots = list(db.scalars(select(TimetableSlot).where(TimetableSlot.user_id == user.id).order_by(TimetableSlot.weekday, TimetableSlot.period)))
    return {"semesterStart": semester_start, "academicCalendar": calendar, "weeks": state.weeks if state else 20,
            "revision": state.revision if state else 0, "courses": course_values,
            "school": school_json(db, user.id), "assignments": assignments_json(db, user.id),
            "slots": [{"id": s.id, "courseId": s.course_id, "weekday": s.weekday, "period": s.period,
                       "weeks": s.weeks, "location": s.location} for s in slots]}


def _save_slots(db: Session, user: User, course: Course, slots: list[SlotBody]):
    existing = list(db.scalars(select(TimetableSlot).where(TimetableSlot.user_id == user.id)))
    for value in slots:
        weeks = sorted(set(value.weeks))
        if not weeks or any(w < 1 or w > 20 for w in weeks):
            raise ValueError("上课周次需要在第 1–20 周内。")
        cell = [s for s in existing if s.weekday == value.weekday and s.period == value.period]
        if any(s.course_id != course.id and set(s.weeks).intersection(weeks) for s in cell):
            raise HTTPException(409, "这个时间段已有课程，请选择空白区域或调整上课周次。")
        slot = next((s for s in cell if s.course_id == course.id), None)
        if slot:
            slot.weeks = sorted(set(slot.weeks) | set(weeks))
            if value.location.strip():
                slot.location = value.location.strip()
        else:
            slot = TimetableSlot(user_id=user.id, course_id=course.id, weekday=value.weekday,
                                 period=value.period, weeks=weeks, location=value.location.strip())
            existing.append(slot)
            db.add(slot)
    db.flush()


def _name_course(db: Session, user: User, name: str) -> Course:
    name = name.strip()
    if not name:
        raise ValueError("请填写课程名称。")
    course = db.scalar(select(Course).where(Course.user_id == user.id, Course.name == name).order_by(Course.created_at.desc()))
    if course is None:
        course = Course(user_id=user.id, name=name, term="", teacher="", prompt="", hotwords=course_hotwords(name))
        db.add(course)
        db.flush()
    elif not course.hotwords.strip():
        course.hotwords = course_hotwords(name)
    ensure_appearance(db, course)
    return course


@router.get("/timetable")
def get_timetable(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return timetable_json(db, user)


@router.patch("/timetable", dependencies=[Depends(require_csrf)])
def patch_timetable(body: TimetablePatch, user: User = Depends(require_user), db: Session = Depends(get_db)):
    state = _lock_state(db, user, body.baseRevision)
    if "semesterStart" in body.model_fields_set:
        state.semester_start = _start(body.semesterStart)
        sync_school_start(db,user.id,state.semester_start)
    state.revision += 1
    db.commit()
    return timetable_json(db, user)


@router.post("/timetable/courses", dependencies=[Depends(require_csrf)])
def create_timetable_course(body: NewCourseBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    state = _lock_state(db, user, body.baseRevision)
    course = _name_course(db, user, body.name)
    _save_slots(db, user, course, body.slots)
    if "semesterStart" in body.model_fields_set:
        state.semester_start = _start(body.semesterStart)
        sync_school_start(db,user.id,state.semester_start)
    state.revision += 1
    db.commit()
    return timetable_json(db, user)


@router.post("/timetable/slots", dependencies=[Depends(require_csrf)])
def add_slot(body: AddSlotBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return add_slots(BatchBody(courseId=body.courseId, slots=[SlotBody(**body.model_dump(exclude={"courseId", "baseRevision"}))], baseRevision=body.baseRevision), user, db)


@router.post("/timetable/slots/batch", dependencies=[Depends(require_csrf)])
def add_slots(body: BatchBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    state = _lock_state(db, user, body.baseRevision)
    _save_slots(db, user, _course(db, user, body.courseId), body.slots)
    state.revision += 1
    db.commit()
    return timetable_json(db, user)


@router.delete("/timetable/slots/{slot_id}", dependencies=[Depends(require_csrf)])
def delete_slot(slot_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    state = _lock_state(db, user)
    slot = db.scalar(select(TimetableSlot).where(TimetableSlot.id == slot_id, TimetableSlot.user_id == user.id))
    if slot is None:
        raise HTTPException(404, "找不到这个上课时段。")
    db.delete(slot)
    state.revision += 1
    db.commit()
    return timetable_json(db, user)


@router.patch("/courses/{course_id}/appearance", dependencies=[Depends(require_csrf)])
def patch_appearance(course_id: str, body: AppearanceBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _lock_state(db, user)
    course = _course(db, user, course_id)
    appearance = ensure_appearance(db, course)
    if body.color is not None:
        appearance.color = _color(body.color)
    if body.shortName is not None:
        value = body.shortName.strip()
        if not value:
            raise ValueError("课程简称不能为空。")
        occupied = db.scalar(select(CourseAppearance).where(CourseAppearance.user_id == user.id,
                            func.lower(CourseAppearance.short_name) == value.lower(), CourseAppearance.course_id != course.id))
        if occupied:
            raise HTTPException(409, "这个简称已用于另一门课程，请换一个。")
        appearance.short_name = value
    db.commit()
    return {"course": {**course_json(course), **appearance_json(db, course)}}


def _import_json(record: TimetableImport) -> dict:
    return {"importId": record.id, "preview": record.preview, "warnings": record.warnings,
            "method": record.method, "alreadyApplied": bool(record.applied_at)}


def _pending_import(db: Session, user: User, digest: str):
    """Saved or empty previews get a new draft; successful drafts stay idempotent."""
    while True:
        record = db.scalar(select(TimetableImport).where(TimetableImport.user_id == user.id, TimetableImport.sha256 == digest))
        if record is None or (not record.applied_at and record.preview.get("slots")):
            return digest, record
        digest = hashlib.sha256((digest + ":" + record.id).encode()).hexdigest()


@router.post("/timetable/import", dependencies=[Depends(require_csrf)])
def import_timetable(body: ImportBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    filename = Path(body.filename.replace("\\", "/")).name
    try:
        content = base64.b64decode(body.contentBase64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("课表文件传输不完整，请重新选择文件。") from None
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("课表文件需要非空，且不超过 3 MB。")
    # A failed result from an older extractor must not permanently mask a fix.
    # Include the format because identical bytes can be exported under different
    # supported extensions; retain old imports and applied schedules untouched.
    digest = hashlib.sha256(EXTRACTOR_VERSION.encode() + Path(filename).suffix.lower().encode() + b"\0" + content).hexdigest()
    digest, existing = _pending_import(db, user, digest)
    if existing:
        return _import_json(existing)
    count = db.scalar(select(func.count()).select_from(TimetableImport).where(
        TimetableImport.user_id == user.id, TimetableImport.created_at >= utcnow() - timedelta(hours=1)))
    if count >= 15:
        raise HTTPException(429, "本小时导入次数较多，请稍后再试。")
    if not _import_capacity.acquire(blocking=False):
        raise HTTPException(503, "课表识别正在处理其他文件，请稍后重试。")
    try:
        result = extract_file(filename, content)
    finally:
        _import_capacity.release()
    # Lock only after OCR; do not hold account locks while processing a document.
    state = _lock_state(db, user)
    digest, existing = _pending_import(db, user, digest)
    if existing:
        return _import_json(existing)
    if not result["preview"].get("semesterStart"):
        result["preview"]["semesterStart"] = state.semester_start or (school_calendar(db,user.id) or {}).get("semesterStart")
    record = TimetableImport(user_id=user.id, sha256=digest, filename=filename, **result)
    db.add(record)
    db.commit()
    return _import_json(record)


@router.post("/timetable/apply", dependencies=[Depends(require_csrf)])
def apply_timetable(body: ApplyBody, user: User = Depends(require_user), db: Session = Depends(get_db)):
    state = _lock_state(db, user)
    record = db.scalar(select(TimetableImport).where(TimetableImport.id == body.importId, TimetableImport.user_id == user.id))
    if record is None:
        raise HTTPException(404, "找不到这次课表导入，请重新选择文件。")
    if record.applied_at:
        return timetable_json(db, user)
    if body.baseRevision is not None and body.baseRevision != state.revision:
        raise HTTPException(409, "课表已在其他页面更新，请刷新后重试。")
    try:
        preview = body.preview or Preview.model_validate(record.preview)
    except ValueError:
        raise ValueError("请先确认课程名称和上课时段，再保存课表。") from None
    keys = [c.key for c in preview.courses]
    if len(keys) != len(set(keys)) or len({c.name.strip() for c in preview.courses}) != len(keys):
        raise ValueError("预览中的课程不能重名或使用重复编号。")
    if any(s.courseKey not in keys for s in preview.slots):
        raise ValueError("课表时段引用了不存在的课程。")
    if body.mode == "replace":
        db.execute(delete(TimetableSlot).where(TimetableSlot.user_id == user.id))
        db.flush()
    for item in preview.courses:
        course = _name_course(db, user, item.name)
        from .syllabus import enqueue_syllabus
        enqueue_syllabus(db, course)
        # Names, vocabulary and colors are generated server-side. Client edits
        # may choose a valid color; abbreviations stay unique across the account.
        appearance = ensure_appearance(db, course)
        if item.color:
            appearance.color = _color(item.color)
        _save_slots(db, user, course, [s for s in preview.slots if s.courseKey == item.key])
    if not state.semester_start:
        state.semester_start = _start(preview.semesterStart) or (school_calendar(db,user.id) or {}).get("semesterStart")
        sync_school_start(db,user.id,state.semester_start)
    state.revision += 1
    record.applied_at = utcnow()
    from .slide_matching import enqueue_user_matching
    enqueue_user_matching(db, user.id)
    db.commit()
    return timetable_json(db, user)
