from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _decimal(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise RuntimeError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_secret_key: str
    public_base_url: str
    cookie_secure: bool
    database_url: str
    max_upload_bytes: int
    worker_poll_seconds: int
    session_ttl_days: int
    email_token_ttl_minutes: int
    reset_token_ttl_minutes: int
    admin_emails: frozenset[str]
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_starttls: bool
    tos_region: str
    tos_bucket: str
    tos_access_key_id: str
    tos_secret_access_key: str
    tos_session_token: str
    volcengine_asr_api_key: str
    volcengine_asr_app_id: str
    volcengine_asr_access_token: str
    deepseek_api_key: str
    deepseek_model: str
    asr_yuan_per_hour: Decimal
    deepseek_cache_hit_yuan_per_million: Decimal
    deepseek_cache_miss_yuan_per_million: Decimal
    deepseek_output_yuan_per_million: Decimal
    static_dir: Path
    local_mode: bool
    local_data_dir: Path
    legacy_data_dir: Path

    @property
    def production(self) -> bool:
        return self.app_env == "production"

    @property
    def mail_configured(self) -> bool:
        return self.app_env == "test" or bool(self.smtp_host and self.smtp_from)

    @property
    def tos_configured(self) -> bool:
        return all((self.tos_bucket, self.tos_access_key_id, self.tos_secret_access_key))

    @property
    def asr_configured(self) -> bool:
        return bool(self.volcengine_asr_api_key or
                    (self.volcengine_asr_app_id and self.volcengine_asr_access_token))

    @property
    def providers_configured(self) -> bool:
        return self.tos_configured and self.asr_configured and bool(self.deepseek_api_key)

    @property
    def allowed_origin(self) -> str:
        parsed = urlsplit(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env not in {"development", "test", "production"}:
        raise RuntimeError("APP_ENV must be development, test, or production")
    local_mode = _bool("TINGJI_LOCAL_MODE", False)
    if local_mode and app_env != "development":
        raise RuntimeError("TINGJI_LOCAL_MODE is only allowed with APP_ENV=development")
    secret = os.getenv("APP_SECRET_KEY", "").strip()
    if app_env == "production" and len(secret) < 32:
        raise RuntimeError("APP_SECRET_KEY must contain at least 32 characters in production")
    if not secret:
        secret = "development-only-secret-change-before-production"
    if local_mode and (len(secret) < 32 or secret.startswith("development-only")):
        raise RuntimeError("Local mode requires a persistent random APP_SECRET_KEY")
    base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
        raise RuntimeError("PUBLIC_BASE_URL must be an origin such as https://tingji.example")
    if app_env == "production" and parsed.scheme != "https":
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./data/tingji-web.sqlite3").strip()
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    root = Path(__file__).resolve().parents[1]
    if local_mode and not database_url.startswith("sqlite:///"):
        raise RuntimeError("Local mode requires its own SQLite database")
    return Settings(
        app_env=app_env,
        app_secret_key=secret,
        public_base_url=base_url,
        cookie_secure=_bool("COOKIE_SECURE", app_env == "production"),
        database_url=database_url,
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
        worker_poll_seconds=_int("WORKER_POLL_SECONDS", 5),
        session_ttl_days=_int("SESSION_TTL_DAYS", 30),
        email_token_ttl_minutes=_int("EMAIL_TOKEN_TTL_MINUTES", 60),
        reset_token_ttl_minutes=_int("RESET_TOKEN_TTL_MINUTES", 30),
        admin_emails=frozenset(x.strip().lower() for x in os.getenv("ADMIN_EMAILS", "").split(",") if x.strip()),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", "").strip(),
        smtp_starttls=_bool("SMTP_STARTTLS", True),
        tos_region=os.getenv("TOS_REGION", "cn-beijing").strip(),
        tos_bucket=os.getenv("TOS_BUCKET", "").strip(),
        tos_access_key_id=os.getenv("TOS_ACCESS_KEY_ID", "").strip(),
        tos_secret_access_key=os.getenv("TOS_SECRET_ACCESS_KEY", ""),
        tos_session_token=os.getenv("TOS_SESSION_TOKEN", ""),
        volcengine_asr_api_key=os.getenv("VOLCENGINE_ASR_API_KEY", "").strip(),
        volcengine_asr_app_id=os.getenv("VOLCENGINE_ASR_APP_ID", "").strip(),
        volcengine_asr_access_token=os.getenv("VOLCENGINE_ASR_ACCESS_TOKEN", "").strip(),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash",
        asr_yuan_per_hour=_decimal("ASR_YUAN_PER_HOUR", "0.8"),
        deepseek_cache_hit_yuan_per_million=_decimal("DEEPSEEK_CACHE_HIT_YUAN_PER_MILLION", "0.10"),
        deepseek_cache_miss_yuan_per_million=_decimal("DEEPSEEK_CACHE_MISS_YUAN_PER_MILLION", "3"),
        deepseek_output_yuan_per_million=_decimal("DEEPSEEK_OUTPUT_YUAN_PER_MILLION", "9"),
        static_dir=Path(os.getenv("STATIC_DIR", str(root / "public"))).resolve(),
        local_mode=local_mode,
        local_data_dir=Path(os.getenv("TINGJI_LOCAL_DATA_DIR", str(root.parent / "data" / "unified"))).resolve(),
        legacy_data_dir=Path(os.getenv("TINGJI_DATA_DIR", str(root.parent / "data"))).resolve(),
    )
