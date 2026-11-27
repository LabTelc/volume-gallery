import hashlib
import hmac
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlmodel import Session, col, delete, select, update

from .. import audit, sessions
from ..config import settings
from ..database import get_session
from ..deps import get_current_user
from ..models import AuditEvent, User, UserSession, UserToken
from ..security import (
    verify_password, needs_rehash, hash_password, validate_new_password, create_access_token, password_fingerprint,
    _DUMMY_HASH,
)
from ..schemas import SessionInfo, SetPassword, SetPasswordCheck, SetPasswordInfo
from ..invites import aware, hash_token
from ..utils import get_utc_now

router = APIRouter(prefix="/api/auth", tags=["auth"])


def start_session(db: Session, request: Request, response: Response, user: User, remember: bool = False) -> SessionInfo:
    """Signs `user` in on this browser: a new session id (never one the browser sent: no session fixation), put into
    the cookie, and the answer the frontend needs. While VG_ALLOW_BEARER_AUTH is on, the answer also carries a bearer
    token for API / CLI clients."""
    sessions.purge_expired(db)
    raw, _row = sessions.create_session(db, user, request, remember=remember)
    sessions.set_cookie(response, raw, remember=remember)
    info = SessionInfo(username=user.username, role=user.role)
    if settings.ALLOW_BEARER_AUTH:
        info.access_token = create_access_token(
            subject=user.username, role=user.role.value, pwv=password_fingerprint(user.password_hash)
        )
        info.token_type = "bearer"
    return info


# ----------------------------------------------------------------------------- sign-in throttle
# Lockout from the audit log: failed sign-ins carry a keyed hash of the name that was tried (known account or not, so
# the lockout does not tell which names exist; the name itself is not stored, people paste passwords into that box).
# Three limits, each over the last VG_LOGIN_WINDOW_MINUTES: one name from one address (a successful sign-in from that
# address clears it), one address with any name, one name from anywhere. nginx limits the request rate on top
# (nginx.conf.template); the address is only as trustworthy as X-Real-IP (see audit.client_ip).

def _name_key(username: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), username.strip().lower().encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def _count(db: Session, action: str, since, *, ip: Optional[str] = None, key: Optional[str] = None) -> int:
    stmt = select(func.count(AuditEvent.id)).where(AuditEvent.action == action, AuditEvent.at > since)
    if ip is not None:
        stmt = stmt.where(AuditEvent.ip == ip)
    if key is not None:
        stmt = stmt.where(col(AuditEvent.detail).contains(f'"key": "{key}"'))
    return db.exec(stmt).one()


def _login_locked(db: Session, request: Request, key: str) -> bool:
    ip = audit.client_ip(request)
    since = get_utc_now() - timedelta(minutes=settings.LOGIN_WINDOW_MINUTES)
    last_ok = db.exec(
        select(func.max(AuditEvent.at)).where(
            AuditEvent.action == "auth.login", AuditEvent.ip == ip, col(AuditEvent.detail).contains(f'"key": "{key}"')
        )
    ).one()
    since_pair = max(since, aware(last_ok)) if last_ok else since
    return (
        _count(db, "auth.login_failed", since_pair, ip=ip, key=key) >= settings.LOGIN_MAX_FAILURES
        or _count(db, "auth.login_failed", since, ip=ip) >= settings.LOGIN_MAX_FAILURES_PER_IP
        or _count(db, "auth.login_failed", since, key=key) >= settings.LOGIN_MAX_FAILURES_PER_USER
    )


@router.post("/login", response_model=SessionInfo, response_model_exclude_none=True)
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    remember: bool = Form(False),
    session: Session = Depends(get_session),
):
    key = _name_key(form.username)
    if _login_locked(session, request, key):
        audit.record_now(session, request, None, "auth.login_blocked", "user", None, "", key=key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed sign-in attempts. Try again in a few minutes.",
            headers={"Retry-After": str(settings.LOGIN_WINDOW_MINUTES * 60)},
        )

    user = session.exec(select(User).where(User.username == form.username)).first()

    # Always run password verification to prevent username/status enumeration via timing
    # (an account that has no password yet is checked against the dummy too: no timing difference)
    target_hash = user.password_hash if user and user.password_hash else _DUMMY_HASH
    password_valid = verify_password(form.password, target_hash)

    if not user or user.disabled or not password_valid:
        reason = "unknown user" if not user else "disabled" if user.disabled else "bad password"
        audit.record_now(session, request, None, "auth.login_failed", "user", user.id if user else None, user.username if user else "", reason=reason, key=key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(form.password)
        session.add(user)
        session.commit()
        session.refresh(user)

    # The session id changes at every sign-in: one the browser presented is ended, not reused.
    sessions.revoke(session, request.cookies.get(sessions.cookie_name()))
    audit.record_now(session, request, user, "auth.login", "user", user.id, user.username, key=key)
    return start_session(session, request, response, user, remember=remember)


@router.post("/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    """Ends the browser session on the server and expires the cookie. Needs no valid session: whoever holds a stale
    cookie can still get rid of it."""
    raw = request.cookies.get(sessions.cookie_name())
    found = sessions.lookup(session, raw)
    if found is not None:
        audit.record(session, request, found[1], "auth.logout", "user", found[1].id, found[1].username)
    sessions.revoke(session, raw)
    session.commit()
    sessions.clear_cookie(response)
    return {"ok": True}


@router.get("/me", response_model=SessionInfo, response_model_exclude_none=True)
def me(user: User = Depends(get_current_user)):
    """Who is signed in: all the frontend gets to know (it cannot read the cookie). A guest share session answers
    with role "guest"."""
    return SessionInfo(username=user.username, role=user.role)


# ----------------------------------------------------------------------------- setting a password from a link
# An admin creates an account without a password, or asks for a password reset; either gives them a one-time
# link (see routers/users.py) to hand to the user, who opens it and picks the password. The token is
# in the POST body (not in the URL) so that it does not end up in access logs.

def _link_user(session: Session, token: str):
    """The (link, user) the token belongs to, or an HTTP error that tells the user what to do."""
    row = session.exec(select(UserToken).where(UserToken.token_hash == hash_token(token.strip()))).first()
    user = session.get(User, row.user_id) if row else None
    if not row or not user:
        raise HTTPException(status_code=404, detail="This link is not valid. Ask an administrator for a new one.")
    if row.used_at is not None:
        raise HTTPException(status_code=410, detail="This link has already been used. Ask an administrator for a new one if you need it.")
    if aware(row.expires_at) <= get_utc_now():
        raise HTTPException(status_code=410, detail="This link has expired. Ask an administrator for a new one.")
    if user.disabled:
        raise HTTPException(status_code=403, detail="This account is disabled.")
    return row, user


@router.post("/set-password/check", response_model=SetPasswordInfo)
def check_password_link(payload: SetPasswordCheck, session: Session = Depends(get_session)):
    """Whose password the link sets (the page shows it above the form), without using up the link."""
    row, user = _link_user(session, payload.token)
    return SetPasswordInfo(username=user.username, purpose=row.purpose, expires_at=aware(row.expires_at))


@router.post("/set-password", response_model=SessionInfo, response_model_exclude_none=True)
def set_password_from_link(payload: SetPassword, request: Request, response: Response, session: Session = Depends(get_session)):
    """Sets the password with a one-time link, uses the link up and signs the user in. Sessions that were
    open before (a reset) are ended."""
    row, user = _link_user(session, payload.token)
    try:
        validate_new_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # The link is used up first, by a conditional update: of two requests with the same link only one succeeds.
    claimed = session.exec(
        update(UserToken).where(UserToken.id == row.id, col(UserToken.used_at).is_(None)).values(used_at=get_utc_now())
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=410, detail="This link has already been used. Ask an administrator for a new one if you need it.")
    user.password_hash = hash_password(payload.password)
    session.add(user)
    session.exec(delete(UserToken).where(UserToken.user_id == user.id, UserToken.id != row.id))  # any other link of the user
    sessions.revoke_user_sessions(session, user.id)
    audit.record(session, request, user, "user.password_set", "user", user.id, user.username, via="link", purpose=row.purpose)
    session.commit()
    session.refresh(user)
    return start_session(session, request, response, user)
