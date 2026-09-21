"""Account-scoped slide files, rendered pages, and durable page explanations."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import uid, utcnow


class SlideDeck(Base):
    __tablename__ = "slide_decks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id", ondelete="SET NULL"))
    note_id: Mapped[str | None] = mapped_column(ForeignKey("notes.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    expected_size: Mapped[int] = mapped_column(Integer)
    original: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[str] = mapped_column(String(24), default="uploading", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SlideChunk(Base):
    __tablename__ = "slide_upload_chunks"
    __table_args__ = (UniqueConstraint("deck_id", "chunk_index", name="uq_slide_chunk"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)


class SlidePage(Base):
    __tablename__ = "slide_pages"
    __table_args__ = (UniqueConstraint("deck_id", "number", name="uq_slide_page"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    text_method: Mapped[str] = mapped_column(String(24), default="pdf-text")
    image: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)


class SlideJob(Base):
    __tablename__ = "slide_jobs"
    __table_args__ = (Index("ix_slide_jobs_claim", "status", "created_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[str | None] = mapped_column(ForeignKey("slide_pages.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(8), default="zh")
    dedupe_key: Mapped[str] = mapped_column(String(180), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    request_state: Mapped[str] = mapped_column(String(24), default="prepared")
    content: Mapped[str] = mapped_column(Text, default="")
    context_snapshot: Mapped[str] = mapped_column(Text, default="")
    context_source: Mapped[str] = mapped_column(String(240), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_yuan: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    actual_yuan: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SlidePageLayout(Base):
    """OCR geometry of the immutable rendered page, shared by retries."""
    __tablename__ = "slide_page_layouts"
    page_id: Mapped[str] = mapped_column(ForeignKey("slide_pages.id", ondelete="CASCADE"), primary_key=True)
    layout_json: Mapped[dict] = mapped_column(JSON, default=dict)


class SlideLocalization(Base):
    """Retain the paid response before doing any fallible local rendering."""
    __tablename__ = "slide_localizations"
    job_id: Mapped[str] = mapped_column(ForeignKey("slide_jobs.id", ondelete="CASCADE"), primary_key=True)
    response_text: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
