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
        assert len(rows) == 1 and rows[0].markdown == "two"

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
        assert labels["Attachments"] == 500 and "Report images" in labels
        assert not [k for k in labels if k == "Other files"]
