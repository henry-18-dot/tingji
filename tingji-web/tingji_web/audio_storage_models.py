"""Explicit audio cleanup previews and per-object removal receipts."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import uid, utcnow


class AudioCleanupPlan(Base):
    __tablename__ = "audio_cleanup_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scope: Mapped[str] = mapped_column(String(32))
    note_ids: Mapped[list] = mapped_column(JSON)
    targets: Mapped[list] = mapped_column(JSON)
    skipped: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AudioRemoval(Base):
    __tablename__ = "audio_removals"
    __table_args__ = (UniqueConstraint("user_id", "locator_hash", name="uq_audio_removal_owner_locator"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)
    locator_hash: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    removed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
