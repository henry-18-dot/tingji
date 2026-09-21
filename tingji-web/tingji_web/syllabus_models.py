"""Durable public-syllabus discovery and compact course memories."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import utcnow


class CourseSyllabusJob(Base):
    __tablename__ = "course_syllabus_jobs"
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str] = mapped_column(String(240), default="")
    source_url: Mapped[str] = mapped_column(String(1600), default="")
    course_code: Mapped[str] = mapped_column(String(80), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MaterialMemory(Base):
    __tablename__ = "course_material_memories"
    material_id: Mapped[str] = mapped_column(ForeignKey("course_materials.id", ondelete="CASCADE"), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    summary: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(1600), default="")
    source_kind: Mapped[str] = mapped_column(String(32), default="upload")
    course_code: Mapped[str] = mapped_column(String(80), default="")
    source_updated_at: Mapped[str] = mapped_column(String(80), default="")
    extractor_version: Mapped[str] = mapped_column(String(40), default="extractive-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyllabusCache(Base):
    """Only public school documents are shared across accounts."""
    __tablename__ = "public_syllabus_cache"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    original_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
