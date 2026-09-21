from __future__ import annotations

import logging
import smtplib
import threading
from email.message import EmailMessage
from urllib.parse import quote

from .config import get_settings


LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()
_OUTBOX: list[dict[str, str]] = []


class MailConfigurationError(RuntimeError):
    pass


def _failure_stage(exc: BaseException, current_stage: str) -> str:
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "mail_from"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "rcpt_to"
    if isinstance(exc, smtplib.SMTPDataError):
        return "data"
    return current_stage


def _smtp_code(exc: BaseException) -> int | None:
    value = getattr(exc, "smtp_code", None)
    return value if isinstance(value, int) else None


def send_message(recipient: str, subject: str, body: str) -> None:
    settings = get_settings()
    if settings.app_env == "test":
        with _LOCK:
            _OUTBOX.append({"to": recipient, "subject": subject, "body": body})
        return
    if not settings.smtp_host or not settings.smtp_from:
        raise MailConfigurationError("邮件服务尚未配置，请联系站长。")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    stage = "connect"
    try:
        smtp_class = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
        with smtp_class(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls and settings.smtp_port != 465:
                stage = "starttls"
                smtp.starttls()
            if settings.smtp_username:
                stage = "auth"
                smtp.login(settings.smtp_username, settings.smtp_password)
            stage = "send"
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        LOGGER.warning(
            "smtp_delivery_failed stage=%s exception_type=%s smtp_code=%s",
            _failure_stage(exc, stage),
            type(exc).__name__,
            _smtp_code(exc),
        )
        raise MailConfigurationError("验证邮件发送失败，请稍后重试。") from exc


def send_verification(email: str, token: str) -> None:
    url = get_settings().public_base_url + "/?verify=" + quote(token, safe="")
    send_message(email, "验证听记邮箱", f"请打开下面的链接验证邮箱：\n\n{url}\n\n链接会过期且只能使用一次。")


def send_reset(email: str, token: str) -> None:
    url = get_settings().public_base_url + "/?reset=" + quote(token, safe="")
    send_message(email, "重置听记密码", f"请打开下面的链接重置密码：\n\n{url}\n\n如果不是你发起的请求，可以忽略本邮件。")


def test_outbox() -> list[dict[str, str]]:
    if get_settings().app_env != "test":
        raise RuntimeError("测试邮件箱只在 APP_ENV=test 可用。")
    with _LOCK:
        return list(_OUTBOX)


def clear_test_outbox() -> None:
    if get_settings().app_env != "test":
        raise RuntimeError("测试邮件箱只在 APP_ENV=test 可用。")
    with _LOCK:
        _OUTBOX.clear()
