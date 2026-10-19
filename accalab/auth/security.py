"""Password, one-time token, and persistent login-throttle services."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.orm import Session

from accalab.models import AuthToken, LoginRateLimit, User, utc_now

_hasher = PasswordHasher()
TOKEN_PURPOSES = {"verify_email", "password_reset"}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LOCK = timedelta(minutes=15)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except Exception:
        return False


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_auth_token(
    session: Session, user: User, purpose: str, *, ttl: timedelta = timedelta(hours=2)
) -> str:
    if purpose not in TOKEN_PURPOSES:
        raise ValueError("Unsupported authentication token purpose.")
    raw = secrets.token_urlsafe(32)
    session.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_digest(raw),
            expires_at=utc_now() + ttl,
        )
    )
    session.flush()
    return raw


def consume_auth_token(session: Session, raw: str, purpose: str) -> User | None:
    token = session.scalar(select(AuthToken).where(AuthToken.token_hash == _digest(raw)))
    now = utc_now()
    if (
        token is None
        or token.purpose != purpose
        or token.consumed_at is not None
        or token.expires_at <= now
    ):
        return None
    token.consumed_at = now
    return session.get(User, token.user_id)


def login_is_locked(session: Session, email: str, now: datetime | None = None) -> bool:
    now = now or utc_now()
    row = session.get(LoginRateLimit, _digest(normalize_email(email)))
    if row is None:
        return False
    if row.locked_until and row.locked_until > now:
        return True
    return row.window_started_at + LOGIN_WINDOW > now and row.attempts >= MAX_LOGIN_ATTEMPTS


def record_login_failure(session: Session, email: str, now: datetime | None = None) -> None:
    now = now or utc_now()
    key = _digest(normalize_email(email))
    row = session.get(LoginRateLimit, key)
    if row is None:
        row = LoginRateLimit(key_hash=key, attempts=0, window_started_at=now)
        session.add(row)
    elif row.window_started_at + LOGIN_WINDOW <= now:
        row.attempts = 0
        row.window_started_at = now
        row.locked_until = None
    row.attempts += 1
    if row.attempts >= MAX_LOGIN_ATTEMPTS:
        row.locked_until = now + LOGIN_LOCK


def clear_login_failures(session: Session, email: str) -> None:
    row = session.get(LoginRateLimit, _digest(normalize_email(email)))
    if row is not None:
        session.delete(row)
