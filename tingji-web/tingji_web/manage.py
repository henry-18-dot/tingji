from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, create_all
from .models import User
from .security import hash_password, normalize_email


def main() -> None:
    parser = argparse.ArgumentParser(description="听记云端版数据库工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    seed = commands.add_parser("create-test-user")
    seed.add_argument("--email", default="student@example.edu")
    seed.add_argument("--password", default="student-pass-123")
    seed.add_argument("--name", default="测试同学")
    args = parser.parse_args()
    if args.command == "init-db":
        create_all()
        print("database ready")
    elif args.command == "create-test-user":
        if get_settings().app_env != "test":
            raise SystemExit("create-test-user is available only when APP_ENV=test")
        create_all()
        email = normalize_email(args.email)
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, name=args.name, password_hash=hash_password(args.password),
                            verified_at=datetime.now(timezone.utc))
                db.add(user)
            else:
                user.name, user.password_hash = args.name, hash_password(args.password)
                user.verified_at = datetime.now(timezone.utc)
            db.commit()
        print(f"test user ready: {email}")


if __name__ == "__main__":
    main()
