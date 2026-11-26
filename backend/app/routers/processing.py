"""Processing tasks (see processing.py): asking for one, watching it, downloading its result.

/api/processing/tasks                          the tasks the signed-in user may see
/api/processing/tasks/{id}                     one task (the Processing page polls it while it runs)
/api/processing/tasks/{id}/download-url        a short-lived link to the result
/api/processing/tiff-export/{volume_id}        GET: the levels to choose from; POST: ask for a TIFF stack of one

Who may do what: anybody signed in who can see the volume may ask for a TIFF stack, see the tasks of the volumes they can
see and download the results; editors and administrators see every task and may delete (stop) any. A guest of a share
link has no Processing page and cannot ask for tasks.
"""
import json
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlmodel import Session, select

from .. import audit, jobs, processing
from ..config import settings
from ..database import get_session
from ..deps import require_roles
from ..helpers import tiff_export
from ..helpers.slices import SliceError
from ..models import AssetStatus, ProcessingTask, Role, TaskStatus, User, Volume, VolumeAccess
from ..schemas import FileAccessUrl, TaskRead, TiffExportOptions, TiffExportRequest, TiffLevelOption
from ..security import create_file_token
from ..downloads import file_download
from ..utils import get_utc_now
from .volumes import _can_see_volume, _check_file_token, _volume_dir

router = APIRouter(prefix="/api/processing", tags=["processing"])

account = require_roles(Role.admin, Role.editor, Role.reader)  # not a guest of a share link
_EDITORS = (Role.admin, Role.editor)


def _iso(dt) -> Optional[str]:
    dt = processing.aware(dt)
    return dt.isoformat() if dt else None


def _read(task: ProcessingTask, user: User, names: Dict[int, str]) -> TaskRead:
    return TaskRead(
        id=task.id, kind=task.kind, volume_id=task.volume_id, volume_slug=task.volume_slug,
        volume_title=task.volume_title, params=processing.params_of(task), status=task.status,
        status_log=[line for line in (task.status_log or "").split("\n") if line],
        progress_done=task.progress_done, progress_total=task.progress_total,
        result_bytes=task.result_bytes, download_name=task.download_name,
        created_by_name=names.get(task.created_by) if user.role in _EDITORS else None,
        created_at=_iso(task.created_at), started_at=_iso(task.started_at), finished_at=_iso(task.finished_at),
        expires_at=_iso(task.expires_at),
    )


def _names(session: Session, tasks: List[ProcessingTask]) -> Dict[int, str]:
    ids = {t.created_by for t in tasks if t.created_by}
    if not ids:
        return {}
    return {u.id: u.username for u in session.exec(select(User).where(User.id.in_(ids))).all()}


def _visible(session: Session, user: User, task: ProcessingTask) -> bool:
    if user.role in _EDITORS:
        return True
    volume = session.get(Volume, task.volume_id)
    return volume is not None and _can_see_volume(user, volume, session)


def _get_task(session: Session, user: User, task_id: int) -> ProcessingTask:
    task = session.get(ProcessingTask, task_id)
    if task is None or not _visible(session, user, task):
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/tasks", response_model=List[TaskRead])
def list_tasks(session: Session = Depends(get_session), user: User = Depends(account)):
    processing.cleanup_expired()
    tasks = session.exec(select(ProcessingTask).order_by(ProcessingTask.id.desc())).all()
    if user.role not in _EDITORS:
        granted = {g.volume_id for g in session.exec(select(VolumeAccess).where(VolumeAccess.user_id == user.id)).all()}
        tasks = [t for t in tasks if t.volume_id in granted]
    names = _names(session, tasks)
    return [_read(t, user, names) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: int, session: Session = Depends(get_session), user: User = Depends(account)):
    task = _get_task(session, user, task_id)
    return _read(task, user, _names(session, [task]))


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: int, request: Request, session: Session = Depends(get_session),
    editor: User = Depends(require_roles(*_EDITORS)),
):
    """Deletes a task and its result. A task that is running stops (the worker notices within a few seconds)."""
    task = session.get(ProcessingTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    processing.remove_result(task)
    audit.record(session, request, editor, "task.delete", "volume", task.volume_id, task.volume_slug,
                 kind=task.kind, task_id=task.id)
    session.delete(task)
    session.commit()
    return {"ok": True}


@router.get("/tasks/{task_id}/download-url", response_model=FileAccessUrl)
def task_download_url(task_id: int, session: Session = Depends(get_session), user: User = Depends(account)):
    task = _get_task(session, user, task_id)
    processing.cleanup_expired()
    session.refresh(task)
    path = processing.result_path(task)
    if task.status == TaskStatus.expired:
        raise HTTPException(status_code=410, detail="This result has been deleted; request it again")
    if task.status != TaskStatus.ready or path is None or not path.is_file():
        raise HTTPException(status_code=409, detail="This result is not ready")
    token = create_file_token(subject=user.username, user=user, asset_id=task.volume_id, kind="task-result")
    return FileAccessUrl(
        url=f"/api/processing/tasks/{task.id}/file/{token}", expires_in_minutes=settings.FILE_TOKEN_EXPIRE_MINUTES,
    )


@router.get("/tasks/{task_id}/file/{token}")
def task_file(task_id: int, token: str, session: Session = Depends(get_session)):
    task = session.get(ProcessingTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not found")
    _check_file_token(token, task.volume_id, "task-result")
    path = processing.result_path(task)
    if task.status != TaskStatus.ready or path is None:
        raise HTTPException(status_code=404, detail="Not found")
    return file_download(path, (task.volume_slug, processing.EXPORTS_DIR, path.name), task.download_name or path.name)


# ------------------------------------------------------------------------------------------------ TIFF stacks

def _ready_volume(session: Session, user: User, volume_id: int) -> Volume:
    volume = session.get(Volume, volume_id)
    if not volume or not _can_see_volume(user, volume, session):
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.status != AssetStatus.ready:
        raise HTTPException(status_code=409, detail="The volume is not ready yet")
    return volume


def _volume_meta(volume: Volume) -> dict:
    try:
        return tiff_export.read_meta(_volume_dir(volume.slug))
    except SliceError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _too_big(nbytes: int) -> Optional[str]:
    limit = settings.EXPORT_MAX_GB
    if limit > 0 and nbytes > limit * 1024 ** 3:
        return f"Over the limit of {limit:g} GB for one TIFF stack"
    return None


def _tasks_of(session: Session, volume_id: int) -> List[ProcessingTask]:
    return session.exec(select(ProcessingTask).where(
        ProcessingTask.kind == processing.KIND_TIFF, ProcessingTask.volume_id == volume_id,
        ProcessingTask.status.in_([TaskStatus.queued, TaskStatus.running, TaskStatus.ready]),
    )).all()


@router.get("/tiff-export/{volume_id}", response_model=TiffExportOptions)
def tiff_export_options(volume_id: int, session: Session = Depends(get_session), user: User = Depends(account)):
    """The levels of the volume a TIFF stack can be made of, with their sizes: level 0 is the full resolution and
    each level after it halves every side."""
    volume = _ready_volume(session, user, volume_id)
    meta = _volume_meta(volume)
    by_level = {processing.params_of(t).get("level"): t for t in _tasks_of(session, volume_id)}
    levels = []
    for option in tiff_export.level_options(meta):
        task = by_level.get(option["level"])
        reason = _too_big(option["bytes"])
        levels.append(TiffLevelOption(
            **option, allowed=reason is None, reason=reason,
            task_id=task.id if task else None, task_status=task.status if task else None,
        ))
    return TiffExportOptions(
        volume_id=volume.id, title=volume.title, data_type=str(meta.get("format", "uint8")),
        unit=(str(meta["unit"]) if meta.get("unit") else None),
        retention_hours=settings.EXPORT_RETENTION_HOURS, levels=levels,
    )


@router.post("/tiff-export/{volume_id}", response_model=TaskRead)
def request_tiff_export(
    volume_id: int,
    body: TiffExportRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    user: User = Depends(account),
):
    """Asks for a TIFF stack of one level. When the same stack is already being made or waits for download, that task
    is returned instead of starting another one."""
    volume = _ready_volume(session, user, volume_id)
    meta = _volume_meta(volume)
    option = next((o for o in tiff_export.level_options(meta) if o["level"] == body.level), None)
    if option is None:
        raise HTTPException(status_code=400, detail=f"This volume has no level {body.level}")
    reason = _too_big(option["bytes"])
    if reason:
        raise HTTPException(status_code=413, detail=reason)

    processing.cleanup_expired()
    for task in _tasks_of(session, volume_id):
        if processing.params_of(task).get("level") == body.level:
            return _read(task, user, _names(session, [task]))

    limit = settings.TASK_MAX_ACTIVE_PER_USER
    if limit > 0 and user.role not in _EDITORS:
        active = session.exec(select(ProcessingTask).where(
            ProcessingTask.created_by == user.id, ProcessingTask.status.in_(processing.ACTIVE))).all()
        if len(active) >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"You already have {len(active)} tasks waiting or running. Wait until one is finished.",
            )

    task = ProcessingTask(
        kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug=volume.slug, volume_title=volume.title,
        params=json.dumps({"level": body.level}), created_by=user.id, progress_total=option["slices"],
    )
    processing.add_log(task, f"Queued: level {body.level}, {option['dimensions'][0]} x {option['dimensions'][1]} x {option['dimensions'][2]} voxels")
    session.add(task)
    session.flush()
    audit.record(session, request, user, "task.create", "volume", volume.id, volume.slug,
                 kind=task.kind, level=body.level, task_id=task.id)
    session.commit()
    session.refresh(task)
    jobs.dispatch(background_tasks, session, "task", task, processing.run_task, task.id)
    return _read(task, user, _names(session, [task]))
