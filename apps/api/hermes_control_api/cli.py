from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from .config import get_settings
from .database import Base, build_engine, build_session_factory
from .models import User
from .security import hash_password


def create_admin(username: str) -> None:
    password = getpass.getpass("New admin password (minimum 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Admin password must be at least 12 characters")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    settings = get_settings()
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as db:
        if db.scalar(select(User).where(User.username == username)) is not None:
            raise SystemExit("User already exists")
        db.add(User(username=username, password_hash=hash_password(password), is_admin=True))
        db.commit()
    print(f"Created administrator {username!r}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-control-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-admin", help="Create the first local administrator")
    create.add_argument("--username", default="admin")
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username)


if __name__ == "__main__":
    main()
