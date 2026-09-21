"""Local identity and the existing owner-only Tailscale Serve boundary."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from .config import get_settings
from .models import User


LOCAL_OWNER_ID = "00000000-0000-4000-8000-000000000001"


def ensure_local_owner(db: Session) -> User:
    if not get_settings().local_mode:
        raise RuntimeError("Local identity is unavailable outside local mode")
    user = db.get(User, LOCAL_OWNER_ID)
    if user is None:
        user = User(id=LOCAL_OWNER_ID, email="local@tingji.invalid", name="本地用户",
                    password_hash="local-device-identity-disabled-password",
                    verified_at=datetime.now(timezone.utc), is_admin=True)
        db.add(user)
        db.commit()
    return user


def remote_configuration() -> dict:
    path = get_settings().legacy_data_dir / "remote.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        origin = str(data.get("url", "")).rstrip("/")
        parsed = urlsplit(origin)
        owner = data.get("ownerLogin")
        if (parsed.scheme != "https" or
                not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", parsed.netloc) or
                parsed.path or parsed.query or parsed.fragment or not isinstance(owner, str) or
                not owner or any(ord(c) < 32 for c in owner)):
            return {}
        return {"url": origin, "host": parsed.netloc, "ownerLogin": owner}
    except (ValueError, OSError, TypeError):
        return {}


def allowed_request(headers, *, method: str, path: str) -> None:
    """Serve injects owner identity; binding stays loopback-only in start-local.py."""
    parsed = urlsplit(get_settings().public_base_url)
    port = parsed.port or 80
    host = headers.get("Host", "")
    local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    identity = headers.get("Tailscale-User-Login", "")
    forwarded = headers.get("X-Forwarded-For") or headers.get("X-Forwarded-Proto")
    if identity or forwarded or host not in local_hosts:
        remote = remote_configuration()
        if not remote or host != remote["host"] or identity != remote["ownerLogin"]:
            raise PermissionError("只有已配置的本人私有网络账号可以访问听记。")
        expected_origin = remote["url"]
    else:
        expected_origin = "http://" + host
    origin = headers.get("Origin", "")
    if origin and origin != expected_origin:
        raise PermissionError("不接受其他网站发起的请求。")
    if path.startswith("/api/") and method not in {"GET", "HEAD", "OPTIONS"} and not origin:
        raise PermissionError("写入请求需要页面来源。")


def transfer_url() -> str:
    remote = remote_configuration()
    return remote["url"] + "/?transfer=1" if remote else ""
