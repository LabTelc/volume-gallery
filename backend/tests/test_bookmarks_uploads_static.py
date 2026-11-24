"""Saved views, the raw-body upload hand-over, the public pages and the static file headers."""
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlmodel import select

import app.routers.branding as branding_router
from app import workdir
from app.config import settings
from app.main import FrontendFiles
from app.models import AssetStatus, ViewBookmark, VolumeAccess

SPEC = {"options": {"mode": "dvr", "windowCenter": 0.5, "cam": [1, 2, 3]}}


def bookmarks_url(volume_id):
    return f"/api/volumes/{volume_id}/bookmarks"


# --------------------------------------------------------------------------- bookmarks

class TestBookmarks:
    def create(self, client, editor, auth, volume, name="Front view", spec=None, expect=200):
        response = client.post(bookmarks_url(volume.id), json={"name": name, "spec": SPEC if spec is None else spec}, headers=auth(editor))
        assert response.status_code == expect, response.text
        return response.json()

    def test_editor_creates_and_a_granted_reader_reads(self, client, editor, reader, auth, make_volume, db):
        volume = make_volume()
        created = self.create(client, editor, auth, volume)
        assert created["name"] == "Front view" and created["created_by_name"] == editor.username
        assert created["spec"]["options"]["mode"] == "dvr"

        assert client.get(bookmarks_url(volume.id), headers=auth(reader)).status_code == 404  # no grant yet
        db.add(VolumeAccess(user_id=reader.id, volume_id=volume.id))
        db.commit()
        listed = client.get(bookmarks_url(volume.id), headers=auth(reader)).json()
        assert [b["name"] for b in listed] == ["Front view"]
        assert client.get(f"{bookmarks_url(volume.id)}/{created['id']}", headers=auth(reader)).status_code == 200

    def test_readers_cannot_write(self, client, editor, reader, auth, make_volume):
        volume = make_volume()
        created = self.create(client, editor, auth, volume)
        assert client.post(bookmarks_url(volume.id), json={"name": "x", "spec": SPEC}, headers=auth(reader)).status_code == 403
        assert client.put(f"{bookmarks_url(volume.id)}/{created['id']}", json={"name": "x", "spec": SPEC}, headers=auth(reader)).status_code == 403
        assert client.delete(f"{bookmarks_url(volume.id)}/{created['id']}", headers=auth(reader)).status_code == 403

    def test_list_is_sorted_by_name_ignoring_case(self, client, editor, auth, make_volume):
        volume = make_volume()
        for name in ("banana", "Apple", "cherry"):
            self.create(client, editor, auth, volume, name)
        names = [b["name"] for b in client.get(bookmarks_url(volume.id), headers=auth(editor)).json()]
        assert names == ["Apple", "banana", "cherry"]

    def test_names(self, client, editor, auth, make_volume):
        volume = make_volume()
        assert self.create(client, editor, auth, volume, "  a   b  ")["name"] == "a b"
        self.create(client, editor, auth, volume, "   ", expect=400)
        self.create(client, editor, auth, volume, "x" * 81, expect=400)
        self.create(client, editor, auth, volume, "x" * 80)

    def test_update_and_delete(self, client, editor, auth, make_volume):
        volume = make_volume()
        created = self.create(client, editor, auth, volume)
        url = f"{bookmarks_url(volume.id)}/{created['id']}"
        updated = client.put(url, json={"name": "Renamed", "spec": {"options": {"mode": "mip"}}}, headers=auth(editor)).json()
        assert updated["name"] == "Renamed" and updated["spec"]["options"]["mode"] == "mip"
        assert client.delete(url, headers=auth(editor)).status_code == 200
        assert client.get(url, headers=auth(editor)).status_code == 404

    def test_a_bookmark_cannot_be_reached_through_another_volume(self, client, editor, auth, make_volume):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        created = self.create(client, editor, auth, a)
        assert client.get(f"{bookmarks_url(b.id)}/{created['id']}", headers=auth(editor)).status_code == 404
        assert client.delete(f"{bookmarks_url(b.id)}/{created['id']}", headers=auth(editor)).status_code == 404
        assert client.put(f"{bookmarks_url(b.id)}/{created['id']}", json={"name": "x", "spec": SPEC}, headers=auth(editor)).status_code == 404

    def test_limit_per_volume(self, client, editor, auth, make_volume, db):
        volume = make_volume()
        for i in range(100):
            db.add(ViewBookmark(volume_id=volume.id, name=f"b{i}", spec='{"options": {}}'))
        db.commit()
        self.create(client, editor, auth, volume, expect=400)

    @pytest.mark.parametrize("spec", [
        {"options": {"mode": "not-a-mode"}},
        {"options": {"cam": [1, 2]}},
        {"options": {"clipMin": [0, 0]}},
        {"options": {"tfColorStops": [{"pos": 0.5, "color": "red"}]}},
        {"options": {"upAxis": "w"}},
        {"options": {}, "min_level": 65},
        {"options": {}, "thumb": "data:text/html;base64,PHNjcmlwdD4="},
        {"options": {}, "thumb": "javascript:alert(1)"},
        {"options": {}, "slices": {f"k{i}": "v" for i in range(21)}},
        {"options": {}, "slices": {"k": "v" * 4001}},
        {},
    ])
    def test_invalid_specs_are_refused(self, client, editor, auth, make_volume, spec):
        volume = make_volume()
        self.create(client, editor, auth, volume, spec=spec, expect=422)

    def test_a_jpeg_thumbnail_is_accepted(self, client, editor, auth, make_volume):
        volume = make_volume()
        spec = {"options": {}, "thumb": "data:image/jpeg;base64,/9j/4AAQ"}
        assert self.create(client, editor, auth, volume, spec=spec)["spec"]["thumb"].startswith("data:image/jpeg;base64,")

    def test_deleting_the_volume_deletes_its_bookmarks(self, client, editor, auth, make_volume, db):
        volume = make_volume()
        self.create(client, editor, auth, volume)
        client.delete(f"/api/volumes/{volume.id}", headers=auth(editor))
        assert db.exec(select(ViewBookmark)).all() == []


# --------------------------------------------------------------------------- uploads

class TestUploadAuth:
    def test_nginx_asks_before_it_writes_to_disk(self, client, editor, reader, auth):
        assert client.get("/api/uploads/auth", headers=auth(editor)).status_code == 204
        assert client.get("/api/uploads/auth", headers=auth(reader)).status_code == 403
        assert client.get("/api/uploads/auth").status_code == 401


class TestStageUpload:
    def put(self, client, headers, kind="volume", name="stack.zip", body=b"PK\x03\x04data", **kw):
        return client.put(f"/api/uploads/{kind}", params={"name": name}, content=body, headers=headers, **kw)

    def test_a_body_sent_straight_to_the_app_is_staged_under_an_upload_id(self, client, editor, auth):
        response = self.put(client, auth(editor))
        assert response.status_code == 200
        body = response.json()
        assert body["size"] == 8
        staged = workdir.staging_dir("volume") / body["upload_id"]
        assert staged.read_bytes() == b"PK\x03\x04data"
        assert body["upload_id"].startswith(f"u{editor.id}_") and body["upload_id"].endswith("__stack.zip")

    def test_meshes_are_staged_in_the_data_folder(self, client, editor, auth):
        upload_id = self.put(client, auth(editor), "mesh", "scan.ply").json()["upload_id"]
        assert (settings.DATA_DIR / ".uploads" / upload_id).is_file()

    def test_an_empty_body_is_refused_and_leaves_nothing_behind(self, client, editor, auth):
        assert self.put(client, auth(editor), body=b"").status_code == 400
        assert list(workdir.staging_dir("volume").iterdir()) == []

    def test_only_editors(self, client, reader, auth):
        assert self.put(client, auth(reader)).status_code == 403
        assert self.put(client, {}).status_code == 401

    def test_unknown_kind(self, client, editor, auth):
        assert self.put(client, auth(editor), "executable").status_code == 422

    def test_the_name_is_required_and_bounded(self, client, editor, auth):
        assert client.put("/api/uploads/volume", content=b"x", headers=auth(editor)).status_code == 422
        assert self.put(client, auth(editor), name="x" * 256).status_code == 422

    @pytest.mark.parametrize("name", ["../../etc/passwd", "..\\..\\windows\\win.ini", "a b;rm -rf.zip", "ünï.zip", "...zip"])
    def test_hostile_file_names_are_sanitised_into_the_staging_folder(self, client, editor, auth, name):
        upload_id = self.put(client, auth(editor), name=name).json()["upload_id"]
        assert "/" not in upload_id and "\\" not in upload_id and ".." not in upload_id.split("__", 1)[1]
        assert (workdir.staging_dir("volume") / upload_id).is_file()

    @pytest.mark.parametrize("announced", ["../../etc/passwd", "/etc/passwd", "gallery.db", "12345", "abcdefghij", "0000000001/../x"])
    def test_a_forged_x_upload_file_header_cannot_name_an_arbitrary_file(self, client, editor, auth, announced):
        # Behind nginx the header is set by nginx only; without it a client could send one. Only a name that
        # looks like an nginx temp file (ten digits) inside the staging folder is accepted.
        response = self.put(client, {**auth(editor), "X-Upload-File": announced}, body=b"")
        assert response.status_code == 400

    def test_a_real_hand_over_is_renamed_not_copied(self, client, editor, auth):
        staging = workdir.staging_dir("volume")
        handed = staging / "0000000042"
        handed.write_bytes(b"from nginx")
        response = self.put(client, {**auth(editor), "X-Upload-File": "/some/other/mount/0000000042"}, body=b"")
        assert response.status_code == 200 and response.json()["size"] == 10
        assert not handed.exists()
        assert (staging / response.json()["upload_id"]).read_bytes() == b"from nginx"


class TestWorkdirClaims:
    def stage(self, user_id=1, kind="volume", name="a.zip"):
        upload_id = workdir.new_upload_id(user_id, name)
        path = workdir.staging_dir(kind)
        path.mkdir(parents=True, exist_ok=True)
        (path / upload_id).write_bytes(b"x")
        return upload_id

    def test_the_owner_can_claim(self):
        claimed = workdir.claim_upload(self.stage(5), "volume", 5)
        assert claimed.filename == "a.zip" and claimed.path.is_file()

    def test_another_user_cannot_claim_it(self):
        with pytest.raises(HTTPException) as exc:
            workdir.claim_upload(self.stage(5), "volume", 6)
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("bad", ["", None, "nope", "u5_short__a.zip", "../u5_" + "a" * 32 + "__a.zip", "u5_" + "g" * 32 + "__a.zip"])
    def test_malformed_ids_are_refused(self, bad):
        with pytest.raises(HTTPException):
            workdir.claim_upload(bad, "volume", 5)

    def test_the_wrong_kind(self):
        with pytest.raises(HTTPException):
            workdir.claim_upload(self.stage(5, "volume"), "mesh", 5)

    def test_a_staged_file_can_only_be_reserved_once(self):
        claimed = workdir.claim_upload(self.stage(5), "volume", 5)
        workdir.reserve_uploads([claimed.path])
        try:
            with pytest.raises(HTTPException):
                workdir.reserve_uploads([claimed.path])
        finally:
            workdir.drop_staged([(claimed.path, None)])
        assert not claimed.path.exists()

    def test_the_same_file_twice_in_one_request_is_refused(self):
        claimed = workdir.claim_upload(self.stage(5), "volume", 5)
        with pytest.raises(HTTPException):
            workdir.reserve_uploads([claimed.path, claimed.path])

    @pytest.mark.parametrize("name,expected", [
        ("scan.ply", "scan.ply"), ("../../x.zip", "x.zip"), ("a b.zip", "a_b.zip"), ("", "upload"), (".hidden", "hidden"),
        ("C:\\temp\\scan.obj", "scan.obj"),
    ])
    def test_safe_filename(self, name, expected):
        assert workdir.safe_filename(name) == expected

    def test_old_unclaimed_uploads_are_swept_and_nginx_temp_files_are_not(self):
        staging = workdir.staging_dir("volume")
        old, fresh, nginx_temp = staging / "u1_old", staging / "u1_fresh", staging / "0000000007"
        for f in (old, fresh, nginx_temp):
            f.write_bytes(b"x")
        long_ago = time.time() - 3600 * (settings.UPLOAD_STAGING_MAX_AGE_HOURS + 1)
        os.utime(old, (long_ago, long_ago))
        os.utime(nginx_temp, (long_ago, long_ago))
        workdir.sweep_staging(force=True)
        assert not old.exists() and fresh.exists() and nginx_temp.exists()


# --------------------------------------------------------------------------- public pages and static files

class TestBranding:
    def test_is_public(self, client):
        response = client.get("/api/branding")
        assert response.status_code == 200
        assert isinstance(response.json(), dict)

    def test_missing_logo_is_a_404_not_an_error(self, client, monkeypatch):
        monkeypatch.setattr(branding_router, "logo_path", lambda key: None)
        assert client.get("/api/branding/logo").status_code == 404
        assert client.get("/api/branding/logo-dark").status_code == 404


class TestStaticFiles:
    @pytest.mark.parametrize("name", ["volume.3fa9c0d1b2e4.js", "style.9bd1e0c4a7f2.css", "volume.3fa9c0d1b2e4.js.map"])
    def test_hashed_names_match(self, name):
        assert FrontendFiles._HASHED.search(name)

    @pytest.mark.parametrize("name", ["volume.js", "style.css", "sw.js", "volume.3fa9c0d1b2e.js", "volume.3FA9C0D1B2E4.js", "x.3fa9c0d1b2e4.png"])
    def test_other_names_do_not(self, name):
        assert FrontendFiles._HASHED.search(name) is None

    def test_pages_are_revalidated(self, client):
        response = client.get("/index.html")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"

    def test_hashed_assets_are_cached_for_good(self, client):
        folder = Path(settings.FRONTEND_DIR)
        (folder / "app.3fa9c0d1b2e4.js").write_text("// x")
        (folder / "other.js").write_text("// y")
        assert client.get("/app.3fa9c0d1b2e4.js").headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "immutable" not in client.get("/other.js").headers.get("cache-control", "")

    def test_the_service_worker_may_control_the_whole_origin(self, client):
        js = Path(settings.FRONTEND_DIR) / "js"
        js.mkdir(exist_ok=True)
        (js / "sw.js").write_text("// sw")
        response = client.get("/js/sw.js")
        assert response.status_code == 200
        assert response.headers["service-worker-allowed"] == "/"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["content-type"].startswith("text/javascript")


def test_asset_statuses_the_ui_relies_on():
    assert {s.value for s in AssetStatus} == {"uploaded", "processing", "ready", "failed"}
