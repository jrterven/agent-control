from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuthSession, User
from .security import constant_time_hash_matches, random_token, token_hash, verify_password


SESSION_COOKIE = "hc_session"
_DUMMY_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$MFpucXpqM0RFdVNxNlN0ZQ$WlUyG5bCRH/gP+OZaFoDUvmRejbkxRBHsqKq4LzaYqs"


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
    if user is None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    if not verify_password(user.password_hash, password):
        return None
    return user


def issue_session(db: Session, user: User, *, ttl_hours: int) -> tuple[str, str, AuthSession]:
    token = random_token()
    csrf = csrf_for_session_token(token)
    row = AuthSession(
        token_hash=token_hash(token),
        csrf_hash=token_hash(csrf),
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return token, csrf, row


def csrf_for_session_token(token: str) -> str:
    # The opaque session token has 256 bits of entropy. Deriving a stable,
    # domain-separated synchronizer token keeps multiple browser tabs valid
    # without exposing or storing the cookie value in JavaScript.
    return token_hash(f"hermes-control:csrf:{token}")


def resolve_session(db: Session, token: str | None) -> AuthSession | None:
    if not token:
        return None
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    if row is None or row.revoked_at is not None or aware(row.expires_at) <= datetime.now(timezone.utc):
        return None
    if not row.user.is_active:
        return None
    return row


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


def current_auth_session(
    request: Request, db: Session = Depends(get_db)
) -> AuthSession:
    row = resolve_session(db, request.cookies.get(SESSION_COOKIE))
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return row


def current_user(auth_session: AuthSession = Depends(current_auth_session)) -> User:
    return auth_session.user


def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user


def require_csrf(
    request: Request,
    auth_session: AuthSession = Depends(current_auth_session),
) -> AuthSession:
    csrf = request.headers.get("X-CSRF-Token")
    if not csrf or not constant_time_hash_matches(auth_session.csrf_hash, csrf):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    return auth_session


def require_idempotency(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value or len(value) > 200:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key is required")
    return value
