"""Answering a download of a file from the data folder (an attachment, the result of a processing task).

Behind nginx the app only authorizes the request and names the file (X-Accel-Redirect): nginx sends it, with
Range support and without holding a worker of the app for the length of a multi-gigabyte transfer. Without it
(VG_SHARD_ACCEL_REDIRECT off, or a bare uvicorn) the app sends the file itself. Either way the file is offered as a
download of unknown type (never shown by the browser, so an uploaded HTML or SVG file cannot run as part of the app).
"""
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from .config import settings

_ACCEL_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def content_disposition(filename: str) -> str:
    """A Content-Disposition header value for a download under `filename` (any characters)."""
    clean = (filename or "download").replace("\r", " ").replace("\n", " ").replace("\\", "_").replace('"', "'")
    ascii_name = clean.encode("ascii", "replace").decode("ascii").replace("?", "_")
    if ascii_name == clean:
        return f'attachment; filename="{ascii_name}"'
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(clean, safe='')}"


def file_download(path: Path, accel_parts: tuple, filename: str) -> Response:
    """Sends `path` as a download named `filename`. `accel_parts` is where the file is below the volumes folder
    (slug, folder, file name): the path nginx serves it from."""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    headers = {
        "Content-Disposition": content_disposition(filename),
        "Content-Type": "application/octet-stream",
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if settings.SHARD_ACCEL_REDIRECT and all(_ACCEL_PART_RE.fullmatch(p) for p in accel_parts):
        prefix = settings.SHARD_ACCEL_PREFIX.rstrip("/")
        return Response(status_code=200, headers={**headers, "X-Accel-Redirect": f"{prefix}/{'/'.join(accel_parts)}"})
    return FileResponse(path, media_type="application/octet-stream", headers=headers)
