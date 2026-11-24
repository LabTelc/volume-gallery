"""Small pure helpers: config check, slugs, path validation, invite tokens, the volume.json level cut."""
import json
from datetime import timezone

import pytest
from fastapi import HTTPException

from app import invites
from app.config import check_secret_key, settings
from app.routers.volumes import _validate_rel_path, truncate_sharded_volume_json
from app.utils import NEVER_EXPIRES, NEVER_EXPIRES_THRESHOLD, SLUG_RE, get_utc_now


# --------------------------------------------------------------------------- config

class TestCheckSecretKey:
    @pytest.mark.parametrize("key", ["", "short", "x" * 31, "   ", "change_me_insecure_default", "dev-secret-change-me", "your-secret-key"])
    def test_refuses_missing_short_or_placeholder_keys(self, monkeypatch, key):
        monkeypatch.setattr(settings, "SECRET_KEY", key)
        with pytest.raises(RuntimeError, match="VG_SECRET_KEY"):
            check_secret_key()

    def test_accepts_a_real_key(self, monkeypatch):
        monkeypatch.setattr(settings, "SECRET_KEY", "k" * 32)
        check_secret_key()

    def test_the_test_suite_itself_uses_a_valid_key(self):
        check_secret_key()


# --------------------------------------------------------------------------- utils

class TestSlug:
    @pytest.mark.parametrize("slug", ["ab", "a1", "my-volume", "my_volume", "0abc", "a" * 63])
    def test_valid(self, slug):
        assert SLUG_RE.fullmatch(slug)

    @pytest.mark.parametrize("slug", ["", "a", "A1", "-ab", "_ab", "a b", "a/b", "a.b", "../x", "a" * 64, "ab\n", "ab\x00", "é1"])
    def test_invalid(self, slug):
        assert SLUG_RE.fullmatch(slug) is None


def test_get_utc_now_is_timezone_aware():
    assert get_utc_now().tzinfo is not None
    assert get_utc_now().utcoffset().total_seconds() == 0


def test_never_expires_sentinel():
    assert NEVER_EXPIRES >= NEVER_EXPIRES_THRESHOLD
    assert NEVER_EXPIRES.tzinfo == timezone.utc
    assert get_utc_now() < NEVER_EXPIRES_THRESHOLD


# --------------------------------------------------------------------------- invites

class TestInvites:
    def test_tokens_are_long_and_unique(self):
        tokens = {invites.new_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(t) >= 40 for t in tokens)  # 256 bits, url-safe base64

    def test_only_a_hash_is_stored(self):
        token = invites.new_token()
        assert invites.hash_token(token) != token
        assert invites.hash_token(token) == invites.hash_token(token)
        assert len(invites.hash_token(token)) == 64

    def test_aware_adds_utc_to_naive_datetimes(self):
        naive = get_utc_now().replace(tzinfo=None)
        assert invites.aware(naive).tzinfo == timezone.utc
        assert invites.aware(get_utc_now()).tzinfo is not None

    def test_link_expiry_follows_the_setting(self, monkeypatch):
        monkeypatch.setattr(settings, "PASSWORD_LINK_HOURS", 2)
        delta = invites.link_expiry() - get_utc_now()
        assert 1.9 * 3600 < delta.total_seconds() <= 2 * 3600


# --------------------------------------------------------------------------- dataset path validation

class TestValidateRelPath:
    @pytest.mark.parametrize("path", ["volume.json", "lod0.bin", "lod12_index.json", "a/b/c.bin"])
    def test_accepts_plain_relative_paths(self, path):
        assert _validate_rel_path(path) == path

    @pytest.mark.parametrize("path", [
        "", "/etc/passwd", "../secret", "a/../b", "a/./b", ".", "..", "a//b", "a/", "a\\b", "..\\x",
        "a\x00b", "a%2e%2e/b", "a%00", "a?x=1", "a#frag",
    ])
    def test_rejects_traversal_and_url_metacharacters(self, path):
        with pytest.raises(HTTPException) as exc:
            _validate_rel_path(path)
        assert exc.value.status_code == 400


# --------------------------------------------------------------------------- level cut for share links

def _meta(levels=3, spacing=(1.0, 2.0, 3.0)):
    dims = [[128 >> i, 128 >> i, 64 >> i] for i in range(levels)]
    return json.dumps({
        "voxelSpacing": list(spacing),
        "originalDimensions": dims[0],
        "maxLod": levels - 1,
        "brickLayout": "tiled66",
        "levels": [{"lod": i, "dimensions": dims[i]} for i in range(levels)],
    }).encode()


class TestTruncateShardedVolumeJson:
    def test_nothing_to_cut_returns_none(self):
        assert truncate_sharded_volume_json(_meta(), 0) is None

    def test_cutting_drops_finer_levels_and_renumbers(self):
        out = json.loads(truncate_sharded_volume_json(_meta(3), 1))
        assert [lvl["lod"] for lvl in out["levels"]] == [0, 1]
        assert out["maxLod"] == 1
        assert out["originalDimensions"] == [64, 64, 32]
        assert out["brickLayout"] == "tiled66"  # untouched

    def test_voxel_spacing_doubles_for_every_halved_axis(self):
        out = json.loads(truncate_sharded_volume_json(_meta(3, (1.0, 2.0, 3.0)), 2))
        assert out["voxelSpacing"] == [4.0, 8.0, 12.0]

    def test_cut_past_the_last_level_returns_none(self):
        assert truncate_sharded_volume_json(_meta(3), 3) is None
        assert truncate_sharded_volume_json(_meta(3), 99) is None

    @pytest.mark.parametrize("raw", [
        b"not json", b"[]", b"{}", b'{"levels": []}', b'{"levels": "x"}',
        json.dumps({"levels": [{"lod": "0", "dimensions": [1, 1, 1]}]}).encode(),
        json.dumps({"levels": [{"lod": 0, "dimensions": [1, 1, 1]}, {"lod": 1, "dimensions": [1, 1]}], "voxelSpacing": [1, 1, 1]}).encode(),
        json.dumps({"levels": [{"lod": 0, "dimensions": [1, 1, 1]}, {"lod": 1, "dimensions": [1, 1, 1]}]}).encode(),
    ])
    def test_malformed_metadata_is_served_unchanged(self, raw):
        assert truncate_sharded_volume_json(raw, 1) is None
