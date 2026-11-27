"""Server-side browser sessions.

A sign-in is a row in `usersession` and a cookie that holds a random 256-bit id (only its SHA-256 is stored). The cookie
is HttpOnly, SameSite=Lax, Path=/ and (unless VG_COOKIE_SECURE=0, local http development only) Secure with the
`__Host-` prefix, which makes the browser refuse it from any other origin or with a Domain attribute.

Why an opaque id and not the JWT in the cookie: the database is already there, and with a row per sign-in the server can
end a session at once (logout, password change, role change, disabled or deleted account), and enforce an idle and an
absolute limit that no cookie attribute can be trusted to carry. The cookie's own lifetime (a session cookie, or a
Max-Age for "keep me signed in") is only a convenience for the browser.

The role is never copied into the session: get_current_user reads the user's row on every request.
"""
import hashlib
import secrets
from datetime import timedelta
from typing import Optional, Tuple

from fastapi import Request, Response
from sqlmodel import Session, delete, select

from . import audit
from .config import settings
from .invites import aware
from .models import User, UserSession
from .security import password_fingerprint
from .utils import get_utc_now

COOKIE_NAME_SECURE = "__Host-vg_session"
COOKIE_NAME_PLAIN = "vg_session"
_TOUCH_EVERY = timedelta(seconds=60)  # last_seen_at is written at most this often per session
_MAX_COOKIE_LENGTH = 200


def cookie_name() -> str:
    return COOKIE_NAME_SECURE if settings.COOKIE_SECURE else COOKIE_NAME_PLAIN


def hash_id(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _idle_limit(remember: bool) -> timedelta:
    return timedelta(days=settings.REMEMBER_IDLE_DAYS) if remember else timedelta(minutes=settings.SESSION_IDLE_MINUTES)


def _absolute_limit(remember: bool) -> timedelta:
    return timedelta(days=settings.REMEMBER_DAYS) if remember else timedelta(hours=settings.SESSION_ABSOLUTE_HOURS)


def _is_live(row: UserSession, now) -> bool:
    return aware(row.expires_at) > now and aware(row.last_seen_at) + _idle_limit(row.remember) > now


def create_session(
    db: Session, user: User, request: Optional[Request] = None, remember: bool = False
) -> Tuple[str, UserSession]:
    """Starts a session for `user` and commits it. Returns the raw id (for the cookie) and the row."""
    now = get_utc_now()
    raw = secrets.token_urlsafe(32)
    row = UserSession(
        token_hash=hash_id(raw),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + _absolute_limit(remember),
        remember=remember,
        pwv=password_fingerprint(user.password_hash),
        ip=audit.client_ip(request),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return raw, row


def lookup(db: Session, raw: Optional[str]) -> Optional[Tuple[UserSession, User]]:
    """The (session, user) a cookie value stands for, or None. A session that has run out, whose user is gone or
    disabled, or whose password has been changed since, is deleted on the spot."""
    if not raw or len(raw) > _MAX_COOKIE_LENGTH:
        return None
    row = db.exec(select(UserSession).where(UserSession.token_hash == hash_id(raw))).first()
    if row is None:
        return None
    now = get_utc_now()
    user = db.get(User, row.user_id)
    if (
        not _is_live(row, now) or user is None or user.disabled or user.pending
        or (row.pwv and row.pwv != password_fingerprint(user.password_hash))
    ):
        db.delete(row)
        db.commit()
        return None
    if now - aware(row.last_seen_at) > _TOUCH_EVERY:
        row.last_seen_at = now
        db.add(row)
        db.commit()
    return row, user


def is_alive(db: Session, session_id: int) -> bool:
    """Whether session number `session_id` still exists and has not run out (used for the signed file tokens a
    session handed out: when the session ends, so do they)."""
    row = db.get(UserSession, session_id)
    return row is not None and _is_live(row, get_utc_now())


def revoke(db: Session, raw: Optional[str]) -> None:
    """Ends the session a cookie value stands for. Does not commit (the caller's action does)."""
    if raw and len(raw) <= _MAX_COOKIE_LENGTH:
        db.exec(delete(UserSession).where(UserSession.token_hash == hash_id(raw)))


def revoke_user_sessions(db: Session, user_id: int) -> None:
    """Ends every session of a user (password change, role change, disabled, deleted). Does not commit."""
    db.exec(delete(UserSession).where(UserSession.user_id == user_id))


def purge_expired(db: Session) -> None:
    """Housekeeping: deletes the rows of sessions that have run out. Commits."""
    now = get_utc_now()
    db.exec(delete(UserSession).where(UserSession.expires_at <= now))
    for remember in (False, True):
        db.exec(delete(UserSession).where(
            UserSession.remember == remember, UserSession.last_seen_at <= now - _idle_limit(remember)
        ))
    db.commit()


def set_cookie(response: Response, raw: str, remember: bool = False) -> None:
    response.set_cookie(
        key=cookie_name(),
        value=raw,
        max_age=int(timedelta(days=settings.REMEMBER_DAYS).total_seconds()) if remember else None,  # None: session cookie
        path="/",            # (required by __Host-)
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )                        # no Domain: the cookie belongs to this exact host only


def clear_cookie(response: Response) -> None:
    """Expires the cookie: the same name and attributes, Max-Age=0 (a __Host- cookie is only overwritten by a
    Set-Cookie that also meets the prefix rules)."""
    response.delete_cookie(key=cookie_name(), path="/", secure=settings.COOKIE_SECURE, httponly=True, samesite="Lax")
