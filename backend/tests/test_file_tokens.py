"""The signed dataset URLs (kiln-render fetches bricks without an Authorization header, so the token is
in the path): what a token opens, what it does not, and when it stops working."""
import json
import time
from datetime import timedelta
from urllib.parse import urlparse

import pytest
import jwt

from app.config import settings
from app.models import ShareLink, VolumeAccess
from app.routers import volumes as volumes_router
from app.security import create_access_token, create_file_token, decode_token
from app.utils import get_utc_now


def mint(client, headers, volume, **params):
    """The token part of the dataset URL for `volume`."""
    response = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return urlparse(response.json()["url"]).path.rsplit("/", 1)[-1]


def fetch(client, volume_id, token, name="volume.json", **kwargs):
    return client.get(f"/api/volumes/{volume_id}/dataset/{token}/{name}", **kwargs)


class TestServing:
    def test_serves_the_metadata_and_the_bricks(self, client, editor, auth, make_volume):
        volume = make_volume(levels=3)
        token = mint(client, auth(editor), volume)

        meta = fetch(client, volume.id, token)
        assert meta.status_code == 200
        assert len(meta.json()["levels"]) == 3

        assert fetch(client, volume.id, token, "lod0.bin").content == b"LOD0" * 16
        assert fetch(client, volume.id, token, "lod2.bin").content == b"LOD2" * 16
        assert fetch(client, volume.id, token, "lod1_index.json").json() == {"lod": 1}

    def test_content_types(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        assert fetch(client, volume.id, token).headers["content-type"].startswith("application/json")
        assert fetch(client, volume.id, token, "lod0.bin").headers["content-type"] == "application/octet-stream"

    def test_responses_are_cacheable_for_as_long_as_the_token_lives(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        cache = fetch(client, volume.id, token, "lod0.bin").headers["cache-control"]
        assert "immutable" in cache and f"max-age={settings.FILE_TOKEN_EXPIRE_MINUTES * 60}" in cache

    def test_range_requests(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        response = fetch(client, volume.id, token, "lod0.bin", headers={"Range": "bytes=4-7"})
        assert response.status_code == 206
        assert response.content == b"LOD0"

    def test_nginx_offload_hands_the_file_over_without_a_body(self, client, editor, auth, make_volume, monkeypatch):
        monkeypatch.setattr(settings, "SHARD_ACCEL_REDIRECT", True)
        volume = make_volume("vol-a")
        token = mint(client, auth(editor), volume)
        response = fetch(client, volume.id, token, "lod0.bin")
        assert response.status_code == 200 and response.content == b""
        assert response.headers["x-accel-redirect"] == "/_internal_volumes/vol-a/lod0.bin"

    def test_a_capped_metadata_file_is_rewritten_by_python_even_with_the_offload(
        self, client, editor, auth, make_volume, create_share, open_share, monkeypatch
    ):
        monkeypatch.setattr(settings, "SHARD_ACCEL_REDIRECT", True)
        volume = make_volume(levels=3)
        guest = open_share(create_share(volume.id, min_lod=1)["code"])
        response = fetch(client, volume.id, mint(client, guest, volume))
        assert "x-accel-redirect" not in response.headers
        assert len(response.json()["levels"]) == 2


class TestWhatAFileTokenDoesNotOpen:
    def test_another_volume(self, client, editor, auth, make_volume):
        a, b = make_volume("vol-a"), make_volume("vol-b")
        token = mint(client, auth(editor), a)
        assert fetch(client, a.id, token).status_code == 200
        assert fetch(client, b.id, token).status_code == 403  # also once the token is cached

    def test_another_kind_of_token(self, client, editor, auth, make_volume):
        volume = make_volume()
        for kind in ("mesh", "slice", "pointcloud", "volume-mesh-download"):
            token = create_file_token(editor.username, volume.id, kind, user=editor)
            assert fetch(client, volume.id, token).status_code == 403, kind

    def test_a_login_token(self, client, editor, make_volume):
        volume = make_volume()
        token = create_access_token(editor.username, "editor")
        assert fetch(client, volume.id, token).status_code == 403

    def test_garbage_and_tampering(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        i = len(token) - 10  # inside the signature, where every character counts
        tampered = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1:]
        for bad in ("garbage", "a.b.c", tampered, token + "x"):
            assert fetch(client, volume.id, bad).status_code == 403, bad

    def test_an_expired_token(self, client, editor, make_volume):
        volume = make_volume()
        claims = {"sub": "x", "vol": volume.id, "kind": "sharded", "type": "file", "exp": get_utc_now() - timedelta(seconds=5)}
        assert fetch(client, volume.id, jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")).status_code == 403

    def test_a_token_signed_with_another_key(self, client, make_volume):
        volume = make_volume()
        claims = {"sub": "x", "vol": volume.id, "kind": "sharded", "type": "file", "exp": get_utc_now() + timedelta(hours=1)}
        forged = jwt.encode(claims, "some-other-key-that-is-long-enough-0123456789", algorithm="HS256")
        assert fetch(client, volume.id, forged).status_code == 403

    @pytest.mark.parametrize("name", ["gallery.db", "lod0.txt", "volume.json.bak", "LOD0.bin", "lod.bin", "lodx.bin", "lod0_index.json.gz", "_upload.zip"])
    def test_only_kiln_files_are_served(self, client, editor, auth, make_volume, name):
        volume = make_volume()
        (settings.VOLUMES_DIR / volume.slug / name).write_bytes(b"secret")
        assert fetch(client, volume.id, mint(client, auth(editor), volume), name).status_code == 404

    @pytest.mark.parametrize("name", ["..%2fgallery.db", "..%2f..%2fgallery.db", "%2e%2e%2fvol-b%2fvolume.json", "lod0.bin%00.json", "a%5cb"])
    def test_path_traversal_is_refused(self, client, editor, auth, make_volume, name):
        volume = make_volume("vol-a")
        make_volume("vol-b")
        response = fetch(client, volume.id, mint(client, auth(editor), volume), name)
        assert response.status_code in (400, 404)
        assert b"LOD" not in response.content

    def test_a_level_that_does_not_exist(self, client, editor, auth, make_volume):
        volume = make_volume(levels=3)
        assert fetch(client, volume.id, mint(client, auth(editor), volume), "lod9.bin").status_code == 404

    def test_a_volume_whose_files_are_gone(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        for f in (settings.VOLUMES_DIR / volume.slug).iterdir():
            f.unlink()
        assert fetch(client, volume.id, token).status_code == 404


class TestResolutionCapOnTheDataset:
    def guest_token(self, client, volume, create_share, open_share, min_lod):
        guest = open_share(create_share(volume.id, min_lod=min_lod)["code"])
        return mint(client, guest, volume)

    def test_finer_levels_are_refused(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=3)
        token = self.guest_token(client, volume, create_share, open_share, 1)
        assert fetch(client, volume.id, token, "lod0.bin").status_code == 403
        assert fetch(client, volume.id, token, "lod0_index.json").status_code == 403
        assert fetch(client, volume.id, token, "lod1.bin").status_code == 200
        assert fetch(client, volume.id, token, "lod2.bin").status_code == 200

    def test_the_metadata_only_lists_the_allowed_levels(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=3)
        token = self.guest_token(client, volume, create_share, open_share, 1)
        meta = fetch(client, volume.id, token).json()
        assert [lvl["lod"] for lvl in meta["levels"]] == [0, 1]
        assert meta["maxLod"] == 1
        assert meta["originalDimensions"] == [64, 64, 64]
        assert meta["voxelSpacing"] == [2.0, 2.0, 2.0]

    def test_the_cap_is_the_tokens_own_not_the_callers(self, client, make_volume, create_share, open_share):
        # whatever the guest puts in the URL, the claim inside the signed token decides
        volume = make_volume(levels=3)
        token = self.guest_token(client, volume, create_share, open_share, 2)
        assert decode_token(token)["min_lod"] == 2
        assert fetch(client, volume.id, token, "lod1.bin", params={"min_level": 0}).status_code == 403

    def test_the_slice_token_carries_the_cap_too(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=4)
        guest = open_share(create_share(volume.id, min_lod=2)["code"])
        url = client.get(f"/api/volumes/{volume.id}/slice-access-url", headers=guest).json()["url"]
        claims = decode_token(url.rstrip("/").rsplit("/", 1)[-1])
        assert claims["kind"] == "slice" and claims["min_lod"] == 2

    def test_a_cap_past_the_coarsest_level_is_clamped_for_slices(self, client, make_volume, create_share, open_share):
        volume = make_volume(levels=3)
        guest = open_share(create_share(volume.id, min_lod=99)["code"])
        url = client.get(f"/api/volumes/{volume.id}/slice-access-url", headers=guest).json()["url"]
        assert decode_token(url.rstrip("/").rsplit("/", 1)[-1])["min_lod"] == 2


class TestRevocation:
    """A file token remembers where it came from (the share link or the account) and is refused once that is gone."""

    def test_deleting_the_share_stops_its_tokens(self, client, editor, auth, make_volume, create_share, open_share):
        volume = make_volume()
        share = create_share(volume.id)
        token = mint(client, open_share(share["code"]), volume)
        assert client.delete(f"/api/shares/{share['id']}", headers=auth(editor)).status_code == 200
        assert fetch(client, volume.id, token).status_code == 403

    def test_an_expired_share_stops_its_tokens(self, client, db, make_volume, create_share, open_share):
        volume = make_volume()
        share = create_share(volume.id)
        token = mint(client, open_share(share["code"]), volume)
        row = db.get(ShareLink, share["id"])
        row.expires_at = get_utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        assert fetch(client, volume.id, token).status_code == 403

    def test_a_revoked_share_stops_its_tokens(self, client, db, make_volume, create_share, open_share):
        volume = make_volume()
        share = create_share(volume.id)
        token = mint(client, open_share(share["code"]), volume)
        row = db.get(ShareLink, share["id"])
        row.revoked = True
        db.add(row)
        db.commit()
        assert fetch(client, volume.id, token).status_code == 403

    def test_disabling_the_account_stops_its_tokens(self, client, db, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        editor.disabled = True
        db.add(editor)
        db.commit()
        assert fetch(client, volume.id, token).status_code == 403

    def test_deleting_the_account_stops_its_tokens(self, client, db, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        db.delete(editor)
        db.commit()
        assert fetch(client, volume.id, token).status_code == 403

    def test_a_token_stays_valid_while_its_share_is_alive(self, client, make_volume, create_share, open_share):
        volume = make_volume()
        token = mint(client, open_share(create_share(volume.id)["code"]), volume)
        assert fetch(client, volume.id, token).status_code == 200
        assert fetch(client, volume.id, token, "lod0.bin").status_code == 200

    # ---- Known gaps. Each test says what SHOULD happen; they are expected to fail until the code changes. ----

    @pytest.mark.xfail(strict=False, reason=(
        "_check_file_token_cached keeps a verified token for up to VG_FILE_META_CACHE_TTL_SECONDS (60 s), "
        "so a token that was already used keeps working that long after its share is deleted."))
    def test_revocation_applies_immediately_even_to_a_token_that_was_just_used(
        self, client, editor, auth, make_volume, create_share, open_share
    ):
        volume = make_volume()
        share = create_share(volume.id)
        token = mint(client, open_share(share["code"]), volume)
        assert fetch(client, volume.id, token).status_code == 200  # now cached
        client.delete(f"/api/shares/{share['id']}", headers=auth(editor))
        assert fetch(client, volume.id, token).status_code == 403

    @pytest.mark.xfail(strict=False, reason=(
        "deps.file_token_revoked only checks that the account still exists and is enabled; "
        "taking a reader's VolumeAccess grant away does not stop the file tokens they already hold "
        "(up to FILE_TOKEN_EXPIRE_MINUTES, 30 min by default)."))
    def test_revoking_a_grant_stops_the_readers_outstanding_tokens(self, client, db, editor, reader, auth, make_volume):
        volume = make_volume()
        db.add(VolumeAccess(user_id=reader.id, volume_id=volume.id))
        db.commit()
        volumes_router._TOKEN_CACHE.clear()
        token = mint(client, auth(reader), volume)
        client.delete(f"/api/volumes/{volume.id}/access/{reader.id}", headers=auth(editor))
        volumes_router._TOKEN_CACHE.clear()
        assert fetch(client, volume.id, token).status_code == 403


class TestTokenCache:
    def test_a_verified_token_is_cached_briefly(self, client, editor, auth, make_volume):
        volume = make_volume()
        token = mint(client, auth(editor), volume)
        assert token not in volumes_router._TOKEN_CACHE
        fetch(client, volume.id, token)
        assert token in volumes_router._TOKEN_CACHE
        expires_at, payload = volumes_router._TOKEN_CACHE[token]
        assert payload["vol"] == volume.id
        assert expires_at - time.time() <= settings.FILE_META_CACHE_TTL_SECONDS + 1

    def test_failures_are_not_cached(self, client, make_volume):
        volume = make_volume()
        fetch(client, volume.id, "garbage")
        assert "garbage" not in volumes_router._TOKEN_CACHE

    def test_the_cache_is_bounded(self, client, editor, auth, make_volume, monkeypatch):
        monkeypatch.setattr(volumes_router, "_CACHE_MAX_SIZE", 3)
        for i in range(3):
            volumes_router._TOKEN_CACHE[f"filler-{i}"] = (1e12, {})
        volume = make_volume()
        fetch(client, volume.id, mint(client, auth(editor), volume))
        assert len(volumes_router._TOKEN_CACHE) <= 3


def test_a_token_is_json_serialisable_proof_of_what_it_holds(client, editor, auth, make_volume):
    volume = make_volume()
    claims = decode_token(mint(client, auth(editor), volume))
    assert json.dumps(claims)  # plain data
    assert claims["vol"] == volume.id and claims["kind"] == "sharded" and claims["uid"] == editor.id
    assert len(claims["ver"]) == 16
