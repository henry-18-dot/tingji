"""Run the shared web app against an independent local database."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WEB_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(WEB_ROOT))


def configure(port: int, local_data: Path | None = None) -> Path:
    legacy_data = Path(os.environ.get("TINGJI_DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()
    local_data = (local_data or legacy_data / "unified").resolve()
    local_data.mkdir(parents=True, exist_ok=True)
    from tingji.storage import protect
    secret_path = local_data / "app-secret.dpapi"
    if not secret_path.exists():
        encrypted = protect(secrets.token_urlsafe(48))
        try:
            with secret_path.open("x", encoding="utf-8") as handle:
                handle.write(encrypted)
        except FileExistsError:
            pass
    secret = protect(secret_path.read_text(encoding="utf-8"), decrypt=True)
    os.environ.update({"APP_ENV": "development", "TINGJI_LOCAL_MODE": "true",
                       "TINGJI_LOCAL_DATA_DIR": str(local_data), "TINGJI_DATA_DIR": str(legacy_data),
                       "APP_SECRET_KEY": secret, "COOKIE_SECURE": "false",
                       "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
                       "DATABASE_URL": "sqlite:///" + (local_data / "tingji.sqlite3").as_posix(),
                       "STATIC_DIR": str(WEB_ROOT / "public")})
    legacy_db = legacy_data / "notes.sqlite3"
    if legacy_db.is_file():
        with sqlite3.connect(legacy_db.as_uri() + "?mode=ro", uri=True) as db:
            stored = dict(db.execute("SELECT key,value FROM settings"))
        plain = {"asrAppId": "VOLCENGINE_ASR_APP_ID", "tosRegion": "TOS_REGION", "tosBucket": "TOS_BUCKET"}
        protected = {"asrApiKey": "VOLCENGINE_ASR_API_KEY", "asrAccessToken": "VOLCENGINE_ASR_ACCESS_TOKEN",
                     "deepseekApiKey": "DEEPSEEK_API_KEY", "tosAccessKeyId": "TOS_ACCESS_KEY_ID",
                     "tosSecretAccessKey": "TOS_SECRET_ACCESS_KEY", "tosSessionToken": "TOS_SESSION_TOKEN"}
        for source, target in plain.items():
            if stored.get(source):
                os.environ[target] = stored[source]
        for source, target in protected.items():
            if stored.get(source):
                os.environ[target] = protect(stored[source], decrypt=True)
        if stored.get("tosPrivateConfirmed") != "true":
            for key in ("TOS_BUCKET", "TOS_ACCESS_KEY_ID", "TOS_SECRET_ACCESS_KEY", "TOS_SESSION_TOKEN"):
                os.environ.pop(key, None)
    # Desktop shortcuts may inherit a PATH from before FFmpeg was installed.
    if not shutil.which("ffmpeg") and os.name == "nt":
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Packages"
        candidates = sorted(packages.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe"))
        if candidates:
            os.environ["PATH"] = str(candidates[-1].parent) + os.pathsep + os.environ.get("PATH", "")
    return local_data


def main() -> None:
    parser = argparse.ArgumentParser(description="启动听记本地应用（与网站共用功能）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-worker", action="store_true", help="仅供界面检查，暂停后台处理")
    parser.add_argument("--check", action="store_true", help="只核对运行配置，不启动服务或处理任务")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    configure(args.port, args.data_dir)
    from tingji_web.config import get_settings
    settings = get_settings()
    if args.check:
        print(json.dumps({"localMode": settings.local_mode, "providersConfigured": settings.providers_configured,
                          "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}))
        return
    from tingji_web.app import app
    from tingji_web.database import SessionLocal, create_all
    from tingji_web.local_runtime import ensure_local_owner
    from tingji_web import worker
    import uvicorn
    create_all()
    with SessionLocal() as db:
        ensure_local_owner(db)
    stop = threading.Event()

    def work() -> None:
        worker.recover_stale_jobs()
        while not stop.is_set():
            try:
                found = worker.run_once()
            except Exception:
                # Provider state is persisted by the shared worker; never print credentials.
                found = False
                print("Local worker will retry reading its queue.", file=sys.stderr, flush=True)
            if not found:
                stop.wait(settings.worker_poll_seconds)

    thread = None
    if not args.no_worker:
        thread = threading.Thread(target=work, name="tingji-local-worker", daemon=True)
        thread.start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)
    finally:
        stop.set()
        if thread:
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
