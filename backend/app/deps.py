from datetime import timezone
from typing import Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select

from . import sessions
from .config import settings
from .database import engine, get_session
from .security import decode_token, password_fingerprint
from .models import User, Role, ShareLink, VolumeAccess, MeshAccess, PointCloudAccess
from .utils import get_utc_now

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _not_authenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _guest_for_share_token(payload: dict, session: Session) -> User:
    share_id = payload.get("sid")
    if not share_id:
        raise _not_authenticated()
    share = session.get(ShareLink, int(share_id))
    if not share or share.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share link revoked or invalid")
    exp = share.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp <= get_utc_now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Share link has expired")
    guest = User(
        id=-1,
        username=f"guest:{share.code}",
        password_hash="",
        role=Role.guest,
        disabled=False,
    )
    guest.__dict__["_share_scope"] = {
        "share_id": share.id,
        "code": share.code,
        "asset_type": share.asset_type,
        "asset_id": share.asset_id,
        "can_download": share.can_download,
        "min_lod": share.min_lod,
        "expires_at": exp,
    }
    return guest


def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Who is asking. In this order:

    1. The session cookie (a browser sign-in, see sessions.py). The role is read from the user's row, so a role change
       or a disabled account counts immediately.
    2. `Authorization: Bearer <token>`:
       - a share-link token (a guest, limited to what the link shows; always accepted, and never has a role that
         the editor/admin endpoints take);
       - a login access token, only while VG_ALLOW_BEARER_AUTH is on (API / CLI clients, see docs/auth.md).
    A valid session cookie wins over a bearer header: a real sign-in replaces a guest share session."""
    found = sessions.lookup(session, request.cookies.get(sessions.cookie_name()))
    if found is not None:
        row, user = found
        user.__dict__["_session_id"] = row.id  # (file tokens minted for this request remember the session)
        return user

    if not token:
        raise _not_authenticated()
    payload = decode_token(token)
    if not payload:
        raise _not_authenticated()
    kind = payload.get("type")
    if kind == "share":
        return _guest_for_share_token(payload, session)
    if kind != "access" or not settings.ALLOW_BEARER_AUTH:
        raise _not_authenticated()
    username = payload.get("sub")
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or user.disabled:
        raise _not_authenticated()
    # Tokens carry a fingerprint of the password they were issued for: after a password
    # change/reset the old ones stop working. (Tokens issued before this claim existed
    # have none and stay valid until they expire.)
    pwv = payload.get("pwv")
    if pwv is not None and pwv != password_fingerprint(user.password_hash):
        raise _not_authenticated()
    return user


# file-token kind -> (grant table, its asset column). A token's "vol" claim is the asset id.
_GRANT_BY_KIND = {
    "sharded": (VolumeAccess, VolumeAccess.volume_id),
    "slice": (VolumeAccess, VolumeAccess.volume_id),
    "volume-mesh-download": (VolumeAccess, VolumeAccess.volume_id),
    "volume-mesh-view": (VolumeAccess, VolumeAccess.volume_id),
    "volume-attachment": (VolumeAccess, VolumeAccess.volume_id),
    "task-result": (VolumeAccess, VolumeAccess.volume_id),
    "mesh": (MeshAccess, MeshAccess.mesh_id),
    "mesh-download": (MeshAccess, MeshAccess.mesh_id),
    "pc-octree": (PointCloudAccess, PointCloudAccess.pointcloud_id),
    "pc-download": (PointCloudAccess, PointCloudAccess.pointcloud_id),
}


def file_token_revoked(payload: dict) -> bool:
    """True when whatever a file token was issued for no longer holds: its share link was revoked or
    has expired, or its account was disabled or deleted, or the browser session it was minted in has ended, or (for a reader) the grant it relied on was removed. (Tokens without either claim, issued before
    this check existed, are never refused here.)"""
    shr, uid, ses = payload.get("shr"), payload.get("uid"), payload.get("ses")
    if shr is None and uid is None:
        return False
    with Session(engine) as session:
        if shr is not None:
            share = session.get(ShareLink, int(shr))
            if share is None or share.revoked:
                return True
            exp = share.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp <= get_utc_now()
        user = session.get(User, int(uid))
        if user is None or user.disabled:
            return True
        if ses is not None and not sessions.is_alive(session, int(ses)):
            return True  # the browser session that minted the token has ended (logout, password or role change, ...)
            # A reader's access comes from a per-asset grant: taking it away must stop the tokens they hold.
        if user.role in (Role.admin, Role.editor):
            return False
        kind, asset_id = payload.get("kind"), payload.get("vol")
        grant = _GRANT_BY_KIND.get(kind)
        if grant is None or asset_id is None:
            return False
        model, column = grant
        found = session.exec(
            select(model).where(model.user_id == user.id, column == int(asset_id))
        ).first()
        return found is None


def require_roles(*roles: Role):
    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {[r.value for r in roles]}",
            )
        return user
    return _checker


require_admin = require_roles(Role.admin)
require_editor = require_roles(Role.admin, Role.editor)
any_authenticated = get_current_user
