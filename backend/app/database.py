import logging

from sqlmodel import SQLModel, create_engine, Session
from . import report_store
from .config import settings

log = logging.getLogger("volume-gallery.database")

engine = create_engine(f"sqlite:///{settings.DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30})


# Columns added after the first release. create_all() only creates missing tables, it never
# alters existing ones, so a database made by an older version gets these added here.
_ADDED_COLUMNS = {
    table: (("folder_id", "INTEGER"), ("tags", "TEXT NOT NULL DEFAULT ''"))
    for table in ("volume", "mesh", "pointcloud")
}


def _add_missing_columns():
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_folder_id ON {table} (folder_id)")


def _migrate_report_text():
    """Moves the report texts out of the database. Older versions kept them in volumereport.markdown; now each one is
    the file <volumes>/<slug>/report/report.md (report_store.py). Every text is written and read back first, and only
    then is the column dropped, so a failure (a full or read-only disk) stops the start-up with the texts still in the
    database; run again after fixing it. A file that already exists is kept, not overwritten. Does nothing once the
    column is gone."""
    def has_column(conn):
        return "markdown" in {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(volumereport)")}

    with engine.connect() as conn:
        if not has_column(conn):
            return
        rows = conn.exec_driver_sql(
            "SELECT r.volume_id, r.markdown, v.slug FROM volumereport r LEFT JOIN volume v ON v.id = r.volume_id"
        ).all()
    moved = 0
    for volume_id, text, slug in rows:
        if slug is None:
            log.warning("report text of volume %s dropped: the volume no longer exists", volume_id)
            continue
        if not text:
            continue
        path = report_store.report_path(slug)
        try:
            if path.exists():
                if report_store.read_text(slug) != text:
                    log.warning("%s already exists and differs from the database text; the file is kept", path)
                continue
            report_store.write_text(slug, text)
            if report_store.read_text(slug) != text:
                raise OSError("read-back differs from what was written")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"Could not move the report text of volume '{slug}' to {path}: {exc}. "
                "Nothing was removed from the database; fix the problem and start again."
            ) from exc
        moved += 1
    with engine.begin() as conn:
        if not has_column(conn):  # another process did it meanwhile
            return
        try:
            conn.exec_driver_sql("ALTER TABLE volumereport DROP COLUMN markdown")
        except Exception:
            if has_column(conn):
                raise
    if moved:
        log.warning("moved %d report text(s) from the database to <volume>/report/report.md", moved)


def init_db():
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _migrate_report_text()


def get_session():
    with Session(engine) as session:
        yield session