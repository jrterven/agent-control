from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from .config import get_settings
from .database import Base, build_engine, build_session_factory
from .models import Gateway, User
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
        user = User(username=username, password_hash=hash_password(password), is_admin=True)
        db.add(user)
        db.flush()
        for gateway in db.scalars(select(Gateway).where(Gateway.owner_id.is_(None))):
            gateway.owner_id = user.id
        db.commit()
    print(f"Created administrator {username!r}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="hermes-control-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-admin", help="Create the first local administrator")
    create.add_argument("--username", default="admin")
    invite = subparsers.add_parser("invite", help="Invite a verified Google email to the cloud beta")
    invite.add_argument("--email", required=True)
    invite.add_argument("--days", type=int, choices=range(1, 91), default=14)
    grant = subparsers.add_parser("grant-platform-admin", help="Promote an existing accepted cloud identity")
    grant.add_argument("--email", required=True)
    args = parser.parse_args()
    if args.command == "create-admin":
        create_admin(args.username)
    elif args.command == "grant-platform-admin":
        from .cloud_auth import normalized_email
        from .models import ExternalIdentity
        settings = get_settings()
        if settings.deployment_mode != "cloud":
            raise SystemExit("Platform administration requires cloud mode")
        factory = build_session_factory(build_engine(settings))
        with factory() as db:
            try:
                email = normalized_email(args.email)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            identities = list(db.scalars(select(ExternalIdentity).where(ExternalIdentity.email == email)).all())
            if len(identities) != 1:
                raise SystemExit("An unambiguous accepted Google identity is required")
            user = db.get(User, identities[0].user_id)
            if user is None or not user.is_active:
                raise SystemExit("An active, accepted Google identity is required")
            user.is_admin = True
            db.commit()
            print(f"Granted platform administration to {email}")
    elif args.command == "invite":
        from .cloud_auth import invite_email
        settings = get_settings()
        if settings.deployment_mode != "cloud":
            raise SystemExit("Invitations require cloud mode")
        factory = build_session_factory(build_engine(settings))
        with factory() as db:
            try:
                invitation = invite_email(db, settings, args.email, days=args.days)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            print(f"Invited {invitation.email}; expires {invitation.expires_at.isoformat()}")


if __name__ == "__main__":
    main()
