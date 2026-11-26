"""Where a volume's report text lives: the file <volumes>/<slug>/report/report.md, next to the report's images.

The database keeps only who saved the text last and when (VolumeReport); the text itself is this file, so it can be
read, backed up and diffed like any other file. The file is replaced atomically, so a reader never sees half a text,
and it goes away with the volume's folder.

The images in the same folder are named <id>__<name> (see routers/reports.py), so none of them can be called report.md.
"""
import os
import secrets
from pathlib import Path

from .config import settings

REPORT_FILE = "report.md"


def report_path(slug: str) -> Path:
    return settings.VOLUMES_DIR / slug / REPORT_FILE


def read_text(slug: str) -> str:
    """The saved text; "" when none has been saved. Invalid UTF-8 (a hand-edited file) is shown with U+FFFD, not an error."""
    try:
        return report_path(slug).read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def write_text(slug: str, text: str) -> None:
    """Replaces the text (written as given: no newline translation). Raises OSError if it cannot be saved, and
    UnicodeEncodeError for text that is not valid Unicode (a lone surrogate)."""
    data = text.encode("utf-8")
    path = report_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{REPORT_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
