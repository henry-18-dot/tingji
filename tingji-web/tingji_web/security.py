from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import get_settings


PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORDS.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return PASSWORDS.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def validate_password(password: str) -> None:
    if not isinstance(password, str) or not 4 <= len(password) <= 256:
        raise ValueError("密码需为 4—256 个字符。")


def normalize_email(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.count("@") != 1:
        raise ValueError("请输入有效的学校邮箱。")
    local, domain = text.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("请输入有效的学校邮箱。") from exc
    if not local or len(local) > 64 or len(text) > 320 or any(c.isspace() for c in text):
        raise ValueError("请输入有效的学校邮箱。")
    if not (domain.endswith(".edu") or domain.endswith(".edu.cn")):
        raise ValueError("仅支持以 .edu 或 .edu.cn 结尾的学校邮箱。")
    prefix = domain.removesuffix(".edu.cn") if domain.endswith(".edu.cn") else domain.removesuffix(".edu")
    if not prefix or prefix.endswith(".") or ".." in domain:
        raise ValueError("请输入有效的学校邮箱。")
    return f"{local}@{domain}"


def token_digest(token: str) -> str:
    return hmac.new(get_settings().app_secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(32)


def signed_csrf_token() -> str:
    stamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    body = f"{stamp}.{nonce}"
    signature = hmac.new(get_settings().app_secret_key.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def verify_signed_csrf(token: str, max_age_seconds: int = 3600) -> bool:
    try:
        stamp, nonce, signature = token.split(".", 2)
        issued = int(stamp)
    except (ValueError, AttributeError):
        return False
    if not nonce or issued > int(time.time()) + 30 or int(time.time()) - issued > max_age_seconds:
        return False
    body = f"{stamp}.{nonce}"
    expected = base64.urlsafe_b64encode(hmac.new(
        get_settings().app_secret_key.encode(), body.encode(), hashlib.sha256
    ).digest()).decode().rstrip("=")
    return hmac.compare_digest(signature, expected)


def expires_in(**kwargs: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
