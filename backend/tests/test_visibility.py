"""Who can see which asset: volumes, meshes and point clouds, grants, and what a not-ready asset serves."""
import pytest
from sqlmodel import select

from app.config import settings
from app.models import AssetStatus, AuditEvent, Mesh, MeshAccess, PointCloud, PointCloudAccess, VolumeAccess

PER_VOLUME_GETS = [
    "/api/volumes/{id}",
    "/api/volumes/{id}/status",
    "/api/volumes/{id}/thumbnail",
    "/api/volumes/{id}/histogram",
    "/api/volumes/{id}/volume-data-access-url",
    "/api/volumes/{id}/slice-access-url",
    "/api/volumes/{id}/mesh-access-url",
    "/api/volumes/{id}/mesh-view-url",
]


def ids(response):
    return sorted(item["id"] for item in response.json())


class TestVolumeVisibility:
    def test_a_reader_sees_nothing_without_a_grant(self, client, reader, auth, make_volume):
        make_volume("vol-a")
        make_volume("vol-b")
        assert client.get("/api/volumes", headers=auth(reader)).json() == []

    @pytest.mark.parametrize("path", PER_VOLUME_GETS)
    def test_every_per_volume_endpoint_hides_an_ungranted_volume(self, client, reader, auth, make_volume, path):
        volume = make_volume("vol-a")
        # 404 (not 403): the API does not even confirm that the volume exists
        assert client.get(path.format(id=volume.id), headers=auth(reader)).status_code == 404

    def test_a_missing_volume_looks_the_same_as_a_hidden_one(self, client, reader, auth, make_volume):
        hidden = make_volume("vol-a")
        a = client.get(f"/api/volumes/{hidden.id}", headers=auth(reader))
        b = client.get("/api/volumes/9999", headers=auth(reader))
        assert a.status_code == b.status_code == 404
        assert a.json() == b.json()

    def test_a_grant_makes_exactly_that_volume_visible(self, client, editor, reader, auth, make_volume):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        assert client.post(f"/api/volumes/{a.id}/access", json={"user_id": reader.id}, headers=auth(editor)).status_code == 200
        assert ids(client.get("/api/volumes", headers=auth(reader))) == [a.id]
        assert client.get(f"/api/volumes/{a.id}", headers=auth(reader)).status_code == 200
        assert client.get(f"/api/volumes/{b.id}", headers=auth(reader)).status_code == 404

    def test_grants_are_per_user(self, client, editor, reader, reader2, auth, make_volume):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert client.get(f"/api/volumes/{volume.id}", headers=auth(reader2)).status_code == 404

    def test_revoking_hides_the_volume_again(self, client, editor, reader, auth, make_volume):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert client.delete(f"/api/volumes/{volume.id}/access/{reader.id}", headers=auth(editor)).status_code == 200
        assert client.get(f"/api/volumes/{volume.id}", headers=auth(reader)).status_code == 404
        assert client.get("/api/volumes", headers=auth(reader)).json() == []

    def test_granting_twice_is_harmless(self, client, editor, reader, auth, make_volume, db):
        volume = make_volume()
        for _ in range(2):
            assert client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor)).status_code == 200
        assert len(db.exec(select(VolumeAccess)).all()) == 1
        assert len(db.exec(select(AuditEvent).where(AuditEvent.action == "access.grant")).all()) == 1

    def test_revoking_something_that_was_never_granted_is_harmless(self, client, editor, reader, auth, make_volume):
        volume = make_volume()
        assert client.delete(f"/api/volumes/{volume.id}/access/{reader.id}", headers=auth(editor)).status_code == 200

    def test_grant_for_missing_user_or_volume(self, client, editor, reader, auth, make_volume):
        volume = make_volume()
        assert client.post(f"/api/volumes/{volume.id}/access", json={"user_id": 9999}, headers=auth(editor)).status_code == 404
        assert client.post("/api/volumes/9999/access", json={"user_id": reader.id}, headers=auth(editor)).status_code == 404

    def test_listing_who_has_access(self, client, editor, reader, reader2, auth, make_volume):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        listed = client.get(f"/api/volumes/{volume.id}/access", headers=auth(editor)).json()
        assert [u["username"] for u in listed] == [reader.username]

    def test_grants_and_revocations_are_audited(self, client, editor, reader, auth, make_volume, db):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        client.delete(f"/api/volumes/{volume.id}/access/{reader.id}", headers=auth(editor))
        actions = [e.action for e in db.exec(select(AuditEvent).order_by(AuditEvent.id)).all()]
        assert actions == ["access.grant", "access.revoke"]

    def test_editors_and_admins_see_everything_including_unfinished_volumes(self, client, admin, editor, auth, make_volume):
        done = make_volume("vol-done")
        busy = make_volume("vol-busy", status=AssetStatus.processing, with_data=False)
        for user in (admin, editor):
            assert ids(client.get("/api/volumes", headers=auth(user))) == sorted([done.id, busy.id])

    def test_deleting_a_volume_removes_its_grants_and_files(self, client, editor, reader, auth, make_volume, db):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        folder = settings.VOLUMES_DIR / volume.slug
        assert folder.is_dir()
        assert client.delete(f"/api/volumes/{volume.id}", headers=auth(editor)).status_code == 200
        assert not folder.exists()
        assert db.exec(select(VolumeAccess)).all() == []
        assert client.get(f"/api/volumes/{volume.id}", headers=auth(editor)).status_code == 404


class TestVolumeDataAccessUrl:
    def test_ready_volume(self, client, reader, editor, auth, make_volume):
        volume = make_volume()
        client.post(f"/api/volumes/{volume.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        body = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=auth(reader)).json()
        assert f"/api/volumes/{volume.id}/dataset/" in body["url"]
        assert body["expires_in_minutes"] == settings.FILE_TOKEN_EXPIRE_MINUTES
        assert len(body["version"]) == 16

    def test_the_version_is_stable_for_the_same_volume(self, client, editor, auth, make_volume):
        volume = make_volume()
        url = f"/api/volumes/{volume.id}/volume-data-access-url"
        assert client.get(url, headers=auth(editor)).json()["version"] == client.get(url, headers=auth(editor)).json()["version"]

    def test_versions_differ_between_volumes(self, client, editor, auth, make_volume):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        va = client.get(f"/api/volumes/{a.id}/volume-data-access-url", headers=auth(editor)).json()["version"]
        vb = client.get(f"/api/volumes/{b.id}/volume-data-access-url", headers=auth(editor)).json()["version"]
        assert va != vb

    def test_a_volume_that_is_still_processing(self, client, editor, auth, make_volume):
        volume = make_volume(status=AssetStatus.processing, with_data=False)
        response = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=auth(editor))
        assert response.status_code == 409 and "processing" in response.json()["detail"]

    def test_a_volume_that_failed(self, client, editor, auth, make_volume):
        volume = make_volume(status=AssetStatus.failed, with_data=False)
        response = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=auth(editor))
        assert response.status_code == 409 and "failed" in response.json()["detail"]

    def test_ready_in_the_database_but_no_files_on_disk(self, client, editor, auth, make_volume):
        volume = make_volume(with_data=False)
        assert client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=auth(editor)).status_code == 404

    def test_min_level_must_be_inside_the_pyramid(self, client, editor, auth, make_volume):
        volume = make_volume(levels=3)  # levels 0, 1, 2 (maxLod = 2)
        url = f"/api/volumes/{volume.id}/volume-data-access-url"
        assert client.get(url, params={"min_level": 0}, headers=auth(editor)).status_code == 200
        assert client.get(url, params={"min_level": 1}, headers=auth(editor)).status_code == 200
        assert client.get(url, params={"min_level": 3}, headers=auth(editor)).status_code == 400
        assert client.get(url, params={"min_level": -1}, headers=auth(editor)).status_code == 422

    def test_the_coarsest_level_is_a_valid_min_level(self, client, editor, auth, make_volume):
        volume = make_volume(levels=3)
        response = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", params={"min_level": 2}, headers=auth(editor))
        assert response.status_code == 200


class TestStatusEndpoint:
    def test_reports_the_log_lines(self, client, editor, auth, make_volume):
        volume = make_volume(status=AssetStatus.processing, status_log="12:00:00  one\n12:00:01  two\n")
        body = client.get(f"/api/volumes/{volume.id}/status", headers=auth(editor)).json()
        assert body["status"] == "processing"
        assert body["status_log"] == ["12:00:00  one", "12:00:01  two"]


# --------------------------------------------------------------------------- meshes and point clouds

class TestMeshVisibility:
    def make(self, db, slug="mesh-a", **kw):
        mesh = Mesh(slug=slug, title=slug, status=AssetStatus.ready, **kw)
        db.add(mesh)
        db.commit()
        db.refresh(mesh)
        return mesh

    def test_readers_need_a_grant(self, client, db, editor, reader, auth):
        mesh = self.make(db)
        assert client.get("/api/meshes", headers=auth(reader)).json() == []
        assert client.get(f"/api/meshes/{mesh.id}", headers=auth(reader)).status_code == 404
        client.post(f"/api/meshes/{mesh.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert ids(client.get("/api/meshes", headers=auth(reader))) == [mesh.id]
        assert client.get(f"/api/meshes/{mesh.id}", headers=auth(reader)).status_code == 200

    def test_download_permission_is_a_separate_flag(self, client, db, editor, reader, auth):
        mesh = self.make(db)
        client.post(f"/api/meshes/{mesh.id}/access", json={"user_id": reader.id, "can_download": False}, headers=auth(editor))
        assert client.get(f"/api/meshes/{mesh.id}", headers=auth(reader)).json()["can_download"] is False
        assert client.get(f"/api/meshes/{mesh.id}/download-access-url", headers=auth(reader)).status_code in (403, 404)
        client.post(f"/api/meshes/{mesh.id}/access", json={"user_id": reader.id, "can_download": True}, headers=auth(editor))
        assert client.get(f"/api/meshes/{mesh.id}", headers=auth(reader)).json()["can_download"] is True
        assert len(db.exec(select(MeshAccess)).all()) == 1  # updated, not duplicated

    def test_a_mesh_attached_to_a_volume_is_not_listed_on_its_own(self, client, db, admin, auth, make_volume):
        volume = make_volume()
        attached = self.make(db, "attached", volume_id=volume.id)
        loose = self.make(db, "loose")
        assert ids(client.get("/api/meshes", headers=auth(admin))) == [loose.id]
        assert client.get(f"/api/meshes/{attached.id}", headers=auth(admin)).status_code == 404

    def test_access_cannot_be_granted_on_an_attached_mesh(self, client, db, editor, reader, auth, make_volume):
        volume = make_volume()
        attached = self.make(db, "attached", volume_id=volume.id)
        response = client.post(f"/api/meshes/{attached.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert response.status_code in (400, 404)

    def test_editors_see_all_loose_meshes(self, client, db, editor, auth):
        a, b = self.make(db, "mesh-a"), self.make(db, "mesh-b")
        assert ids(client.get("/api/meshes", headers=auth(editor))) == sorted([a.id, b.id])


class TestPointCloudVisibility:
    def make(self, db, slug="pc-a"):
        pc = PointCloud(slug=slug, title=slug, status=AssetStatus.ready)
        db.add(pc)
        db.commit()
        db.refresh(pc)
        return pc

    def test_readers_need_a_grant(self, client, db, editor, reader, auth):
        pc = self.make(db)
        assert client.get("/api/pointclouds", headers=auth(reader)).json() == []
        assert client.get(f"/api/pointclouds/{pc.id}", headers=auth(reader)).status_code == 404
        assert client.get(f"/api/pointclouds/{pc.id}/octree-access-url", headers=auth(reader)).status_code == 404
        client.post(f"/api/pointclouds/{pc.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert ids(client.get("/api/pointclouds", headers=auth(reader))) == [pc.id]
        assert len(db.exec(select(PointCloudAccess)).all()) == 1

    def test_one_readers_grant_is_not_anothers(self, client, db, editor, reader, reader2, auth):
        pc = self.make(db)
        client.post(f"/api/pointclouds/{pc.id}/access", json={"user_id": reader.id}, headers=auth(editor))
        assert client.get(f"/api/pointclouds/{pc.id}", headers=auth(reader2)).status_code == 404
