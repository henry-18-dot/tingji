from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, get_settings


ALGORITHM = "TOS4-HMAC-SHA256"
PREFIX = "tingji/users/"


class TosError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TosError("对象存储返回了重定向，请检查桶地域。")


def _configured(settings: Settings | None = None) -> Settings:
    cfg = settings or get_settings()
    if not cfg.tos_configured:
        raise TosError("对象存储尚未配置，请联系站长。")
    if not re.fullmatch(r"(?:cn|ap|eu|us)-[a-z]+(?:-\d+)?", cfg.tos_region):
        raise TosError("TOS_REGION 格式无效。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", cfg.tos_bucket):
        raise TosError("TOS_BUCKET 格式无效。")
    return cfg


def _host(cfg: Settings) -> str:
    return f"{cfg.tos_bucket}.tos-{cfg.tos_region}.volces.com"


def _query(params: dict[str, str]) -> str:
    return "&".join(urllib.parse.quote(str(key), safe="-_.~") + "=" +
                    urllib.parse.quote(str(params[key]), safe="-_.~") for key in sorted(params))


def _signature(method: str, object_key: str, query: dict[str, str], signed_headers: dict[str, str],
               cfg: Settings, timestamp: str, payload_hash: str = "UNSIGNED-PAYLOAD") -> tuple[str, str, str]:
    date = timestamp[:8]
    scope = f"{date}/{cfg.tos_region}/tos/request"
    headers = {key.lower(): str(value).strip() for key, value in signed_headers.items()}
    names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join((method.upper(), "/" + urllib.parse.quote(object_key, safe="/~"),
                                    _query(query), canonical_headers, names, payload_hash))
    to_sign = "\n".join((ALGORITHM, timestamp, scope,
                         hashlib.sha256(canonical_request.encode()).hexdigest()))
    key = cfg.tos_secret_access_key.encode()
    for component in (date, cfg.tos_region, "tos", "request"):
        key = hmac.new(key, component.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), scope, names


def _validate_key(object_key: str, user_id: str | None = None) -> None:
    expected = PREFIX + user_id + "/" if user_id else PREFIX
    if (not object_key.startswith(expected) or "\\" in object_key or
            any(part in {"", ".", ".."} for part in object_key.split("/")) or
            any(ord(c) < 32 for c in object_key)):
        raise TosError("对象路径无效。")


def presign(object_key: str, method: str = "GET", expires: int = 900, *, user_id: str | None = None) -> str:
    cfg = _configured()
    _validate_key(object_key, user_id)
    if method not in {"GET", "PUT", "HEAD"} or not 60 <= expires <= 86400:
        raise TosError("对象签名参数无效。")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    host = _host(cfg)
    params = {
        "X-Tos-Algorithm": ALGORITHM,
        "X-Tos-Credential": f"{cfg.tos_access_key_id}/{timestamp[:8]}/{cfg.tos_region}/tos/request",
        "X-Tos-Date": timestamp,
        "X-Tos-Expires": str(expires),
        "X-Tos-SignedHeaders": "host",
    }
    if cfg.tos_session_token:
        params["X-Tos-Security-Token"] = cfg.tos_session_token
    signature, _, _ = _signature(method, object_key, params, {"host": host}, cfg, timestamp)
    params["X-Tos-Signature"] = signature
    return f"https://{host}/" + urllib.parse.quote(object_key, safe="/~") + "?" + _query(params)


def head_object(object_key: str, *, user_id: str | None = None) -> dict[str, str | int]:
    request = urllib.request.Request(presign(object_key, "HEAD", 300, user_id=user_id), method="HEAD")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            length = int(response.headers.get("Content-Length", "-1"))
            return {"size": length, "etag": response.headers.get("ETag", ""),
                    "versionId": response.headers.get("x-tos-version-id", "")}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 404:
            raise TosError("尚未在对象存储中找到完整上传文件。") from None
        raise TosError(f"无法核对上传文件（TOS HTTP {status}）。", retryable=status in {408, 429} or status >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TosError("暂时无法核对上传文件，请稍后重试。", retryable=True) from exc


def download_file(object_key: str, destination: Path, *, user_id: str) -> None:
    cfg = get_settings()
    request = urllib.request.Request(presign(object_key, "GET", 1800, user_id=user_id), method="GET")
    total = 0
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=600) as response, destination.open("xb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > cfg.max_upload_bytes:
                    raise TosError("对象存储中的录音超过允许大小。")
                output.write(chunk)
    except TosError:
        destination.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        destination.unlink(missing_ok=True)
        raise TosError("下载待处理录音失败，请稍后恢复任务。", retryable=status in {408, 429} or status >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise TosError("下载待处理录音失败，请稍后恢复任务。", retryable=True) from exc


def upload_file(source: Path, object_key: str, *, user_id: str) -> dict[str, str | int]:
    cfg = _configured()
    _validate_key(object_key, user_id)
    size = source.stat().st_size
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    payload_hash = digest.hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = _host(cfg)
    content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    signed = {"host": host, "content-type": content_type, "x-tos-date": timestamp,
              "x-tos-content-sha256": payload_hash, "x-tos-acl": "private"}
    if cfg.tos_session_token:
        signed["x-tos-security-token"] = cfg.tos_session_token
    signature, scope, names = _signature("PUT", object_key, {}, signed, cfg, timestamp, payload_hash)
    headers = {**signed, "Content-Length": str(size),
               "Authorization": f"{ALGORITHM} Credential={cfg.tos_access_key_id}/{scope}, SignedHeaders={names}, Signature={signature}"}
    url = f"https://{host}/" + urllib.parse.quote(object_key, safe="/~")
    try:
        with source.open("rb") as stream:
            request = urllib.request.Request(url, data=stream, headers=headers, method="PUT")
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=600) as response:
                return {"size": size, "sha256": payload_hash, "etag": response.headers.get("ETag", "")}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise TosError("标准音频上传结果未确认；恢复任务前会先核对固定对象。",
                       retryable=status in {408, 429} or status >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TosError("标准音频上传结果未确认；恢复任务前会先核对固定对象。", retryable=True) from exc
