"""Library organization and durable receipts for bulk actions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .models import uid, utcnow


class NoteLibrary(Base):
    __tablename__ = "note_library"
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    before_trash: Mapped[dict] = mapped_column(JSON, default=dict)


class LibraryOperation(Base):
    __tablename__ = "library_operations"
    __table_args__ = (UniqueConstraint("user_id", "request_id", name="uq_library_user_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
