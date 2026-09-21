"""V2 timetable tables. Existing courses and note history remain untouched."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import uid, utcnow


class TimetableState(Base):
    __tablename__ = "timetable_states"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    semester_start: Mapped[str | None] = mapped_column(String(10))
    weeks: Mapped[int] = mapped_column(Integer, default=20)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class CourseAppearance(Base):
    __tablename__ = "course_appearances"
    __table_args__ = (UniqueConstraint("user_id", "short_name", name="uq_appearance_user_short"),)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    short_name: Mapped[str] = mapped_column(String(12))
    color: Mapped[str] = mapped_column(String(7))


class TimetableSlot(Base):
    __tablename__ = "timetable_slots"
    __table_args__ = (UniqueConstraint("user_id", "course_id", "weekday", "period", name="uq_timetable_course_cell"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    period: Mapped[int] = mapped_column(Integer)
    weeks: Mapped[list] = mapped_column(JSON, default=list)
    location: Mapped[str] = mapped_column(String(120), default="")


class TimetableImport(Base):
    __tablename__ = "timetable_imports"
    __table_args__ = (UniqueConstraint("user_id", "sha256", name="uq_timetable_import_user_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(240))
    preview: Mapped[dict] = mapped_column(JSON)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(80))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
