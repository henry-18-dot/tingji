"""Inspect/export legacy text or add a verified copy to an existing account."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tingji_web.legacy_import import export_transcripts, import_legacy, load_transcript_bundle, scan_legacy


def main():
    parser = argparse.ArgumentParser(description="复制旧版听记数据；默认只检查，--apply 才写入目标库。")
    source_args = parser.add_mutually_exclusive_group()
    source_args.add_argument("--source-root", type=Path, default=None)
    source_args.add_argument("--bundle", type=Path)
    parser.add_argument("--mode", choices=("full", "transcripts_only"), default="transcripts_only")
    parser.add_argument("--export", type=Path)
    owner = parser.add_mutually_exclusive_group()
    owner.add_argument("--user-id")
    owner.add_argument("--email")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.bundle and args.mode != "transcripts_only":
        parser.error("原文包只能使用 transcripts_only 模式。")
    source = load_transcript_bundle(args.bundle) if args.bundle else scan_legacy(args.source_root or ROOT.parent)
    if args.export:
        if args.mode != "transcripts_only" or args.apply or args.user_id or args.email:
            parser.error("--export 仅用于导出原文，不同时写入目标账号。")
        result = export_transcripts(source, args.export)
    elif args.user_id or args.email:
        from sqlalchemy import select
        from tingji_web.database import SessionLocal
        from tingji_web.models import User
        with SessionLocal() as db:
            user = db.get(User, args.user_id) if args.user_id else db.scalar(select(User).where(User.email == args.email.strip().lower()))
            if user is None:
                raise ValueError("目标账号不存在，未写入任何内容。")
            result = import_legacy(db, user, source, mode=args.mode, dry_run=not args.apply)
            if args.apply:
                db.commit()
    else:
        if args.apply:
            parser.error("--apply 需要明确 --user-id 或 --email。")
        result = source.report(args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
