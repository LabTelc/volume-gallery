"""Share links: creating them, opening /s/<code>, and what a guest session may do."""
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import select

from app.config import settings
from app.models import AuditEvent, Mesh, PointCloud, ShareLink, ShareView
from app.security import decode_token
from app.utils import get_utc_now


def token_of(headers):
    return headers["Authorization"].split(" ", 1)[1]


class TestCreate:
    def test_defaults(self, make_volume, create_share):
        volume = make_volume()
        share = create_share(volume.id)
        assert share["asset_type"] == "volume" and share["asset_id"] == volume.id
        assert share["can_download"] is False and share["min_lod"] is None
        assert share["revoked"] is False and share["expired"] is False and share["never_expires"] is False
        assert share["views_count"] == 0
        assert len(share["code"]) == settings.SHARE_CODE_MAX_LENGTH
        assert share["url"].endswith(f"/s/{share['code']}")

    def test_default_lifetime_is_24_hours(self, make_volume, create_share):
        share = create_share(make_volume().id)
        remaining = (datetime.fromisoformat(share["expires_at"]) - get_utc_now()).total_seconds()
        assert 23.9 * 3600 < remaining <= 24 * 3600

    def test_codes_are_random(self, make_volume, create_share):
        volume = make_volume()
        codes = {create_share(volume.id)["code"] for _ in range(5)}
        assert len(codes) == 5

    def test_the_url_uses_the_forwarded_host(self, client, editor, auth, make_volume):
        volume = make_volume()
        response = client.post(
            "/api/shares", json={"asset_type": "volume", "asset_id": volume.id},
            headers={
                **auth(editor), "X-Forwarded-Proto": "https", "X-Forwarded-Host": "gallery.example:8443",
                "Origin": "https://gallery.example:8443",  # (what the browser sends to that address)
            },
        )
        assert response.json()["url"] == f"https://gallery.example:8443/s/{response.json()['code']}"

    def test_suggested_code(self, client, editor, auth):
        body = client.get("/api/shares/suggest", headers=auth(editor)).json()
        assert body["min_length"] == settings.SHARE_CODE_MIN_LENGTH
        assert body["max_length"] == settings.SHARE_CODE_MAX_LENGTH
        assert len(body["suggested_code"]) == settings.SHARE_CODE_MAX_LENGTH

    @pytest.mark.parametrize("code", ["my-demo-link", "A1_b-c2d3", "12345678", "x" * 24])
    def test_custom_codes(self, make_volume, create_share, code):
        assert create_share(make_volume().id, code=code)["code"] == code

    def test_custom_code_is_trimmed(self, make_volume, create_share):
        assert create_share(make_volume().id, code="  my-demo-link  ")["code"] == "my-demo-link"

    def test_blank_custom_code_means_generate_one(self, make_volume, create_share):
        assert len(create_share(make_volume().id, code="   ")["code"]) == settings.SHARE_CODE_MAX_LENGTH

    @pytest.mark.parametrize("code", ["short", "x" * 25, "has space!", "-leading", "_leading", "dot.dot.dot", "slash/slash", "emoji-😀-code", "semi;colon1"])
    def test_bad_custom_codes(self, client, editor, auth, make_volume, code):
        volume = make_volume()
        response = client.post("/api/shares", json={"asset_type": "volume", "asset_id": volume.id, "code": code}, headers=auth(editor))
        assert response.status_code == 400, code

    def test_a_code_cannot_be_used_twice(self, make_volume, create_share):
        volume = make_volume()
        create_share(volume.id, code="taken-code")
        create_share(volume.id, code="taken-code", expect=409)

    def test_unknown_asset_type(self, make_volume, create_share):
        create_share(make_volume().id, asset_type="document", expect=400)

    def test_unknown_asset(self, create_share):
        create_share(9999, expect=404)
        create_share(9999, asset_type="mesh", expect=404)
        create_share(9999, asset_type="pointcloud", expect=404)

    def test_meshes_and_point_clouds_can_be_shared_too(self, db, create_share):
        mesh, pc = Mesh(slug="m1", title="m"), PointCloud(slug="p1", title="p")
        db.add(mesh)
        db.add(pc)
        db.commit()
        assert create_share(mesh.id, asset_type="mesh")["asset_type"] == "mesh"
        assert create_share(pc.id, asset_type="pointcloud")["asset_type"] == "pointcloud"

    def test_never_expires(self, make_volume, create_share):
        share = create_share(make_volume().id, expires_in_minutes=0)
        assert share["never_expires"] is True and share["expired"] is False

    @pytest.mark.parametrize("minutes,ok", [(1, True), (525600, True), (-1, False), (525601, False), (10**9, False)])
    def test_expiry_bounds(self, client, editor, auth, make_volume, minutes, ok):
        volume = make_volume()
        response = client.post(
            "/api/shares", json={"asset_type": "volume", "asset_id": volume.id, "expires_in_minutes": minutes}, headers=auth(editor)
        )
        assert (response.status_code == 200) is ok

    def test_view_query_is_cleaned_of_leading_separators(self, make_volume, create_share):
        assert create_share(make_volume().id, view_query="?&x=1&y=2")["view_query"] == "x=1&y=2"

    def test_options_are_stored(self, make_volume, create_share):
        share = create_share(make_volume().id, can_download=True, min_lod=2)
        assert share["can_download"] is True and share["min_lod"] == 2

    def test_is_audited(self, db, make_volume, create_share, editor):
        share = create_share(make_volume().id, can_download=True)
        event = db.exec(select(AuditEvent).where(AuditEvent.action == "share.create")).one()
        assert event.actor_name == editor.username and event.target_name == share["code"]

    def test_readers_cannot_create(self, client, reader, auth, make_volume):
        volume = make_volume()
        assert client.post("/api/shares", json={"asset_type": "volume", "asset_id": volume.id}, headers=auth(reader)).status_code == 403


class TestListAndDelete:
    def test_listing_filters_by_asset(self, client, editor, auth, make_volume, create_share):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        sa, sb = create_share(a.id), create_share(b.id)
        everything = client.get("/api/shares", headers=auth(editor)).json()
        assert {s["code"] for s in everything} == {sa["code"], sb["code"]}
        only_a = client.get("/api/shares", params={"asset_type": "volume", "asset_id": a.id}, headers=auth(editor)).json()
        assert [s["code"] for s in only_a] == [sa["code"]]

    def test_delete_kills_the_link_and_open_sessions(self, client, editor, auth, make_volume, create_share, open_share, db):
        volume = make_volume()
        share = create_share(volume.id)
        guest = open_share(share["code"])
        assert client.get(f"/api/volumes/{volume.id}", headers=guest).status_code == 200

        assert client.delete(f"/api/shares/{share['id']}", headers=auth(editor)).status_code == 200

        assert client.get(f"/s/{share['code']}", follow_redirects=False).status_code == 404
        assert client.get(f"/api/volumes/{volume.id}", headers=guest).status_code == 401
        assert db.exec(select(ShareLink)).all() == [] and db.exec(select(ShareView)).all() == []
        assert [e.action for e in db.exec(select(AuditEvent).order_by(AuditEvent.id)).all()][-1] == "share.delete"

    def test_unknown_share(self, client, editor, auth):
        assert client.delete("/api/shares/9999", headers=auth(editor)).status_code == 404


class TestOpeningALink:
    def test_redirects_to_the_viewer_with_a_session_token_in_the_fragment(self, client, make_volume, create_share):
        volume = make_volume()
        share = create_share(volume.id)
        response = client.get(f"/s/{share['code']}", follow_redirects=False)
        assert response.status_code == 302
        target = urlparse(response.headers["location"])
        assert target.path == "/viewers/volume.html" and parse_qs(target.query) == {"id": [str(volume.id)]}
        fragment = parse_qs(target.fragment)
        assert fragment["vg_sc"] == [share["code"]]
        claims = decode_token(fragment["vg_st"][0])
        assert (claims["type"], claims["sid"], claims["kind"], claims["aid"]) == ("share", share["id"], "volume", volume.id)

    def test_the_token_is_never_in_the_query_string(self, client, make_volume, create_share):
        share = create_share(make_volume().id)
        location = client.get(f"/s/{share['code']}", follow_redirects=False).headers["location"]
        assert "vg_st" not in urlparse(location).query  # the fragment is not sent to servers or logged

    def test_view_query_is_appended(self, client, make_volume, create_share):
        volume = make_volume()
        share = create_share(volume.id, view_query="slice=xy&z=12")
        location = client.get(f"/s/{share['code']}", follow_redirects=False).headers["location"]
        assert parse_qs(urlparse(location).query) == {"id": [str(volume.id)], "slice": ["xy"], "z": ["12"]}

    @pytest.mark.parametrize("kind,viewer", [("mesh", "/viewers/mesh.html"), ("pointcloud", "/viewers/pointcloud.html")])
    def test_each_asset_type_has_its_viewer(self, client, db, create_share, kind, viewer):
        asset = Mesh(slug="m1", title="m") if kind == "mesh" else PointCloud(slug="p1", title="p")
        db.add(asset)
        db.commit()
        share = create_share(asset.id, asset_type=kind)
        location = client.get(f"/s/{share['code']}", follow_redirects=False).headers["location"]
        assert urlparse(location).path == viewer

    def test_each_opening_is_counted(self, client, editor, auth, make_volume, create_share, db):
        share = create_share(make_volume().id)
        for _ in range(3):
            client.get(f"/s/{share['code']}", follow_redirects=False)
        listed = client.get("/api/shares", headers=auth(editor)).json()
        assert listed[0]["views_count"] == 3
        assert len(db.exec(select(ShareView)).all()) == 3

    @pytest.mark.parametrize("how", ["unknown", "expired", "revoked"])
    def test_dead_links_all_look_the_same(self, client, db, make_volume, create_share, how):
        share = create_share(make_volume().id)
        code = "doesnotexist" if how == "unknown" else share["code"]
        if how != "unknown":
            row = db.get(ShareLink, share["id"])
            if how == "expired":
                row.expires_at = get_utc_now() - timedelta(seconds=1)
            else:
                row.revoked = True
            db.add(row)
            db.commit()
        response = client.get(f"/s/{code}", follow_redirects=False)
        assert response.status_code == 404
        assert "Link unavailable" in response.text
        assert "location" not in response.headers

    def test_dead_links_are_not_counted(self, client, db, make_volume, create_share):
        share = create_share(make_volume().id)
        row = db.get(ShareLink, share["id"])
        row.revoked = True
        db.add(row)
        db.commit()
        client.get(f"/s/{share['code']}", follow_redirects=False)
        assert db.exec(select(ShareView)).all() == []

    def test_the_session_ends_when_the_link_does(self, client, open_share, make_volume, create_share):
        share = create_share(make_volume().id, expires_in_minutes=60)
        remaining = decode_token(token_of(open_share(share["code"])))["exp"] - time.time()
        assert 59 * 60 < remaining <= 60 * 60 + 5

    def test_a_never_expiring_link_hands_out_30_day_sessions(self, open_share, make_volume, create_share):
        share = create_share(make_volume().id, expires_in_minutes=0)
        remaining = decode_token(token_of(open_share(share["code"])))["exp"] - time.time()
        assert 29 * 86400 < remaining <= 30 * 86400 + 5


class TestGuestSessions:
    def test_a_guest_sees_only_the_shared_volume(self, client, make_volume, create_share, open_share):
        shared, other = make_volume("vol-shared"), make_volume("vol-other")
        guest = open_share(create_share(shared.id)["code"])
        listed = client.get("/api/volumes", headers=guest).json()
        assert [v["id"] for v in listed] == [shared.id]
        assert client.get(f"/api/volumes/{shared.id}", headers=guest).status_code == 200
        assert client.get(f"/api/volumes/{other.id}", headers=guest).status_code == 404

    @pytest.mark.parametrize("suffix", ["status", "volume-data-access-url", "slice-access-url"])
    def test_other_volumes_are_closed_on_every_endpoint(self, client, make_volume, create_share, open_share, suffix):
        shared, other = make_volume("vol-shared"), make_volume("vol-other")
        guest = open_share(create_share(shared.id)["code"])
        assert client.get(f"/api/volumes/{shared.id}/{suffix}", headers=guest).status_code == 200
        assert client.get(f"/api/volumes/{other.id}/{suffix}", headers=guest).status_code == 404

    def test_a_volume_share_gives_no_access_to_meshes_or_point_clouds(self, client, db, make_volume, create_share, open_share):
        db.add(Mesh(slug="loose", title="m"))
        db.add(PointCloud(slug="cloud", title="p"))
        db.commit()
        guest = open_share(create_share(make_volume().id)["code"])
        assert client.get("/api/meshes", headers=guest).json() == []
        assert client.get("/api/pointclouds", headers=guest).json() == []

    def test_a_mesh_share_gives_no_access_to_volumes(self, client, db, make_volume, create_share, open_share):
        make_volume()
        mesh = Mesh(slug="loose", title="m")
        db.add(mesh)
        db.commit()
        guest = open_share(create_share(mesh.id, asset_type="mesh")["code"])
        assert client.get("/api/volumes", headers=guest).json() == []
        assert [m["id"] for m in client.get("/api/meshes", headers=guest).json()] == [mesh.id]

    def test_downloads_need_the_download_flag(self, client, make_volume, create_share, open_share):
        volume = make_volume(has_mesh=True)
        view_only = open_share(create_share(volume.id)["code"])
        may_download = open_share(create_share(volume.id, can_download=True)["code"])
        assert client.get(f"/api/volumes/{volume.id}/mesh-access-url", headers=view_only).status_code == 403
        allowed = client.get(f"/api/volumes/{volume.id}/mesh-access-url", headers=may_download)
        assert allowed.status_code == 200 and "/mesh-file/" in allowed.json()["url"]

    def test_the_session_dies_with_an_expired_link(self, client, db, make_volume, create_share, open_share):
        volume = make_volume()
        share = create_share(volume.id)
        guest = open_share(share["code"])
        row = db.get(ShareLink, share["id"])
        row.expires_at = get_utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        response = client.get(f"/api/volumes/{volume.id}", headers=guest)
        assert response.status_code == 401 and "expired" in response.json()["detail"].lower()

    def test_the_session_dies_with_a_revoked_link(self, client, db, make_volume, create_share, open_share):
        volume = make_volume()
        share = create_share(volume.id)
        guest = open_share(share["code"])
        row = db.get(ShareLink, share["id"])
        row.revoked = True
        db.add(row)
        db.commit()
        assert client.get(f"/api/volumes/{volume.id}", headers=guest).status_code == 401

    def test_a_guest_token_cannot_be_retargeted(self, client, make_volume, create_share, open_share):
        # the scope lives in the database row of the share, not in claims a client could rewrite
        shared, other = make_volume("vol-shared"), make_volume("vol-other")
        guest = open_share(create_share(shared.id)["code"])
        claims = decode_token(token_of(guest))
        assert claims["aid"] == shared.id
        assert client.get(f"/api/volumes/{other.id}", headers=guest).status_code == 404


class TestResolutionCap:
    def minted_level(self, client, headers, volume, **params):
        url = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", params=params, headers=headers).json()["url"]
        return decode_token(urlparse(url).path.rsplit("/", 1)[-1]).get("min_lod")

    def test_the_cap_is_written_into_the_file_token(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=4)
        guest = open_share(create_share(volume.id, min_lod=2)["code"])
        assert self.minted_level(client, guest, volume) == 2

    def test_a_guest_cannot_ask_for_a_finer_level_than_the_cap(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=4)
        guest = open_share(create_share(volume.id, min_lod=2)["code"])
        assert self.minted_level(client, guest, volume, min_level=0) == 2
        assert self.minted_level(client, guest, volume, min_level=1) == 2

    def test_a_guest_may_ask_for_a_coarser_level(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=5)
        guest = open_share(create_share(volume.id, min_lod=1)["code"])
        assert self.minted_level(client, guest, volume, min_level=3) == 3

    def test_a_cap_past_the_coarsest_level_means_the_coarsest(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=3)
        guest = open_share(create_share(volume.id, min_lod=99)["code"])
        assert self.minted_level(client, guest, volume) == 2

    def test_no_cap_no_claim(self, client, make_volume, create_share, open_share):
        volume = make_volume()
        guest = open_share(create_share(volume.id)["code"])
        assert self.minted_level(client, guest, volume) is None

    def test_members_are_not_capped(self, client, editor, auth, make_volume):
        volume = make_volume()
        assert self.minted_level(client, auth(editor), volume) is None
