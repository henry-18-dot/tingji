"""Account-specific school sources, separate from retained course materials."""
from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
from .database import Base
from .models import utcnow


class SchoolSettings(Base):
    __tablename__ = "school_settings"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="南方科技大学")
    official_domain: Mapped[str] = mapped_column(String(253), default="sustech.edu.cn")
    calendar_url: Mapped[str] = mapped_column(String(1800), default="")
    syllabus_url: Mapped[str] = mapped_column(String(1800), default="")
    semester_start: Mapped[str | None] = mapped_column(String(10))
    semester_end: Mapped[str | None] = mapped_column(String(10))
    calendar: Mapped[dict] = mapped_column(JSON, default=dict)
    source_excerpt: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
