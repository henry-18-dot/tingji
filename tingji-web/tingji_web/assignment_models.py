"""Extracted coursework, with stable source identity and manual-edit protection."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import uid, utcnow


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("user_id", "source_key", name="uq_assignment_source"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"), index=True)
    source_key: Mapped[str] = mapped_column(String(180))
    source_type: Mapped[str] = mapped_column(String(12))
    note_id: Mapped[str | None] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[str | None] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    details: Mapped[str] = mapped_column(Text)
    source_excerpt: Mapped[str] = mapped_column(Text)
    due_date: Mapped[str | None] = mapped_column(String(10), index=True)
    due_time: Mapped[str | None] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(12), default="open")
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
