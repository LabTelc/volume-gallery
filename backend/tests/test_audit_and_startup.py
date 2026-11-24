"""The audit log, database upgrade, and what the app does at start-up."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import select
from starlette.requests import Request

import app.database as database
from app import audit
from app.config import settings
from app.models import AssetStatus, AuditEvent, PointCloud, Role, User, Volume
from app.security import verify_password
from app.utils import get_utc_now


def make_request(headers=None, client=("9.9.9.9", 1234)):
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    })


# --------------------------------------------------------------------------- audit helpers

class TestClientIp:
    def test_prefers_x_real_ip(self):
        assert audit.client_ip(make_request({"X-Real-IP": "1.2.3.4"})) == "1.2.3.4"

    def test_falls_back_to_the_socket_address(self):
        assert audit.client_ip(make_request()) == "9.9.9.9"

    def test_no_request_no_address(self):
        assert audit.client_ip(None) == ""

    def test_is_clipped(self):
        assert len(audit.client_ip(make_request({"X-Real-IP": "9" * 200}))) == 64


class TestRecord:
    def test_record_is_saved_with_the_callers_commit(self, db):
        audit.record(db, make_request({"X-Real-IP": "5.6.7.8"}), None, "volume.create", "volume", 3, "my-vol", title="T")
        db.commit()
        event = db.exec(select(AuditEvent)).one()
        assert (event.action, event.target_type, event.target_id, event.target_name) == ("volume.create", "volume", 3, "my-vol")
        assert event.ip == "5.6.7.8"
        assert event.actor_name == "" and event.actor_id is None
        assert audit.parse_detail(event) == {"title": "T"}

    def test_record_without_commit_is_rolled_back_with_the_action(self, db):
        audit.record(db, None, None, "user.delete", "user", 1, "x")
        db.rollback()
        assert db.exec(select(AuditEvent)).all() == []

    def test_record_now_commits(self, db):
        audit.record_now(db, None, None, "auth.login_failed")
        assert len(db.exec(select(AuditEvent)).all()) == 1

    def test_actor_is_copied_in(self, db, admin):
        audit.record_now(db, None, admin, "user.create", "user", 9, "newbie")
        event = db.exec(select(AuditEvent)).one()
        assert event.actor_id == admin.id and event.actor_name == admin.username

    def test_long_text_is_clipped_and_none_details_are_dropped(self, db):
        audit.record_now(db, None, None, "x", target_name="n" * 500, long="v" * 500, skipped=None)
        event = db.exec(select(AuditEvent)).one()
        assert len(event.target_name) == 200
        detail = audit.parse_detail(event)
        assert len(detail["long"]) == 200 and "skipped" not in detail

    def test_parse_detail_survives_garbage(self):
        assert audit.parse_detail(AuditEvent(action="x", detail="not json")) == {}
        assert audit.parse_detail(AuditEvent(action="x", detail='["a list"]')) == {}
        assert audit.parse_detail(AuditEvent(action="x", detail="")) == {}


class TestPrune:
    def _add(self, db, days_old):
        db.add(AuditEvent(action="a", at=get_utc_now() - timedelta(days=days_old)))
        db.commit()

    def test_deletes_only_old_lines(self, db):
        for age in (1, 10, 100, 400):
            self._add(db, age)
        assert audit.prune(db, days=30) == 2
        assert len(db.exec(select(AuditEvent)).all()) == 2

    def test_zero_keeps_everything(self, db):
        self._add(db, 4000)
        assert audit.prune(db, days=0) == 0
        assert len(db.exec(select(AuditEvent)).all()) == 1

    def test_uses_the_setting_by_default(self, db, monkeypatch):
        monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 5)
        self._add(db, 1)
        self._add(db, 9)
        assert audit.prune(db) == 1


# --------------------------------------------------------------------------- database upgrade

class TestAddMissingColumns:
    def _old_database(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
        with engine.begin() as conn:
            for table in ("volume", "mesh", "pointcloud"):
                conn.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, slug TEXT)")
                conn.exec_driver_sql(f"INSERT INTO {table} (slug) VALUES ('legacy')")
        return engine

    def _columns(self, engine, table):
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}

    def test_old_schema_gets_the_new_columns(self, tmp_path, monkeypatch):
        engine = self._old_database(tmp_path)
        monkeypatch.setattr(database, "engine", engine)
        database._add_missing_columns()
        for table in ("volume", "mesh", "pointcloud"):
            assert {"folder_id", "tags"} <= self._columns(engine, table)

    def test_existing_rows_keep_working(self, tmp_path, monkeypatch):
        engine = self._old_database(tmp_path)
        monkeypatch.setattr(database, "engine", engine)
        database._add_missing_columns()
        with engine.connect() as conn:
            row = conn.exec_driver_sql("SELECT slug, folder_id, tags FROM volume").one()
        assert tuple(row) == ("legacy", None, "")

    def test_is_idempotent(self, tmp_path, monkeypatch):
        engine = self._old_database(tmp_path)
        monkeypatch.setattr(database, "engine", engine)
        database._add_missing_columns()
        database._add_missing_columns()
        assert "tags" in self._columns(engine, "mesh")


# --------------------------------------------------------------------------- start-up

class TestBootstrapAdmin:
    def _run(self):
        from app.main import _bootstrap_admin
        _bootstrap_admin()

    def test_creates_the_first_admin(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USER", "root")
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-password")
        self._run()
        user = db.exec(select(User)).one()
        assert user.username == "root" and user.role == Role.admin
        assert verify_password("bootstrap-password", user.password_hash)

    def test_does_nothing_without_a_password(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "")
        self._run()
        assert db.exec(select(User)).all() == []

    def test_does_nothing_when_users_exist(self, db, admin, monkeypatch):
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USER", "intruder")
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-password")
        self._run()
        assert [u.username for u in db.exec(select(User)).all()] == [admin.username]

    def test_running_twice_creates_one_admin(self, db, monkeypatch):
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-password")
        self._run()
        self._run()
        assert len(db.exec(select(User)).all()) == 1


class TestInterruptedJobs:
    def test_processing_assets_are_marked_failed_and_others_left_alone(self, db, make_volume):
        from app.main import _recover_interrupted_jobs
        stuck = make_volume("stuck-vol", status=AssetStatus.processing, with_data=False)
        done = make_volume("done-vol", status=AssetStatus.ready)
        db.add(PointCloud(slug="stuck-pc", title="PC", status=AssetStatus.processing))
        db.commit()

        _recover_interrupted_jobs()

        db.expire_all()
        assert db.get(Volume, stuck.id).status == AssetStatus.failed
        assert "interrupted" in db.get(Volume, stuck.id).status_log
        assert db.get(Volume, done.id).status == AssetStatus.ready
        assert db.exec(select(PointCloud)).one().status == AssetStatus.failed


class TestLifespan:
    def test_app_starts_creates_the_admin_and_serves_the_login(self, monkeypatch):
        from app.main import app
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_USER", "boss")
        monkeypatch.setattr(settings, "BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-password")
        with TestClient(app) as client:
            response = client.post("/api/auth/login", data={"username": "boss", "password": "bootstrap-password"})
            assert response.status_code == 200
            assert response.json()["role"] == "admin"

    def test_app_refuses_to_start_with_a_placeholder_secret(self, monkeypatch):
        from app.main import app
        monkeypatch.setattr(settings, "SECRET_KEY", "change_me_insecure_default")
        with pytest.raises(RuntimeError, match="VG_SECRET_KEY"):
            with TestClient(app):
                pass
