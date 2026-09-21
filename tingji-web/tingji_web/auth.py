from __future__ import annotations

import hmac
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .config import get_settings
from .models import LoginSession, User
from .security import aware, token_digest, verify_signed_csrf


COOKIE_NAME = "tingji_session"


def session_record(request: Request, db: Session) -> LoginSession | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    record = db.scalar(select(LoginSession).where(LoginSession.token_hash == token_digest(raw)))
    if record is None or aware(record.expires_at) <= datetime.now(timezone.utc):
        return None
    return record


def optional_user(request: Request, db: Session) -> User | None:
    if get_settings().local_mode:
        from .local_runtime import ensure_local_owner
        return ensure_local_owner(db)
    record = session_record(request, db)
    return db.get(User, record.user_id) if record else None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = optional_user(request, db)
    if user is None:
        raise HTTPException(401, "请先登录。")
    return user


def require_verified(user: User = Depends(require_user)) -> User:
    if user.verified_at is None:
        raise HTTPException(403, "请先通过学校邮箱验证。")
    return user


def require_csrf(request: Request, db: Session = Depends(get_db)) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    record = session_record(request, db)
    valid = hmac.compare_digest(supplied, record.csrf_token) if record else verify_signed_csrf(supplied)
    if not valid:
        raise HTTPException(403, "页面验证已过期，请刷新后重试。")
