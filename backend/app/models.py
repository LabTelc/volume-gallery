import enum
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel, DateTime

from .utils import get_utc_now


class Role(str, enum.Enum):
    admin = "admin"
    editor = "editor"
    reader = "reader"
    guest = "guest"


class AssetStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: Role = Field(default=Role.reader)
    created_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )
    disabled: bool = Field(default=False)

    @property
    def pending(self) -> bool:
        """An account an admin created that its user has not set a password for yet (it cannot sign in)."""
        return not self.password_hash


class UserToken(SQLModel, table=True):
    """A one-time link for setting a password (see invites.py). Only the hash of the token is kept."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    purpose: str  # invites.PURPOSE_SETUP | PURPOSE_RESET
    token_hash: str = Field(index=True, unique=True)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    used_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class AuditEvent(SQLModel, table=True):
    """One line of the audit log (see audit.py): who did what to what, when, from where. Append-only. Names are
    copied in and there are no foreign keys, so a line stays readable after the user or the item it mentions is
    deleted."""
    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True), index=True)
    actor_id: Optional[int] = Field(default=None, index=True)
    actor_name: str = ""          # "" when nobody was signed in (a failed login)
    action: str = Field(index=True)   # "user.create", "volume.delete", ... (audit.py lists them)
    target_type: str = ""         # "user", "volume", "mesh", "pointcloud", "share"
    target_id: Optional[int] = None
    target_name: str = ""
    detail: str = ""              # JSON object with what else is worth knowing (old and new role, ...)
    ip: str = ""


class AssetBase(SQLModel):
    slug: str = Field(index=True, unique=True)
    title: str
    description: str = Field(default="")
    thumbnail_path: Optional[str] = Field(default=None)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )
    status: AssetStatus = Field(default=AssetStatus.ready)
    status_log: str = Field(default="")
    # Gallery organisation: the folder the asset is filed in (None = top level) and its tags
    # (comma-separated; see organize.clean_tags).
    folder_id: Optional[int] = Field(default=None, foreign_key="folder.id", index=True)
    tags: str = Field(default="")


class Folder(SQLModel, table=True):
    """A gallery folder. Folders nest (parent_id) and hold assets of any kind."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    parent_id: Optional[int] = Field(default=None, foreign_key="folder.id", index=True)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )


class AssetAccessBase(SQLModel):
    """Shared fields for asset access control."""
    user_id: int = Field(foreign_key="user.id", index=True)
    granted_by: Optional[int] = Field(default=None, foreign_key="user.id")
    granted_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )


class Volume(AssetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    has_mesh: bool = Field(default=False)
    num_lod_levels: Optional[int] = Field(default=None)


class VolumeAccess(AssetAccessBase, table=True):
    """Grants a reader visibility into a volume."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", index=True)


class AnimationPath(SQLModel, table=True):
    """A saved way of showing a volume on screen (camera path, turntable or slice sweep), visible to
    everybody who can see the volume. `spec` is the validated PlaybackSpec as JSON text."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", index=True)
    name: str
    spec: str
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))


class ViewBookmark(SQLModel, table=True):
    """A saved view of a volume (camera, render settings, slice position), visible to everybody who can see
    the volume. `spec` is the validated BookmarkSpec as JSON text."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", index=True)
    name: str
    spec: str
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))


class Mesh(AssetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    file_extension: Optional[str] = Field(default="stl")  # file extension, e.g. "stl" or "ply"
    volume_id: Optional[int] = Field(default=None, foreign_key="volume.id", index=True)


class MeshAccess(AssetAccessBase, table=True):
    """Grants a reader visibility into a mesh."""
    id: Optional[int] = Field(default=None, primary_key=True)
    mesh_id: int = Field(foreign_key="mesh.id", index=True)
    can_download: bool = Field(default=False)


class PointCloud(AssetBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    has_mesh: bool = Field(default=False)


class PointCloudAccess(AssetAccessBase, table=True):
    """Grants a reader visibility into a point cloud."""
    id: Optional[int] = Field(default=None, primary_key=True)
    pointcloud_id: int = Field(foreign_key="pointcloud.id", index=True)


class ShareLink(SQLModel, table=True):
    """Time-limited named/shortened share link granting guest access to one asset."""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    asset_type: str = Field(index=True)  # "volume" | "mesh" | "pointcloud"
    asset_id: int = Field(index=True)
    view_query: str = Field(default="")
    can_download: bool = Field(default=False)
    min_lod: Optional[int] = Field(default=None)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True)
    )
    expires_at: datetime = Field(
        sa_type=DateTime(timezone=True),
        index=True
    )
    revoked: bool = Field(default=False)
    views_count: int = Field(default=0)


class ShareView(SQLModel, table=True):
    """One opening of a share link (/s/<code>): the log behind the statistics on the admin's Shares page.
    Deleted with its link; old rows are pruned (see routers/shares.py)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    share_id: int = Field(index=True)
    viewed_at: datetime = Field(
        default_factory=get_utc_now,
        sa_type=DateTime(timezone=True),
        index=True,
    )


# ---------- dataset report page: attachments and images ----------

class VolumeAttachment(SQLModel, table=True):
    """A file an editor attached to a volume (a PDF report, raw data, a calibration file, ...). Everybody who can
    see the volume sees the list; downloading follows the same rules as downloading a mesh. The file lives in
    <volumes>/<slug>/attachments/<stored_name>."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", index=True)
    filename: str                      # the name shown, and the name the browser saves it under
    stored_name: str                   # the file's name on disk (<id>__<safe filename>)
    size: int = 0
    content_type: str = ""             # as the browser reported it; informational only, downloads are always octet-stream
    description: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))


class VolumeReport(SQLModel, table=True):
    """Who saved a volume's report text last, and when (one row per volume, made when the first text is saved). The
    markdown itself is the file <volumes>/<slug>/report/report.md (see report_store.py), not a column."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", unique=True, index=True)
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))


class ReportImage(SQLModel, table=True):
    """An image used in a report's text (written as ![caption](img:<id>)). Lives in <volumes>/<slug>/report/<stored_name>."""
    id: Optional[int] = Field(default=None, primary_key=True)
    volume_id: int = Field(foreign_key="volume.id", index=True)
    filename: str
    stored_name: str
    content_type: str                  # detected from the file itself, never taken from the upload
    size: int = 0
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))


# ---------- processing tasks ----------

class TaskStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    ready = "ready"
    failed = "failed"
    expired = "expired"      # it finished, and its result has been deleted after VG_EXPORT_RETENTION_HOURS


class ProcessingTask(SQLModel, table=True):
    """Work done on a volume in the background whose result is a file to download (see processing.py). `kind` names
    what is done (\"tiff_export\" is the only one so far), `params` holds its settings as JSON. The title and slug of the
    volume are copied in, so a line stays readable in the list while the volume is renamed."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(index=True)
    volume_id: int = Field(index=True)
    volume_slug: str = ""
    volume_title: str = ""
    params: str = Field(default="{}")
    status: TaskStatus = Field(default=TaskStatus.queued, index=True)
    status_log: str = Field(default="")
    progress_done: int = 0
    progress_total: int = 0
    result_name: Optional[str] = None      # file name inside <volumes>/<slug>/exports/ while the result exists
    download_name: Optional[str] = None    # the name the browser saves it under
    result_bytes: Optional[int] = None
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=get_utc_now, sa_type=DateTime(timezone=True))
    started_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))
    expires_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))

    def queue_failed(self, message: str) -> None:
        """Called by jobs.dispatch when the job could not be queued."""
        self.status = TaskStatus.failed
        self.status_log = (self.status_log or "") + f"{message}\n"
