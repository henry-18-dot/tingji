"""Durable, account-scoped recording/slide matching; no existing columns change."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import uid, utcnow


class SlideNoteLink(Base):
    __tablename__ = "slide_note_links"
    __table_args__ = (UniqueConstraint("deck_id", "note_id", name="uq_slide_note_link"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="automatic")
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SlideMatchJob(Base):
    __tablename__ = "slide_match_jobs"
    __table_args__ = (UniqueConstraint("deck_id", "input_digest", name="uq_slide_match_input"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    input_digest: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    request_state: Mapped[str] = mapped_column(String(24), default="prepared")
    response_text: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    estimated_yuan: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    actual_yuan: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SlideLinkDecision(Base):
    """Append-only suppression history; source links and evidence stay intact."""
    __tablename__ = "slide_link_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("slide_decks.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20), default="reject")
    source: Mapped[str] = mapped_column(String(30), default="user")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
