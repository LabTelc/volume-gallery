"""The report page of a volume: a markdown text (with images and formulas) and the volume's attachments.

GET    /api/volumes/{id}/report               the text and the attachments (anyone who can see the volume)
PUT    /api/volumes/{id}/report               save the text (editor)
GET    /api/volumes/{id}/report/images        the images uploaded for the text (editor)
POST   /api/volumes/{id}/report/images        upload an image (editor); the text refers to it as ![caption](img:<id>)
GET    /api/volumes/{id}/report/images/{iid}  the image (anyone who can see the volume; fetched with the
                                              Authorization header, since an <img> cannot send it)
DELETE /api/volumes/{id}/report/images/{iid}  delete it (editor)

The text is stored as written and turned into HTML by the browser, which does not accept raw HTML in it (frontend/js/
report-markdown.js), so a report can show formatting, images and formulas but cannot run anything. Images are checked by
looking at the file itself: only PNG, JPEG, GIF and WebP are taken (not SVG, which can carry scripts), whatever the
upload says it is. They are kept in <volumes>/<slug>/report/<id>__<name>.
"""
import io
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from PIL import Image
from sqlmodel import Session, select

from .. import audit, workdir
from ..config import settings
from ..database import get_session
from ..deps import get_current_user, require_editor
from ..models import AssetStatus, ReportImage, User, Volume, VolumeReport
from ..schemas import ReportImageRead, ReportRead, ReportWrite
from ..utils import get_utc_now
from .attachments import _display_name, list_attachments, visible_volume
from .volumes import _volume_dir

router = APIRouter(prefix="/api/volumes", tags=["reports"])

REPORT_DIR = "report"
_IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


@router.get("/{volume_id}/report", response_model=ReportRead)
def get_report(volume_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    visible_volume(session, user, volume_id)
    report = session.exec(select(VolumeReport).where(VolumeReport.volume_id == volume_id)).first()
    scope = getattr(user, "_share_scope", None)
    editor = session.get(User, report.updated_by) if report and report.updated_by else None
    return ReportRead(
        volume_id=volume_id,
        markdown=report.markdown if report else "",
        updated_at=_iso(report.updated_at) if report else None,
        updated_by_name=editor.username if editor else None,
        attachments=list_attachments(session, volume_id),
        can_download=scope is None or bool(scope.get("can_download")),
    )


@router.put("/{volume_id}/report", response_model=ReportRead)
def put_report(
    volume_id: int, body: ReportWrite, request: Request,
    session: Session = Depends(get_session), editor: User = Depends(require_editor),
):
    volume = session.get(Volume, volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    report = session.exec(select(VolumeReport).where(VolumeReport.volume_id == volume_id)).first()
    if report is None:
        report = VolumeReport(volume_id=volume_id)
    report.markdown = body.markdown
    report.updated_by = editor.id
    report.updated_at = get_utc_now()
    session.add(report)
    audit.record(session, request, editor, "report.update", "volume", volume.id, volume.slug, characters=len(body.markdown))
    session.commit()
    return get_report(volume_id, session, editor)


# ----------------------------------------------------------------------------------------------- images

def _image_read(image: ReportImage) -> ReportImageRead:
    return ReportImageRead(id=image.id, filename=image.filename, content_type=image.content_type,
                           size=image.size, created_at=_iso(image.created_at))


@router.get("/{volume_id}/report/images", response_model=List[ReportImageRead])
def list_images(volume_id: int, session: Session = Depends(get_session), _editor: User = Depends(require_editor)):
    rows = session.exec(select(ReportImage).where(ReportImage.volume_id == volume_id).order_by(ReportImage.id)).all()
    return [_image_read(r) for r in rows]


@router.post("/{volume_id}/report/images", response_model=ReportImageRead)
def upload_image(
    volume_id: int, request: Request, file: UploadFile = File(...),
    session: Session = Depends(get_session), editor: User = Depends(require_editor),
):
    volume = session.get(Volume, volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status != AssetStatus.ready:
        raise HTTPException(status_code=409, detail="The volume is not ready yet")
    limit = settings.REPORT_IMAGE_MAX_MB * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"The image is larger than {settings.REPORT_IMAGE_MAX_MB} MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            kind = _IMAGE_TYPES.get(img.format or "")
            img.verify()
    except Exception:  # noqa: BLE001 - whatever is wrong with it, it is not an image we take
        kind = None
    if kind is None:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, GIF and WebP images can be added")

    name = _display_name(file.filename)
    image = ReportImage(volume_id=volume_id, filename=name, stored_name="", content_type=kind,
                        size=len(data), created_by=editor.id)
    session.add(image)
    session.flush()
    image.stored_name = f"{image.id}__{workdir.safe_filename(name)}"
    dest = _volume_dir(volume.slug) / REPORT_DIR / image.stored_name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Could not save the image: {exc}")
    audit.record(session, request, editor, "report.image.add", "volume", volume.id, volume.slug, filename=name)
    session.add(image)
    session.commit()
    session.refresh(image)
    return _image_read(image)


def _image(session: Session, volume_id: int, image_id: int) -> ReportImage:
    image = session.get(ReportImage, image_id)
    if image is None or image.volume_id != volume_id:
        raise HTTPException(status_code=404, detail="Image not found")
    return image


@router.get("/{volume_id}/report/images/{image_id}")
def get_image(
    volume_id: int, image_id: int,
    session: Session = Depends(get_session), user: User = Depends(get_current_user),
):
    volume = visible_volume(session, user, volume_id)
    image = _image(session, volume_id, image_id)
    path: Path = _volume_dir(volume.slug) / REPORT_DIR / image.stored_name
    try:
        data = path.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(content=data, media_type=image.content_type, headers={
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.delete("/{volume_id}/report/images/{image_id}")
def delete_image(
    volume_id: int, image_id: int, request: Request,
    session: Session = Depends(get_session), editor: User = Depends(require_editor),
):
    image = _image(session, volume_id, image_id)
    volume = session.get(Volume, volume_id)
    if volume is not None:
        (_volume_dir(volume.slug) / REPORT_DIR / image.stored_name).unlink(missing_ok=True)
    session.delete(image)
    session.commit()
    return {"ok": True}
