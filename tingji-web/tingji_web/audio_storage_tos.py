"""Signed HEAD/DELETE of a single approved TOS object; never list a bucket."""
from datetime import datetime, timezone
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from . import tos


def _request(key, method, user_id, version=""):
    cfg = tos._configured()
    tos._validate_key(key, user_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    host = tos._host(cfg)
    query = {"versionId": version} if version and version != "null" else {}
    payload = hashlib.sha256(b"").hexdigest()
    signed = {"host": host, "x-tos-date": stamp, "x-tos-content-sha256": payload}
    if cfg.tos_session_token:
        signed["x-tos-security-token"] = cfg.tos_session_token
    signature, scope, names = tos._signature(method, key, query, signed, cfg, stamp, payload)
    headers = {**signed, "Authorization": f"{tos.ALGORITHM} Credential={cfg.tos_access_key_id}/{scope}, SignedHeaders={names}, Signature={signature}"}
    url = f"https://{host}/" + urllib.parse.quote(key, safe="/~") + ("?" + tos._query(query) if query else "")
    return urllib.request.Request(url, method=method, headers=headers)


def cloud_stat(key, *, user_id, version=""):
    try:
        with urllib.request.build_opener(tos._NoRedirect()).open(_request(key, "HEAD", user_id, version), timeout=20) as response:
            return {"size": int(response.headers.get("Content-Length", "-1")),
                    "etag": response.headers.get("ETag", ""), "versionId": response.headers.get("x-tos-version-id", "")}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 404:
            return None
        raise ValueError(f"无法读取云端录音（HTTP {status}）。") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ValueError("云端录音状态暂未确认，请稍后重试。") from exc


def cloud_delete(key, *, user_id, identity):
    version = str(identity.get("versionId") or "")
    current = cloud_stat(key, user_id=user_id, version=version)
    if current is None:
        return
    if current != identity:
        raise ValueError("云端录音已变化，请重新预览。")
    try:
        with urllib.request.build_opener(tos._NoRedirect()).open(_request(key, "DELETE", user_id, version), timeout=20):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        # A lost DELETE response is resolved by checking the same object/version.
        # Never report success solely from a sent request or an HTTP 2xx.
        if cloud_stat(key, user_id=user_id, version=version) is None:
            return
        raise ValueError("删除结果暂未确认，可重试此项。") from None
    if cloud_stat(key, user_id=user_id, version=version) is not None:
        raise ValueError("云端仍能读取这份录音，请重试此项。")
