"""A volume's report page: its markdown text, the images in it, and the files attached to the volume."""
import io

from PIL import Image

from app.config import settings
from app.models import ReportImage, VolumeAttachment, VolumeReport


def png(size=(4, 3), colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def attach(client, headers, volume_id, name="notes.txt", body=b"hello", **extra):
    return client.post(f"/api/volumes/{volume_id}/attachments", files={"file": (name, body)}, data=extra, headers=headers)


class TestReportText:
    def test_empty_until_an_editor_writes_it(self, client, make_volume, reader, auth):
        volume = make_volume()
        body = client.get(f"/api/volumes/{volume.id}/report", headers=auth(reader))
        # a reader sees only the volumes they were granted
        assert body.status_code == 404

    def test_editor_saves_and_a_reader_with_access_reads(self, client, make_volume, editor, reader, auth, db):
        from app.models import VolumeAccess
        volume = make_volume()
        db.add(VolumeAccess(volume_id=volume.id, user_id=reader.id))
        db.commit()
        assert client.get(f"/api/volumes/{volume.id}/report", headers=auth(reader)).json()["markdown"] == ""
        text = "# Scan\n\nInline $E=mc^2$ and an image ![x](img:1)"
        saved = client.put(f"/api/volumes/{volume.id}/report", json={"markdown": text}, headers=auth(editor))
        assert saved.status_code == 200 and saved.json()["updated_by_name"] == editor.username
        got = client.get(f"/api/volumes/{volume.id}/report", headers=auth(reader)).json()
        assert got["markdown"] == text and got["can_download"] is True

    def test_a_second_save_replaces_the_text(self, client, make_volume, editor, auth, db):
        volume = make_volume()
        for text in ("one", "two"):
            assert client.put(f"/api/volumes/{volume.id}/report", json={"markdown": text}, headers=auth(editor)).status_code == 200
        rows = db.query(VolumeReport).all()
        assert len(rows) == 1 and rows[0].updated_by == editor.id
        assert client.get(f"/api/volumes/{volume.id}/report", headers=auth(editor)).json()["markdown"] == "two"

    def test_the_text_is_a_file_not_a_column(self, client, make_volume, editor, auth, db):
        volume = make_volume()
        text = "# Scan\r\n\r\nzaostření \u2013 $E=mc^2$\n"
        assert client.put(f"/api/volumes/{volume.id}/report", json={"markdown": text}, headers=auth(editor)).status_code == 200
        path = settings.VOLUMES_DIR / volume.slug / "report" / "report.md"
        assert path.read_bytes() == text.encode("utf-8")  # as written: no newline translation
        assert "markdown" not in VolumeReport.__table__.columns
        assert not list(path.parent.glob(".*.tmp"))
        # an edit made on disk shows up at once
        path.write_text("edited on disk", encoding="utf-8")
        assert client.get(f"/api/volumes/{volume.id}/report", headers=auth(editor)).json()["markdown"] == "edited on disk"

    def test_the_file_goes_with_the_volume_and_sits_beside_the_images(self, client, make_volume, editor, auth):
        volume = make_volume()
        client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "x"}, headers=auth(editor))
        image = client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("report.md", png())}, headers=auth(editor)).json()
        folder = settings.VOLUMES_DIR / volume.slug / "report"
        assert (folder / "report.md").read_text() == "x"  # an image named report.md is stored as <id>__report.md
        assert (folder / f"{image['id']}__report.md").is_file()
        client.delete(f"/api/volumes/{volume.id}", headers=auth(editor))
        assert not folder.exists()

    def test_a_failed_write_keeps_the_old_text(self, client, make_volume, editor, auth, monkeypatch):
        volume = make_volume()
        client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "old"}, headers=auth(editor))
        import os
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", boom)
        assert client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "new"}, headers=auth(editor)).status_code == 500
        monkeypatch.undo()
        assert client.get(f"/api/volumes/{volume.id}/report", headers=auth(editor)).json()["markdown"] == "old"
        assert not list((settings.VOLUMES_DIR / volume.slug / "report").glob(".*.tmp"))

    def test_readers_cannot_write(self, client, make_volume, reader, auth, db):
        from app.models import VolumeAccess
        volume = make_volume()
        db.add(VolumeAccess(volume_id=volume.id, user_id=reader.id))
        db.commit()
        assert client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "x"}, headers=auth(reader)).status_code == 403
        assert client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "x"}).status_code == 401

    def test_text_over_the_limit_is_refused(self, client, make_volume, editor, auth):
        volume = make_volume()
        response = client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "x" * 1_000_001}, headers=auth(editor))
        assert response.status_code == 422

    def test_a_share_link_guest_reads_it(self, client, make_volume, editor, auth, create_share, open_share):
        volume = make_volume()
        client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "shared"}, headers=auth(editor))
        guest = open_share(create_share(volume.id)["code"])
        body = client.get(f"/api/volumes/{volume.id}/report", headers=guest).json()
        assert body["markdown"] == "shared" and body["can_download"] is False
        other = make_volume("vol-b")
        assert client.get(f"/api/volumes/{other.id}/report", headers=guest).status_code == 404


class TestImages:
    def test_upload_view_and_delete(self, client, make_volume, editor, auth):
        volume = make_volume()
        up = client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("plot.png", png())}, headers=auth(editor))
        assert up.status_code == 200, up.text
        image = up.json()
        assert image["content_type"] == "image/png"
        assert (settings.VOLUMES_DIR / volume.slug / "report" / f"{image['id']}__plot.png").is_file()
        got = client.get(f"/api/volumes/{volume.id}/report/images/{image['id']}", headers=auth(editor))
        assert got.status_code == 200 and got.headers["content-type"] == "image/png"
        assert got.headers["x-content-type-options"] == "nosniff"
        assert [i["id"] for i in client.get(f"/api/volumes/{volume.id}/report/images", headers=auth(editor)).json()] == [image["id"]]
        assert client.delete(f"/api/volumes/{volume.id}/report/images/{image['id']}", headers=auth(editor)).status_code == 200
        assert client.get(f"/api/volumes/{volume.id}/report/images/{image['id']}", headers=auth(editor)).status_code == 404
        assert not list((settings.VOLUMES_DIR / volume.slug / "report").iterdir())

    def test_the_type_comes_from_the_file_not_from_its_name(self, client, make_volume, editor, auth):
        volume = make_volume()
        up = client.post(f"/api/volumes/{volume.id}/report/images",
                         files={"file": ("pic.svg", png(), "image/svg+xml")}, headers=auth(editor))
        assert up.status_code == 200 and up.json()["content_type"] == "image/png"

    def test_svg_and_other_files_are_refused(self, client, make_volume, editor, auth):
        volume = make_volume()
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        for name, body in (("a.svg", svg), ("a.png", b"not an image"), ("a.html", b"<script>1</script>")):
            response = client.post(f"/api/volumes/{volume.id}/report/images", files={"file": (name, body)}, headers=auth(editor))
            assert response.status_code == 400, name

    def test_too_large(self, client, make_volume, editor, auth, monkeypatch):
        monkeypatch.setattr(settings, "REPORT_IMAGE_MAX_MB", 0)
        volume = make_volume()
        response = client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("a.png", png())}, headers=auth(editor))
        assert response.status_code == 413

    def test_only_editors_upload_or_list(self, client, make_volume, reader, auth, db):
        from app.models import VolumeAccess
        volume = make_volume()
        db.add(VolumeAccess(volume_id=volume.id, user_id=reader.id))
        db.commit()
        assert client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("a.png", png())}, headers=auth(reader)).status_code == 403
        assert client.get(f"/api/volumes/{volume.id}/report/images", headers=auth(reader)).status_code == 403

    def test_an_image_is_only_served_under_its_own_volume(self, client, make_volume, editor, auth):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        image = client.post(f"/api/volumes/{a.id}/report/images", files={"file": ("a.png", png())}, headers=auth(editor)).json()
        assert client.get(f"/api/volumes/{b.id}/report/images/{image['id']}", headers=auth(editor)).status_code == 404

    def test_a_reader_without_access_cannot_fetch_it(self, client, make_volume, editor, reader, auth):
        volume = make_volume()
        image = client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("a.png", png())}, headers=auth(editor)).json()
        assert client.get(f"/api/volumes/{volume.id}/report/images/{image['id']}", headers=auth(reader)).status_code == 404


class TestAttachments:
    def test_attach_list_download(self, client, make_volume, editor, auth):
        volume = make_volume()
        response = attach(client, auth(editor), volume.id, "Scan report (v2).pdf", b"%PDF-1.4 data", description="  final ")
        assert response.status_code == 200, response.text
        att = response.json()
        assert att["filename"] == "Scan report (v2).pdf" and att["size"] == 13 and att["description"] == "final"
        assert att["created_by_name"] == editor.username
        listing = client.get(f"/api/volumes/{volume.id}/attachments", headers=auth(editor)).json()
        assert [a["id"] for a in listing] == [att["id"]]
        stored = settings.VOLUMES_DIR / volume.slug / "attachments" / f"{att['id']}__Scan_report_v2_.pdf"
        assert stored.read_bytes() == b"%PDF-1.4 data"

        link = client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=auth(editor)).json()
        got = client.get(link["url"])
        assert got.status_code == 200 and got.content == b"%PDF-1.4 data"
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["content-disposition"].startswith("attachment;") and "Scan report (v2).pdf" in got.headers["content-disposition"]

    def test_a_html_attachment_is_never_shown_by_the_browser(self, client, make_volume, editor, auth):
        volume = make_volume()
        att = attach(client, auth(editor), volume.id, "x.html", b"<script>alert(1)</script>").json()
        url = client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=auth(editor)).json()["url"]
        got = client.get(url)
        assert got.headers["content-type"] == "application/octet-stream"
        assert got.headers["content-disposition"].startswith("attachment")

    def test_a_name_with_a_path_or_unicode(self, client, make_volume, editor, auth):
        volume = make_volume()
        att = attach(client, auth(editor), volume.id, "../../etc/pásswd ü.bin", b"1").json()
        assert att["filename"] == "pásswd ü.bin"
        url = client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=auth(editor)).json()["url"]
        assert "filename*=UTF-8''" in client.get(url).headers["content-disposition"]
        assert all(p.parent.name == "attachments" for p in (settings.VOLUMES_DIR / volume.slug).rglob("*__*"))

    def test_several_files_with_the_same_name(self, client, make_volume, editor, auth):
        volume = make_volume()
        a = attach(client, auth(editor), volume.id, "d.txt", b"one").json()
        b = attach(client, auth(editor), volume.id, "d.txt", b"two").json()
        assert a["id"] != b["id"]
        for att, content in ((a, b"one"), (b, b"two")):
            url = client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=auth(editor)).json()["url"]
            assert client.get(url).content == content

    def test_readers_see_and_download_but_do_not_change(self, client, make_volume, editor, reader, auth, db):
        from app.models import VolumeAccess
        volume = make_volume()
        att = attach(client, auth(editor), volume.id).json()
        assert client.get(f"/api/volumes/{volume.id}/attachments", headers=auth(reader)).status_code == 404
        db.add(VolumeAccess(volume_id=volume.id, user_id=reader.id))
        db.commit()
        assert len(client.get(f"/api/volumes/{volume.id}/attachments", headers=auth(reader)).json()) == 1
        assert client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=auth(reader)).status_code == 200
        assert attach(client, auth(reader), volume.id).status_code == 403
        assert client.patch(f"/api/volumes/{volume.id}/attachments/{att['id']}", json={"description": "x"}, headers=auth(reader)).status_code == 403
        assert client.delete(f"/api/volumes/{volume.id}/attachments/{att['id']}", headers=auth(reader)).status_code == 403

    def test_a_download_link_is_for_its_volume_only(self, client, make_volume, editor, auth):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        att = attach(client, auth(editor), a.id).json()
        url = client.get(f"/api/volumes/{a.id}/attachments/{att['id']}/download-url", headers=auth(editor)).json()["url"]
        token = url.split("/attachment-file/")[1].split("/")[0]
        assert client.get(f"/api/volumes/{b.id}/attachment-file/{token}/{att['id']}").status_code == 403
        assert client.get(f"/api/volumes/{a.id}/attachment-file/not-a-token/{att['id']}").status_code == 403

    def test_share_links_download_only_when_they_allow_it(self, client, make_volume, editor, auth, create_share, open_share):
        volume = make_volume()
        att = attach(client, auth(editor), volume.id).json()
        closed = open_share(create_share(volume.id)["code"])
        assert len(client.get(f"/api/volumes/{volume.id}/attachments", headers=closed).json()) == 1
        assert client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=closed).status_code == 403
        allowed = open_share(create_share(volume.id, can_download=True)["code"])
        link = client.get(f"/api/volumes/{volume.id}/attachments/{att['id']}/download-url", headers=allowed)
        assert link.status_code == 200 and client.get(link.json()["url"]).content == b"hello"
        assert attach(client, allowed, volume.id).status_code == 403

    def test_rename_and_describe(self, client, make_volume, editor, auth):
        volume = make_volume()
        att = attach(client, auth(editor), volume.id).json()
        changed = client.patch(f"/api/volumes/{volume.id}/attachments/{att['id']}",
                               json={"filename": "dir/final.txt", "description": "the data"}, headers=auth(editor))
        assert changed.status_code == 200 and changed.json()["filename"] == "final.txt"
        assert changed.json()["description"] == "the data"

    def test_delete_removes_the_file(self, client, make_volume, editor, auth):
        volume = make_volume()
        att = attach(client, auth(editor), volume.id).json()
        folder = settings.VOLUMES_DIR / volume.slug / "attachments"
        assert len(list(folder.iterdir())) == 1
        assert client.delete(f"/api/volumes/{volume.id}/attachments/{att['id']}", headers=auth(editor)).status_code == 200
        assert list(folder.iterdir()) == []
        assert client.delete(f"/api/volumes/{volume.id}/attachments/{att['id']}", headers=auth(editor)).status_code == 404

    def test_a_staged_upload_is_attached_by_its_id(self, client, make_volume, editor, auth):
        volume = make_volume()
        staged = client.put("/api/uploads/attachment", params={"name": "big.raw"}, content=b"R" * 1000, headers=auth(editor)).json()
        assert (settings.DATA_DIR / ".uploads" / staged["upload_id"]).is_file()
        att = client.post(f"/api/volumes/{volume.id}/attachments", data={"attachment_upload_id": staged["upload_id"]}, headers=auth(editor))
        assert att.status_code == 200 and att.json()["filename"] == "big.raw" and att.json()["size"] == 1000
        assert not (settings.DATA_DIR / ".uploads" / staged["upload_id"]).exists()
        # the id is used up
        assert client.post(f"/api/volumes/{volume.id}/attachments", data={"attachment_upload_id": staged["upload_id"]}, headers=auth(editor)).status_code == 400

    def test_a_file_or_an_upload_id_but_not_both_or_neither(self, client, make_volume, editor, auth):
        volume = make_volume()
        assert client.post(f"/api/volumes/{volume.id}/attachments", headers=auth(editor)).status_code == 400
        assert client.post(f"/api/volumes/{volume.id}/attachments", files={"file": ("a", b"1")},
                           data={"attachment_upload_id": "x"}, headers=auth(editor)).status_code == 400

    def test_not_while_the_volume_is_processing(self, client, make_volume, editor, auth):
        from app.models import AssetStatus
        volume = make_volume(status=AssetStatus.processing)
        assert attach(client, auth(editor), volume.id).status_code == 409

    def test_deleting_the_volume_deletes_all_of_it(self, client, make_volume, editor, auth, db):
        volume = make_volume()
        attach(client, auth(editor), volume.id)
        client.put(f"/api/volumes/{volume.id}/report", json={"markdown": "x"}, headers=auth(editor))
        client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("a.png", png())}, headers=auth(editor))
        assert client.delete(f"/api/volumes/{volume.id}", headers=auth(editor)).status_code == 200
        assert not (settings.VOLUMES_DIR / volume.slug).exists()
        for model in (VolumeAttachment, VolumeReport, ReportImage):
            assert db.query(model).count() == 0

    def test_the_storage_overview_counts_them_apart(self, client, make_volume, admin, editor, auth):
        volume = make_volume()
        attach(client, auth(editor), volume.id, "a.bin", b"x" * 500)
        client.post(f"/api/volumes/{volume.id}/report/images", files={"file": ("a.png", png())}, headers=auth(editor))
        report = client.get(f"/api/storage/volume/{volume.id}", headers=auth(editor)).json()
        labels = {part["label"]: part["apparent_bytes"] for part in report["entries"]}
        assert labels["Attachments"] == 500 and "Report text and images" in labels
        assert not [k for k in labels if k == "Other files"]


class TestMovingTheTextOutOfTheDatabase:
    """Databases made by older versions hold the text in volumereport.markdown (see database._migrate_report_text)."""

    def _old_database(self, tmp_path, rows):
        from sqlalchemy import create_engine
        engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE volume (id INTEGER PRIMARY KEY, slug TEXT)")
            conn.exec_driver_sql("INSERT INTO volume (id, slug) VALUES (1, 'vol-a'), (2, 'vol-b'), (3, 'vol-c')")
            conn.exec_driver_sql(
                "CREATE TABLE volumereport (id INTEGER PRIMARY KEY, volume_id INTEGER NOT NULL UNIQUE, "
                "markdown VARCHAR NOT NULL, updated_by INTEGER, updated_at DATETIME)")
            conn.exec_driver_sql("CREATE INDEX ix_volumereport_volume_id ON volumereport (volume_id)")
            for volume_id, text in rows:
                conn.exec_driver_sql("INSERT INTO volumereport (volume_id, markdown, updated_by) VALUES (?, ?, 7)",
                                     (volume_id, text))
        return engine

    def _columns(self, engine):
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(volumereport)")}

    def test_texts_become_files_and_the_column_goes(self, tmp_path, monkeypatch):
        from app import database
        engine = self._old_database(tmp_path, [(1, "# A\nčau\r\n"), (2, ""), (99, "orphan")])
        monkeypatch.setattr(database, "engine", engine)
        database._migrate_report_text()
        assert (settings.VOLUMES_DIR / "vol-a" / "report" / "report.md").read_bytes() == "# A\nčau\r\n".encode()
        assert not (settings.VOLUMES_DIR / "vol-b" / "report").exists()  # an empty text makes no file
        assert self._columns(engine) == {"id", "volume_id", "updated_by", "updated_at"}
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT count(*), max(updated_by) FROM volumereport").one() == (3, 7)

    def test_is_idempotent_and_keeps_an_existing_file(self, tmp_path, monkeypatch):
        from app import database, report_store
        engine = self._old_database(tmp_path, [(1, "from the database")])
        monkeypatch.setattr(database, "engine", engine)
        report_store.write_text("vol-a", "already on disk")
        database._migrate_report_text()
        database._migrate_report_text()
        assert report_store.read_text("vol-a") == "already on disk"

    def test_a_failure_leaves_the_database_untouched(self, tmp_path, monkeypatch):
        import pytest
        from app import database, report_store
        engine = self._old_database(tmp_path, [(1, "keep me")])
        monkeypatch.setattr(database, "engine", engine)
        def boom(slug, text):
            raise OSError("read-only file system")
        monkeypatch.setattr(report_store, "write_text", boom)
        with pytest.raises(RuntimeError, match="Nothing was removed"):
            database._migrate_report_text()
        assert "markdown" in self._columns(engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT markdown FROM volumereport").scalar() == "keep me"
