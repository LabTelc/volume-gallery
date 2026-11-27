"""Shared fixtures for the backend tests.

The app reads its configuration (app/config.py) once, when it is first imported, and creates its folders
and its SQLite engine right then. So the environment is set up HERE, before anything from `app` is
imported, and points everything at a throw-away directory. No test touches a real data folder.

Each test starts from an empty database and empty volume folders (see `_clean_state`).
"""
import atexit
import json
import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TMP = Path(tempfile.mkdtemp(prefix="vg-tests-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

_FRONTEND = _TMP / "frontend"
_FRONTEND.mkdir()
(_FRONTEND / "index.html").write_text("<!doctype html><title>stub</title>", encoding="utf-8")

os.environ.update({
    "VG_DATA_DIR": str(_TMP / "data"),
    "VG_PROCESSING_DIR": str(_TMP / "processing"),
    "VG_FRONTEND_DIR": str(_FRONTEND),
    "VG_SECRET_KEY": "test-secret-key-0123456789abcdef0123456789abcdef-0123456789abcde",
    "VG_REDIS_URL": "",               # no job queue: uploads would be converted in-process
    "VG_SHARD_ACCEL_REDIRECT": "0",   # serve files from Python, not through nginx's X-Accel-Redirect
    "VG_HISTOGRAM_BACKFILL": "0",
    "VG_MIN_PASSWORD_LENGTH": "8",
    "VG_ADMIN_PASSWORD": "",
    "VG_COOKIE_SECURE": "1",          # the production setting: __Host-vg_session, Secure
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AssetStatus, Role, User, Volume  # noqa: E402
from app.routers import volumes as volumes_router  # noqa: E402
from app import sessions as vg_sessions  # noqa: E402
from app.security import hash_password  # noqa: E402

PASSWORD = "correct-horse-battery"


@lru_cache(maxsize=None)
def _hash(password: str) -> str:
    # Argon2id with the default profile takes ~50-100 ms: hash each distinct password only once.
    return hash_password(password)


# --------------------------------------------------------------------------- state

@pytest.fixture(autouse=True)
def _clean_state():
    SQLModel.metadata.drop_all(engine)
    init_db()
    for folder in (settings.VOLUMES_DIR, settings.MESHES_DIR, settings.POINTCLOUDS_DIR):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    for staging in (settings.PROCESSING_DIR / ".uploads", settings.DATA_DIR / ".uploads"):
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
    volumes_router._TOKEN_CACHE.clear()
    volumes_router._VOLUME_LOC_CACHE.clear()
    yield


@pytest.fixture
def client():
    # Not used as a context manager on purpose: that would run the app's start-up (bootstrap admin,
    # housekeeping). The tests that cover start-up do it themselves.
    # Like the frontend, it sends the CSRF header on everything (a test that wants it missing uses `raw_client`).
    return TestClient(app, headers={"X-Requested-With": "vg"})


@pytest.fixture
def allow_bearer(monkeypatch):
    """The compatibility mode for API / CLI clients: VG_ALLOW_BEARER_AUTH=1 (off by default)."""
    monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", True)


@pytest.fixture
def raw_client():
    """A client that sends no CSRF header (and nothing else) of its own."""
    return TestClient(app)


@pytest.fixture
def db():
    with Session(engine) as session:
        yield session


# --------------------------------------------------------------------------- users

@pytest.fixture
def make_user(db):
    def _make(username, role=Role.reader, *, password=PASSWORD, disabled=False, pending=False):
        user = User(
            username=username,
            role=role,
            disabled=disabled,
            password_hash="" if pending else _hash(password),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    return _make


def session_headers(raw: str) -> dict:
    """What a browser holding session id `raw` sends: the cookie, the CSRF header and an Origin of this app."""
    return {
        "Cookie": f"{vg_sessions.cookie_name()}={raw}",
        "X-Requested-With": "vg",
        "Origin": "http://testserver",
    }


@pytest.fixture
def auth():
    """auth(user) -> the headers of a browser that has just signed in as that user (a new server-side session)."""
    def _auth(user, *, remember=False):
        with Session(engine) as db:
            raw, _row = vg_sessions.create_session(db, user, remember=remember)
        return session_headers(raw)
    return _auth


@pytest.fixture
def admin(make_user):
    return make_user("alice-admin", Role.admin)


@pytest.fixture
def editor(make_user):
    return make_user("erin-editor", Role.editor)


@pytest.fixture
def reader(make_user):
    return make_user("rita-reader", Role.reader)


@pytest.fixture
def reader2(make_user):
    return make_user("ron-reader", Role.reader)


# --------------------------------------------------------------------------- volumes

def volume_json(levels: int = 3) -> dict:
    dims = [[128 >> i, 128 >> i, 128 >> i] for i in range(levels)]
    return {
        "voxelSpacing": [1.0, 1.0, 1.0],
        "originalDimensions": dims[0],
        "maxLod": levels - 1,
        "levels": [{"lod": i, "dimensions": dims[i]} for i in range(levels)],
    }


def write_volume_files(slug: str, levels: int = 3) -> Path:
    folder = settings.VOLUMES_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "volume.json").write_text(json.dumps(volume_json(levels)), encoding="utf-8")
    for i in range(levels):
        (folder / f"lod{i}.bin").write_bytes(f"LOD{i}".encode() * 16)
        (folder / f"lod{i}_index.json").write_text(json.dumps({"lod": i}), encoding="utf-8")
    return folder


@pytest.fixture
def make_volume(db):
    def _make(slug="vol-a", *, status=AssetStatus.ready, levels=3, with_data=True, has_mesh=False, **fields):
        volume = Volume(
            slug=slug,
            title=fields.pop("title", slug.upper()),
            status=status,
            num_lod_levels=levels - 1,
            has_mesh=has_mesh,
            **fields,
        )
        db.add(volume)
        db.commit()
        db.refresh(volume)
        if with_data:
            write_volume_files(slug, levels)
        return volume
    return _make


# --------------------------------------------------------------------------- share links

@pytest.fixture
def create_share(client, editor, auth):
    def _create(volume_id, *, asset_type="volume", expect=200, **payload):
        body = {"asset_type": asset_type, "asset_id": volume_id, **payload}
        response = client.post("/api/shares", json=body, headers=auth(editor))
        assert response.status_code == expect, response.text
        return response.json()
    return _create


@pytest.fixture
def open_share(client):
    """open_share(code) -> Authorization header of the guest session that /s/<code> hands out."""
    def _open(code):
        response = client.get(f"/s/{code}", follow_redirects=False)
        assert response.status_code == 302, response.text
        fragment = parse_qs(urlparse(response.headers["location"]).fragment)
        return {"Authorization": f"Bearer {fragment['vg_st'][0]}", "X-Requested-With": "vg"}
    return _open
