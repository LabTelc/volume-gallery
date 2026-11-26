"""Files attached to a volume (shown on its report page).

GET    /api/volumes/{id}/attachments                      the list (anyone who can see the volume)
POST   /api/volumes/{id}/attachments                      attach a file (editor): `file`, or `attachment_upload_id`
                                                          from PUT /api/uploads/attachment; one file per request
PATCH  /api/volumes/{id}/attachments/{aid}                rename it or change its description (editor)
DELETE /api/volumes/{id}/attachments/{aid}                remove it (editor)
GET    /api/volumes/{id}/attachments/{aid}/download-url   a short-lived link; the file itself is at
GET    /api/volumes/{id}/attachment-file/{token}/{aid}

The files are kept in <volumes>/<slug>/attachments/<id>__<name>: they go with the volume when it is deleted and
count in its disk usage. Downloads follow the volume's rules (like the mesh): whoever can see the volume may take
them, a share link only when it allows downloads. They are always sent as a download, never opened by the browser.
"""
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlmodel import Session, select

from .. import audit, workdir
from ..config import settings
from ..database import get_session
from ..deps import get_current_user, require_editor
from ..downloads import file_download
from ..models import AssetStatus, User, Volume, VolumeAttachment
from ..schemas import AttachmentRead, AttachmentUpdate, FileAccessUrl
from ..security import create_file_token
from .volumes import _can_see_volume, _check_file_token, _require_download, _volume_dir

router = APIRouter(prefix="/api/volumes", tags=["attachments"])

ATTACHMENTS_DIR = "attachments"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def attachments_dir(slug: str) -> Path:
    return _volume_dir(slug) / ATTACHMENTS_DIR


def _display_name(name: str) -> str:
    """The name of an attachment as shown and as saved by the browser: no folders, no control characters."""
    name = _CONTROL_RE.sub("", (name or "").replace("\\", "/").split("/")[-1]).strip()
    return name[-255:] or "file"


def visible_volume(session: Session, user: User, volume_id: int) -> Volume:
    volume = session.get(Volume, volume_id)
    if not volume or not _can_see_volume(user, volume, session):
        raise HTTPException(status_code=404, detail="Volume not found")
    return volume


def _read(att: VolumeAttachment, names: dict) -> AttachmentRead:
    created = att.created_at
    return AttachmentRead(
        id=att.id, filename=att.filename, size=att.size, content_type=att.content_type or "",
        description=att.description or "", created_at=created.isoformat() if created else "",
        created_by_name=names.get(att.created_by),
    )


def list_attachments(session: Session, volume_id: int) -> List[AttachmentRead]:
    rows = session.exec(select(VolumeAttachment).where(VolumeAttachment.volume_id == volume_id)
                        .order_by(VolumeAttachment.id)).all()
    ids = {r.created_by for r in rows if r.created_by}
    names = {u.id: u.username for u in session.exec(select(User).where(User.id.in_(ids))).all()} if ids else {}
    return [_read(r, names) for r in rows]


def _get(session: Session, volume_id: int, attachment_id: int) -> VolumeAttachment:
    att = session.get(VolumeAttachment, attachment_id)
    if att is None or att.volume_id != volume_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return att


@router.get("/{volume_id}/attachments", response_model=List[AttachmentRead])
def get_attachments(volume_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    visible_volume(session, user, volume_id)
    return list_attachments(session, volume_id)


@router.post("/{volume_id}/attachments", response_model=AttachmentRead)
def add_attachment(
    volume_id: int,
    request: Request,
    file: Optional[UploadFile] = File(None, description="The file"),
    attachment_upload_id: Optional[str] = Form(None, description="Instead of file: an id from PUT /api/uploads/attachment"),
    description: str = Form("", max_length=2000),
    session: Session = Depends(get_session),
    editor: User = Depends(require_editor),
):
    volume = session.get(Volume, volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status != AssetStatus.ready:
        raise HTTPException(status_code=409, detail="The volume is not ready yet")
    if (file is None) == (not attachment_upload_id):
        raise HTTPException(status_code=400, detail="Provide exactly one of file or attachment_upload_id")
    staged = workdir.claim_upload(attachment_upload_id, "attachment", editor.id) if attachment_upload_id else None
    shown = _display_name(staged.filename if staged else file.filename)

    att = VolumeAttachment(
        volume_id=volume_id, filename=shown, stored_name="", description=description.strip(),
        content_type=(getattr(file, "content_type", None) or "")[:200] if file else "", created_by=editor.id,
    )
    session.add(att)
    session.flush()  # the id is part of the name on disk
    att.stored_name = f"{att.id}__{workdir.safe_filename(shown)}"
    dest = attachments_dir(volume.slug) / att.stored_name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        workdir.accept_upload(file, staged, dest, save_now=True)
        att.size = dest.stat().st_size
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not save the file: {exc}")
    audit.record(session, request, editor, "attachment.add", "volume", volume.id, volume.slug,
                 filename=shown, size=att.size)
    session.add(att)
    session.commit()
    session.refresh(att)
    return _read(att, {editor.id: editor.username})


@router.patch("/{volume_id}/attachments/{attachment_id}", response_model=AttachmentRead)
def update_attachment(
    volume_id: int, attachment_id: int, body: AttachmentUpdate, request: Request,
    session: Session = Depends(get_session), editor: User = Depends(require_editor),
):
    att = _get(session, volume_id, attachment_id)
    if body.filename is not None:
        att.filename = _display_name(body.filename)  # only the name shown: the file on disk keeps its name
    if body.description is not None:
        att.description = body.description.strip()
    session.add(att)
    session.commit()
    session.refresh(att)
    return _read(att, {})


@router.delete("/{volume_id}/attachments/{attachment_id}")
def delete_attachment(
    volume_id: int, attachment_id: int, request: Request,
    session: Session = Depends(get_session), editor: User = Depends(require_editor),
):
    att = _get(session, volume_id, attachment_id)
    volume = session.get(Volume, volume_id)
    if volume is not None and att.stored_name:
        (attachments_dir(volume.slug) / att.stored_name).unlink(missing_ok=True)
    audit.record(session, request, editor, "attachment.delete", "volume", volume_id,
                 volume.slug if volume else "", filename=att.filename)
    session.delete(att)
    session.commit()
    return {"ok": True}


@router.get("/{volume_id}/attachments/{attachment_id}/download-url", response_model=FileAccessUrl)
def attachment_download_url(
    volume_id: int, attachment_id: int,
    session: Session = Depends(get_session), user: User = Depends(get_current_user),
):
    visible_volume(session, user, volume_id)
    _get(session, volume_id, attachment_id)
    _require_download(user)
    token = create_file_token(subject=user.username, user=user, asset_id=volume_id, kind="volume-attachment")
    return FileAccessUrl(
        url=f"/api/volumes/{volume_id}/attachment-file/{token}/{attachment_id}",
        expires_in_minutes=settings.FILE_TOKEN_EXPIRE_MINUTES,
    )


@router.get("/{volume_id}/attachment-file/{token}/{attachment_id}")
def attachment_file(volume_id: int, token: str, attachment_id: int, session: Session = Depends(get_session)):
    _check_file_token(token, volume_id, "volume-attachment")
    att = _get(session, volume_id, attachment_id)
    volume = session.get(Volume, volume_id)
    if volume is None or not att.stored_name:
        raise HTTPException(status_code=404, detail="Not found")
    return file_download(attachments_dir(volume.slug) / att.stored_name,
                         (volume.slug, ATTACHMENTS_DIR, att.stored_name), att.filename)
