"""Processing tasks: work done on a volume in the background whose result is a file to download.

A person asks for a task (today: a TIFF stack of one resolution level, see routers/processing.py); a row in
models.ProcessingTask records it and the work is queued like the conversions of uploads (jobs.dispatch: on the
Redis worker, or as a background task of the web process when there is no queue). The row follows the work
(queued -> running -> ready | failed), carries its progress for the Processing page, and says when the result will
be deleted: a result is kept VG_EXPORT_RETENTION_HOURS after it was made, then its file is removed and the row
becomes "expired" (rows of expired and failed tasks are forgotten after FORGET_AFTER).

    <volumes>/<slug>/exports/t<task id>.zip      the result of a task, in the volume's folder: it is deleted
                                                 with the volume, counted in its disk usage, and served by
                                                 nginx from the same place as the volume's other files

A new kind of task is added by writing a function `runner(session, task, progress)` that does the work, fills in
`task.result_name`, `task.result_bytes` and `task.download_name` and calls `progress(done, total)` now and then
(it raises TaskCancelled when somebody deleted the task: let it pass; a half-written file is the runner's to
remove, remove_result does it), by registering it in RUNNERS and by adding
an endpoint that creates the task (routers/processing.py). Nothing else needs to change.
"""
import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .helpers import tiff_export
from .helpers.slices import SliceError
from .models import ProcessingTask, TaskStatus
from .utils import _now, get_utc_now

KIND_TIFF = "tiff_export"
EXPORTS_DIR = "exports"
ACTIVE = (TaskStatus.queued, TaskStatus.running)
FORGET_AFTER = timedelta(days=30)   # how long the rows of expired and failed tasks stay in the list
_PROGRESS_EVERY = 1.0               # seconds between two writes of a task's progress to the database
_FREE_SPACE_MARGIN = 64 * 1024 ** 2  # bytes that must stay free beyond what a result needs


class TaskCancelled(Exception):
    """The task was deleted while it ran."""


class TaskError(Exception):
    """A task that cannot be done; the message is shown to the person."""


def exports_dir(slug: str) -> Path:
    return settings.VOLUMES_DIR / slug / EXPORTS_DIR


def result_path(task: ProcessingTask) -> Optional[Path]:
    return exports_dir(task.volume_slug) / task.result_name if task.result_name else None


def params_of(task: ProcessingTask) -> dict:
    try:
        value = json.loads(task.params or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands datetimes back without their time zone: they are all UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def add_log(task: ProcessingTask, message: str) -> None:
    task.status_log = (task.status_log or "") + f"{_now()}  {message}\n"


def remove_result(task: ProcessingTask) -> None:
    """Deletes the task's result file and whatever a run of it left half-written (t<id>.zip is where the
    TIFF stack goes; a task that has not finished has no result_name yet)."""
    folder = exports_dir(task.volume_slug)
    names = {f"t{task.id}.zip"}
    if task.result_name:
        names.add(task.result_name)
    for name in names:
        (folder / name).unlink(missing_ok=True)
        (folder / f"{name}.partial").unlink(missing_ok=True)


# ----------------------------------------------------------------------------------------- the kinds of task

def _run_tiff_export(session: Session, task: ProcessingTask, progress: Callable[[int, int], None]) -> None:
    level = int(params_of(task).get("level", 0))
    vol_dir = settings.VOLUMES_DIR / task.volume_slug
    try:
        meta = tiff_export.read_meta(vol_dir)
        option = next((o for o in tiff_export.level_options(meta) if o["level"] == level), None)
    except SliceError as exc:
        raise TaskError(str(exc)) from exc
    if option is None:
        raise TaskError(f"This volume has no level {level}")
    target_dir = exports_dir(task.volume_slug)
    target_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target_dir).free
    if free < option["bytes"] + _FREE_SPACE_MARGIN:
        raise TaskError(
            f"Not enough free disk space: the stack needs about {fmt_bytes(option['bytes'])} and {fmt_bytes(free)} "
            "is free. Choose a coarser level, or ask the administrator to free space."
        )
    w, h, d = option["dimensions"]
    add_log(task, f"Writing level {level} ({w} x {h} x {d} voxels, about {fmt_bytes(option['bytes'])})")
    session.add(task)
    session.commit()
    out = target_dir / f"t{task.id}.zip"
    try:
        size = tiff_export.export_tiff_zip(
            vol_dir, level, out, name=f"{task.volume_slug}_lod{level}", title=task.volume_title,
            threads=settings.EXPORT_THREADS, progress=progress,
        )
    except SliceError as exc:
        raise TaskError(str(exc)) from exc
    task.result_name = out.name
    task.result_bytes = size
    task.download_name = tiff_export.download_name(task.volume_slug, level)


RUNNERS: Dict[str, Callable[[Session, ProcessingTask, Callable[[int, int], None]], None]] = {
    KIND_TIFF: _run_tiff_export,
}


# ---------------------------------------------------------------------------------------------- running a task

def run_task(task_id: int) -> None:
    """Does a task. This is what the job queue (or the web process, without a queue) runs; it opens its own
    database session. Whatever happens, the task ends up ready or failed, or gone (when it was deleted)."""
    with Session(engine) as session:
        task = session.get(ProcessingTask, task_id)
        if task is None or task.status != TaskStatus.queued:
            return
        runner = RUNNERS.get(task.kind)
        task.status = TaskStatus.running
        task.started_at = get_utc_now()
        add_log(task, "Started")
        session.add(task)
        session.commit()
        print(f"[Task {task_id}] {task.kind} of '{task.volume_slug}' started")

        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] < _PROGRESS_EVERY and done < total:
                return
            last[0] = now
            if session.exec(select(ProcessingTask.id).where(ProcessingTask.id == task_id)).first() is None:
                raise TaskCancelled()  # deleted: the runner stops (and removes what it wrote)
            task.progress_done, task.progress_total = done, total
            session.add(task)
            session.commit()

        try:
            if runner is None:
                raise TaskError(f"Unknown kind of task: {task.kind}")
            runner(session, task, progress)
            task.status = TaskStatus.ready
            task.finished_at = get_utc_now()
            task.expires_at = task.finished_at + timedelta(hours=settings.EXPORT_RETENTION_HOURS)
            task.progress_done = task.progress_total = max(task.progress_total, task.progress_done)
            add_log(task, f"Ready ({fmt_bytes(task.result_bytes or 0)})")
        except TaskCancelled:
            remove_result(task)
            print(f"[Task {task_id}] cancelled")
            return
        except Exception as exc:  # noqa: BLE001 - the message is the point: it is shown to the person
            session.rollback()
            if session.get(ProcessingTask, task_id) is None:  # deleted while it ran
                return
            task = session.get(ProcessingTask, task_id)
            remove_result(task)
            task.status = TaskStatus.failed
            task.finished_at = get_utc_now()
            add_log(task, f"Failed: {exc}" if isinstance(exc, TaskError) else f"Failed: {type(exc).__name__}: {exc}")
            print(f"[Task {task_id}] failed: {exc!r}")
        leftover = result_path(task)
        try:
            session.add(task)
            session.commit()
        except Exception:  # noqa: BLE001 - the row was deleted between the last check and now
            session.rollback()
            if leftover is not None:
                leftover.unlink(missing_ok=True)


# ----------------------------------------------------------------------------------------------- housekeeping

def cleanup_expired(now: Optional[datetime] = None) -> int:
    """Deletes the results that have outlived their retention (the task becomes "expired"), forgets old rows of
    expired and failed tasks and removes files in the exports folders that no task owns (left by a crash).
    Returns how many results were deleted."""
    now = now or get_utc_now()
    removed = 0
    with Session(engine) as session:
        for task in session.exec(select(ProcessingTask).where(ProcessingTask.status == TaskStatus.ready)).all():
            expires = aware(task.expires_at)
            if expires is not None and expires <= now:
                remove_result(task)
                task.status = TaskStatus.expired
                task.result_name = None
                add_log(task, "Result deleted (retention time over)")
                session.add(task)
                removed += 1
        for task in session.exec(select(ProcessingTask).where(
                ProcessingTask.status.in_([TaskStatus.expired, TaskStatus.failed]))).all():
            ended = aware(task.finished_at) or aware(task.created_at)
            if ended is not None and now - ended > FORGET_AFTER:
                remove_result(task)
                session.delete(task)
        session.commit()
        owned = {(t.volume_slug, t.result_name) for t in session.exec(select(ProcessingTask)).all() if t.result_name}
    sweep_orphans(owned)
    return removed


def sweep_orphans(owned: Iterable[tuple]) -> None:
    """Deletes what is in a volume's exports folder but belongs to no task. Partial files of a running task are
    left alone (they are only this old when a crash left them: the folder is checked at start-up too)."""
    owned = set(owned)
    base = settings.VOLUMES_DIR
    if not base.is_dir():
        return
    cutoff = time.time() - 3600
    for volume in base.iterdir():
        folder = volume / EXPORTS_DIR
        if not folder.is_dir():
            continue
        for child in folder.iterdir():
            try:
                if (volume.name, child.name) in owned or child.stat().st_mtime > cutoff:
                    continue
                child.unlink()
            except OSError:
                pass


def recover_interrupted(live_ids: Optional[set] = None) -> None:
    """At start-up: a task that is queued or running while nothing works on it (the server was restarted; with a
    queue: its job is gone) failed. `live_ids` are the tasks whose job is still queued or running."""
    live_ids = live_ids or set()
    with Session(engine) as session:
        for task in session.exec(select(ProcessingTask).where(ProcessingTask.status.in_(ACTIVE))).all():
            if task.id in live_ids:
                continue
            remove_result(task)
            task.status = TaskStatus.failed
            task.finished_at = get_utc_now()
            add_log(task, "Failed: processing was interrupted by a server restart. Request it again.")
            session.add(task)
            print(f"[volume-gallery] Marked interrupted task {task.id} ({task.kind}, '{task.volume_slug}') as failed.")
        session.commit()


_CLEANUP_EVERY = 600.0  # seconds


def start_cleanup_thread() -> threading.Thread:
    """A background thread of the web process that deletes expired results every few minutes."""
    def loop() -> None:
        while True:
            time.sleep(_CLEANUP_EVERY)
            try:
                cleanup_expired()
            except Exception as exc:  # noqa: BLE001 - housekeeping must never take the server down
                print(f"[volume-gallery] Cleaning up processing results failed: {exc!r}")

    thread = threading.Thread(target=loop, name="task-cleanup", daemon=True)
    thread.start()
    return thread
