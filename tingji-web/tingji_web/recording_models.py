"""Recording groups preserve every uploaded source without altering old notes."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import uid, utcnow


class RecordingBatch(Base):
    __tablename__ = "recording_batches"
    __table_args__ = (UniqueConstraint("user_id", "request_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64))
    note_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecordingInfo(Base):
    __tablename__ = "recording_info"
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    recording_date: Mapped[str] = mapped_column(String(10))
    auto_title: Mapped[bool] = mapped_column(Boolean, default=True)
    generated_title: Mapped[str] = mapped_column(String(160), default="")
    gaps: Mapped[list] = mapped_column(JSON, default=list)


class RecordingSegment(Base):
    __tablename__ = "recording_segments"
    __table_args__ = (UniqueConstraint("note_id", "position"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(240))
    expected_size: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(600), unique=True)
    duration: Mapped[float | None]


class RecordingMerge(Base):
    __tablename__ = "recording_merges"
    __table_args__ = (UniqueConstraint("user_id", "request_key"),)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(80))
    input_hash: Mapped[str] = mapped_column(String(64))
    source_ids: Mapped[list] = mapped_column(JSON)
