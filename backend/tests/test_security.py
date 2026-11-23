"""Password hashing and token creation/validation (app/security.py)."""
import base64
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import bcrypt
import pytest
from argon2 import PasswordHasher
from jose import jwt

from app import security
from app.config import settings
from app.security import (
    MAX_PASSWORD_LENGTH, create_access_token, create_file_token, create_share_session_token, decode_token,
    hash_password, needs_rehash, password_fingerprint, validate_new_password, verify_password,
)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# --------------------------------------------------------------------------- passwords

class TestPasswordHashing:
    def test_new_hashes_are_argon2id(self):
        assert hash_password("a-fine-password").startswith("$argon2id$")

    def test_correct_password_verifies_and_wrong_one_does_not(self):
        hashed = hash_password("a-fine-password")
        assert verify_password("a-fine-password", hashed) is True
        assert verify_password("a-fine-passworD", hashed) is False
        assert verify_password("", hashed) is False

    def test_hashes_are_salted(self):
        assert hash_password("same-password") != hash_password("same-password")

    def test_too_long_password_cannot_be_hashed(self):
        with pytest.raises(ValueError):
            hash_password("x" * (MAX_PASSWORD_LENGTH + 1))

    def test_too_long_password_never_verifies(self):
        # protects against CPU/memory DoS through huge payloads: rejected before any hashing
        assert verify_password("x" * (MAX_PASSWORD_LENGTH + 1), hash_password("x")) is False

    def test_longest_allowed_password_works(self):
        password = "y" * MAX_PASSWORD_LENGTH
        assert verify_password(password, hash_password(password)) is True

    @pytest.mark.parametrize("garbage", ["", "not-a-hash", "$argon2id$garbage", "$2b$notbcrypt", "plaintext-password"])
    def test_garbage_hashes_never_verify(self, garbage):
        assert verify_password("plaintext-password", garbage) is False

    def test_unicode_passwords(self):
        password = "pässwörd-日本語-🔑"
        assert verify_password(password, hash_password(password)) is True


class TestLegacyBcrypt:
    def _bcrypt(self, password: bytes) -> str:
        return bcrypt.hashpw(password, bcrypt.gensalt(rounds=4)).decode("ascii")

    def test_legacy_hash_verifies(self):
        hashed = self._bcrypt(b"old-password-1")
        assert verify_password("old-password-1", hashed) is True
        assert verify_password("old-password-2", hashed) is False

    def test_legacy_hash_is_truncated_at_72_bytes_like_old_bcrypt(self):
        long_password = "a" * 100
        hashed = self._bcrypt(long_password.encode()[:72])
        assert verify_password(long_password, hashed) is True

    def test_legacy_hashes_are_flagged_for_rehash(self):
        assert needs_rehash(self._bcrypt(b"old-password-1")) is True


class TestNeedsRehash:
    def test_fresh_hash_is_current(self):
        assert needs_rehash(hash_password("a-fine-password")) is False

    def test_weaker_argon2_parameters_are_flagged(self):
        weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("a-fine-password")
        assert needs_rehash(weak) is True

    def test_unparseable_hash_is_flagged(self):
        assert needs_rehash("$argon2id$nonsense") is True
        assert needs_rehash("") is True


class TestValidateNewPassword:
    def test_minimum_length(self, monkeypatch):
        monkeypatch.setattr(settings, "MIN_PASSWORD_LENGTH", 8)
        validate_new_password("12345678")
        with pytest.raises(ValueError, match="at least 8"):
            validate_new_password("1234567")

    def test_maximum_length(self):
        validate_new_password("z" * MAX_PASSWORD_LENGTH)
        with pytest.raises(ValueError, match="too long"):
            validate_new_password("z" * (MAX_PASSWORD_LENGTH + 1))


class TestPasswordFingerprint:
    def test_is_short_hex_and_stable(self):
        fp = password_fingerprint("$argon2id$abc")
        assert fp == password_fingerprint("$argon2id$abc")
        assert len(fp) == 16 and int(fp, 16) >= 0

    def test_differs_between_hashes(self):
        assert password_fingerprint("hash-one") != password_fingerprint("hash-two")

    def test_does_not_contain_the_hash(self):
        assert "argon2" not in password_fingerprint("$argon2id$abc")


# --------------------------------------------------------------------------- access tokens

class TestAccessToken:
    def test_claims(self):
        payload = decode_token(create_access_token("alice", "admin", pwv="abc123"))
        assert payload["sub"] == "alice"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"
        assert payload["pwv"] == "abc123"

    def test_pwv_claim_is_omitted_when_not_given(self):
        assert "pwv" not in decode_token(create_access_token("alice", "admin"))

    def test_lifetime_follows_the_setting(self):
        payload = decode_token(create_access_token("alice", "admin"))
        remaining = payload["exp"] - time.time()
        assert settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 - 120 < remaining <= settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60 + 120

    def test_custom_lifetime(self):
        payload = decode_token(create_access_token("alice", "admin", expires_minutes=5))
        remaining = payload["exp"] - time.time()
        assert 4 * 60 < remaining <= 5 * 60 + 60

    def test_expired_token_is_rejected(self):
        assert decode_token(create_access_token("alice", "admin", expires_minutes=-1)) is None


class TestTokenForgery:
    def _claims(self):
        return {"sub": "alice", "role": "admin", "type": "access", "exp": datetime.utcnow() + timedelta(hours=1)}

    def test_garbage_is_rejected(self):
        for junk in ("", "abc", "a.b.c", "Bearer x", "....", "x" * 5000):
            assert decode_token(junk) is None

    def test_token_signed_with_another_key_is_rejected(self):
        forged = jwt.encode(self._claims(), "some-other-key-that-is-long-enough-0123456789", algorithm="HS256")
        assert decode_token(forged) is None

    def test_tampered_payload_is_rejected(self):
        header, _payload, signature = create_access_token("bob", "reader").split(".")
        evil = _b64({"sub": "bob", "role": "admin", "type": "access", "exp": 9999999999})
        assert decode_token(f"{header}.{evil}.{signature}") is None

    def test_alg_none_is_rejected(self):
        token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'alice', 'role': 'admin', 'type': 'access', 'exp': 9999999999})}."
        assert decode_token(token) is None

    def test_other_hmac_algorithms_are_rejected(self):
        # the right key but HS512: only the configured algorithm is accepted
        forged = jwt.encode(self._claims(), settings.SECRET_KEY, algorithm="HS512")
        assert decode_token(forged) is None


# --------------------------------------------------------------------------- share session tokens

class TestShareSessionToken:
    def _make(self, **kw):
        args = dict(share_id=7, code="abc123xyz", asset_type="volume", asset_id=3,
                    expires_at=datetime.utcnow() + timedelta(hours=1))
        args.update(kw)
        return decode_token(create_share_session_token(**args))

    def test_claims(self):
        payload = self._make(can_download=True)
        assert payload["type"] == "share"
        assert payload["sub"] == "share:abc123xyz"
        assert (payload["sid"], payload["kind"], payload["aid"], payload["cd"]) == (7, "volume", 3, True)

    def test_download_is_off_by_default(self):
        assert self._make()["cd"] is False

    def test_min_lod_only_present_when_set(self):
        assert "min_lod" not in self._make()
        assert self._make(min_lod=2)["min_lod"] == 2

    def test_expires_when_the_share_does(self):
        assert self._make(expires_at=datetime.utcnow() - timedelta(seconds=5)) is None


# --------------------------------------------------------------------------- file tokens

class TestFileToken:
    def test_basic_claims(self):
        payload = decode_token(create_file_token("alice", 12, "sharded"))
        assert (payload["sub"], payload["vol"], payload["kind"], payload["type"]) == ("alice", 12, "sharded", "file")

    def test_lifetime_follows_the_setting(self):
        payload = decode_token(create_file_token("alice", 1, "sharded"))
        remaining = payload["exp"] - time.time()
        assert 0 < remaining <= settings.FILE_TOKEN_EXPIRE_MINUTES * 60 + 60

    def test_optional_claims(self):
        payload = decode_token(create_file_token("alice", 1, "sharded", min_lod=0, ver="v1"))
        assert payload["min_lod"] == 0 and payload["ver"] == "v1"
        bare = decode_token(create_file_token("alice", 1, "sharded"))
        assert "min_lod" not in bare and "ver" not in bare

    def test_remembers_the_account_it_was_minted_for(self):
        payload = decode_token(create_file_token("alice", 1, "sharded", user=SimpleNamespace(id=5)))
        assert payload["uid"] == 5 and "shr" not in payload

    def test_remembers_the_share_link_it_was_minted_for(self):
        guest = SimpleNamespace(id=-1, _share_scope={"share_id": 9})
        payload = decode_token(create_file_token("guest:x", 1, "sharded", user=guest))
        assert payload["shr"] == 9 and "uid" not in payload

    def test_no_binding_without_a_user(self):
        payload = decode_token(create_file_token("alice", 1, "sharded"))
        assert "uid" not in payload and "shr" not in payload


def test_module_has_no_default_secret():
    # the signing key always comes from the environment (config.check_secret_key refuses placeholders)
    assert security.settings.SECRET_KEY
    assert "change_me" not in security.settings.SECRET_KEY
