"""Signing in, tokens as the API sees them, invite/reset links, and changing your own password."""
import json
from datetime import timedelta

import bcrypt
import pytest
from jose import jwt
from sqlmodel import select

from app.config import settings
from app.models import AuditEvent, Role, User, UserToken
from app.security import create_access_token, create_file_token, create_share_session_token, hash_password
from app.utils import get_utc_now

from conftest import PASSWORD


def login(client, username, password=PASSWORD):
    return client.post("/api/auth/login", data={"username": username, "password": password})


def audit_actions(db):
    db.expire_all()
    return [e.action for e in db.exec(select(AuditEvent).order_by(AuditEvent.id)).all()]


# --------------------------------------------------------------------------- login

class TestLogin:
    def test_success(self, client, reader):
        response = login(client, reader.username)
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["role"] == "reader" and body["username"] == reader.username
        claims = jwt.decode(body["access_token"], settings.SECRET_KEY, algorithms=["HS256"])
        assert claims["sub"] == reader.username and claims["type"] == "access" and "pwv" in claims

    def test_the_token_works(self, client, reader):
        token = login(client, reader.username).json()["access_token"]
        me = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200 and me.json()["username"] == reader.username

    def test_wrong_password(self, client, reader):
        response = login(client, reader.username, "wrong-password")
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"

    def test_unknown_user_gets_the_same_answer_as_a_wrong_password(self, client, reader):
        unknown = login(client, "nobody", "whatever-1234")
        wrong = login(client, reader.username, "wrong-password")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()

    def test_disabled_account_cannot_sign_in_even_with_the_right_password(self, client, make_user):
        user = make_user("gone", disabled=True)
        assert login(client, user.username).status_code == 401

    def test_pending_account_without_a_password_cannot_sign_in(self, client, make_user):
        user = make_user("pending", pending=True)
        assert login(client, user.username, "").status_code in (401, 422)
        assert login(client, user.username, "anything-1234").status_code == 401

    def test_overlong_password_is_a_plain_401_not_an_error(self, client, reader):
        assert login(client, reader.username, "x" * 5000).status_code == 401

    def test_username_is_case_sensitive_and_exact(self, client, reader):
        assert login(client, reader.username.upper()).status_code == 401
        assert login(client, reader.username + " ").status_code == 401

    def test_missing_fields(self, client):
        assert client.post("/api/auth/login", data={"username": "x"}).status_code == 422

    def test_json_body_is_not_accepted(self, client, reader):
        response = client.post("/api/auth/login", json={"username": reader.username, "password": PASSWORD})
        assert response.status_code == 422

    def test_successful_and_failed_logins_are_audited(self, client, db, reader):
        login(client, reader.username)
        login(client, reader.username, "wrong-password")
        login(client, "nobody", "whatever-1234")
        assert audit_actions(db) == ["auth.login", "auth.login_failed", "auth.login_failed"]

    def test_failed_login_for_an_unknown_name_does_not_log_the_name(self, client, db):
        login(client, "hunter2-pasted-into-the-username-box", "x" * 8)
        db.expire_all()
        event = db.exec(select(AuditEvent)).one()
        assert event.target_name == "" and event.actor_name == ""
        assert "hunter2" not in event.detail

    def test_failed_login_records_the_reason(self, client, db, reader, make_user):
        make_user("off", disabled=True)
        login(client, reader.username, "wrong-password")
        login(client, "off")
        login(client, "nobody", "whatever-1234")
        db.expire_all()
        events = db.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
        reasons = [json.loads(e.detail)["reason"] for e in events]
        assert reasons == ["bad password", "disabled", "unknown user"]


class TestLoginRehash:
    def test_legacy_bcrypt_hash_is_upgraded_on_login(self, client, db, make_user):
        user = make_user("legacy")
        user.password_hash = bcrypt.hashpw(b"old-password-1", bcrypt.gensalt(rounds=4)).decode()
        db.add(user)
        db.commit()
        assert login(client, "legacy", "old-password-1").status_code == 200
        db.refresh(user)
        assert user.password_hash.startswith("$argon2id$")
        assert login(client, "legacy", "old-password-1").status_code == 200  # still works afterwards

    def test_failed_login_does_not_upgrade_the_hash(self, client, db, make_user):
        user = make_user("legacy")
        legacy = bcrypt.hashpw(b"old-password-1", bcrypt.gensalt(rounds=4)).decode()
        user.password_hash = legacy
        db.add(user)
        db.commit()
        assert login(client, "legacy", "wrong-password").status_code == 401
        db.refresh(user)
        assert user.password_hash == legacy


# --------------------------------------------------------------------------- how the API treats tokens

class TestBearerTokens:
    def test_no_token(self, client):
        response = client.get("/api/users/me")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Bearer not.a.jwt", "Basic dXNlcjpwYXNz", "garbage"])
    def test_malformed_credentials(self, client, header):
        assert client.get("/api/users/me", headers={"Authorization": header}).status_code == 401

    def test_expired_token(self, client, reader):
        token = create_access_token(reader.username, "reader", expires_minutes=-5)
        assert client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_token_of_a_deleted_user(self, client, reader, auth, db):
        headers = auth(reader)
        db.delete(reader)
        db.commit()
        assert client.get("/api/users/me", headers=headers).status_code == 401

    def test_token_of_a_disabled_user_stops_working_at_once(self, client, reader, auth, db):
        headers = auth(reader)
        assert client.get("/api/users/me", headers=headers).status_code == 200
        reader.disabled = True
        db.add(reader)
        db.commit()
        assert client.get("/api/users/me", headers=headers).status_code == 401

    def test_the_role_comes_from_the_database_not_from_the_token(self, client, reader):
        forged_claim = create_access_token(reader.username, "admin")  # claims admin, is a reader
        response = client.get("/api/users", headers={"Authorization": f"Bearer {forged_claim}"})
        assert response.status_code == 403

    def test_a_demotion_applies_to_tokens_already_issued(self, client, make_user, auth, db):
        user = make_user("ex-editor", Role.editor)
        headers = auth(user)
        assert client.get("/api/shares", headers=headers).status_code == 200
        user.role = Role.reader
        db.add(user)
        db.commit()
        assert client.get("/api/shares", headers=headers).status_code == 403

    def test_a_file_token_is_not_a_login_token(self, client, reader):
        token = create_file_token(reader.username, 1, "sharded", user=reader)
        assert client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_share_session_for_a_missing_share_is_refused(self, client):
        token = create_share_session_token(
            share_id=999, code="nope", asset_type="volume", asset_id=1, expires_at=get_utc_now() + timedelta(hours=1)
        )
        assert client.get("/api/volumes", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_token_without_a_subject(self, client):
        token = jwt.encode({"type": "access", "exp": get_utc_now() + timedelta(hours=1)}, settings.SECRET_KEY, algorithm="HS256")
        assert client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_unknown_token_type(self, client, reader):
        token = jwt.encode(
            {"sub": reader.username, "type": "refresh", "exp": get_utc_now() + timedelta(hours=1)},
            settings.SECRET_KEY, algorithm="HS256",
        )
        assert client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_tokens_stop_working_after_a_password_change_elsewhere(self, client, reader, auth, db):
        headers = auth(reader)
        reader.password_hash = hash_password("a-brand-new-password")
        db.add(reader)
        db.commit()
        assert client.get("/api/users/me", headers=headers).status_code == 401


# --------------------------------------------------------------------------- one-time password links

class TestPasswordLinks:
    def invite(self, client, admin, auth, username="newbie", role="reader"):
        response = client.post("/api/users", json={"username": username, "role": role}, headers=auth(admin))
        assert response.status_code == 200, response.text
        return response.json()

    def set_password(self, client, token, password="my-chosen-password"):
        return client.post("/api/auth/set-password", json={"token": token, "password": password})

    def test_invite_flow_end_to_end(self, client, admin, auth):
        link = self.invite(client, admin, auth)
        assert link["purpose"] == "setup" and link["user"]["pending"] is True

        info = client.post("/api/auth/set-password/check", json={"token": link["token"]})
        assert info.status_code == 200
        assert info.json()["username"] == "newbie" and info.json()["purpose"] == "setup"

        done = self.set_password(client, link["token"])
        assert done.status_code == 200
        assert done.json()["username"] == "newbie"
        assert login(client, "newbie", "my-chosen-password").status_code == 200

    def test_checking_a_link_does_not_use_it_up(self, client, admin, auth):
        link = self.invite(client, admin, auth)
        for _ in range(3):
            assert client.post("/api/auth/set-password/check", json={"token": link["token"]}).status_code == 200
        assert self.set_password(client, link["token"]).status_code == 200

    def test_a_link_works_once(self, client, admin, auth):
        link = self.invite(client, admin, auth)
        assert self.set_password(client, link["token"]).status_code == 200
        again = self.set_password(client, link["token"], "another-password-1")
        assert again.status_code == 410
        assert login(client, "newbie", "my-chosen-password").status_code == 200  # the first password stands

    def test_unknown_token(self, client):
        assert self.set_password(client, "x" * 43).status_code == 404
        assert client.post("/api/auth/set-password/check", json={"token": "x" * 43}).status_code == 404

    def test_expired_link(self, client, admin, auth, db):
        link = self.invite(client, admin, auth)
        row = db.exec(select(UserToken)).one()
        row.expires_at = get_utc_now() - timedelta(minutes=1)
        db.add(row)
        db.commit()
        assert self.set_password(client, link["token"]).status_code == 410
        assert client.post("/api/auth/set-password/check", json={"token": link["token"]}).status_code == 410

    def test_the_database_holds_only_a_hash_of_the_token(self, client, admin, auth, db):
        link = self.invite(client, admin, auth)
        row = db.exec(select(UserToken)).one()
        assert link["token"] not in (row.token_hash, row.purpose)
        assert row.token_hash != link["token"] and len(row.token_hash) == 64

    def test_weak_password_is_refused_and_the_link_survives(self, client, admin, auth):
        link = self.invite(client, admin, auth)
        assert self.set_password(client, link["token"], "short").status_code == 400
        assert self.set_password(client, link["token"], "x" * 2000).status_code == 400
        assert self.set_password(client, link["token"]).status_code == 200

    def test_disabled_account(self, client, admin, auth, db):
        link = self.invite(client, admin, auth)
        user = db.exec(select(User).where(User.username == "newbie")).one()
        user.disabled = True
        db.add(user)
        db.commit()
        assert self.set_password(client, link["token"]).status_code == 403

    def test_a_new_link_replaces_the_old_one(self, client, admin, auth):
        first = self.invite(client, admin, auth)
        user_id = first["user"]["id"]
        second = client.post(f"/api/users/{user_id}/password-link", headers=auth(admin)).json()
        assert second["token"] != first["token"]
        assert self.set_password(client, first["token"]).status_code == 404
        assert self.set_password(client, second["token"]).status_code == 200

    def test_reset_signs_the_old_sessions_out(self, client, admin, reader, auth):
        old_session = auth(reader)
        assert client.get("/api/users/me", headers=old_session).status_code == 200
        link = client.post(f"/api/users/{reader.id}/password-link", headers=auth(admin)).json()
        assert link["purpose"] == "reset"
        # until the link is used, nothing changes for the user
        assert client.get("/api/users/me", headers=old_session).status_code == 200
        assert login(client, reader.username).status_code == 200
        done = self.set_password(client, link["token"], "a-reset-password-1")
        assert done.status_code == 200
        assert client.get("/api/users/me", headers=old_session).status_code == 401
        assert login(client, reader.username).status_code == 401  # the old password is gone
        assert login(client, reader.username, "a-reset-password-1").status_code == 200

    def test_using_the_link_returns_a_working_session(self, client, admin, auth):
        link = self.invite(client, admin, auth)
        token = self.set_password(client, link["token"]).json()["access_token"]
        assert client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    def test_links_are_audited(self, client, admin, auth, db):
        link = self.invite(client, admin, auth)
        self.set_password(client, link["token"])
        assert audit_actions(db) == ["user.create", "user.password_set"]


# --------------------------------------------------------------------------- changing your own password

class TestChangeOwnPassword:
    def change(self, client, headers, current=PASSWORD, new="a-new-password-1"):
        return client.post("/api/users/me/password", json={"current_password": current, "new_password": new}, headers=headers)

    def test_requires_sign_in(self, client):
        assert self.change(client, {}).status_code == 401

    def test_wrong_current_password(self, client, reader, auth):
        assert self.change(client, auth(reader), current="nope-nope-nope").status_code == 400

    def test_new_must_differ(self, client, reader, auth):
        assert self.change(client, auth(reader), new=PASSWORD).status_code == 400

    def test_new_must_be_long_enough(self, client, reader, auth):
        assert self.change(client, auth(reader), new="short").status_code == 400

    def test_success_signs_other_sessions_out_but_returns_a_fresh_one(self, client, reader, auth):
        old = auth(reader)
        response = self.change(client, old)
        assert response.status_code == 200
        fresh = {"Authorization": f"Bearer {response.json()['access_token']}"}
        assert client.get("/api/users/me", headers=old).status_code == 401
        assert client.get("/api/users/me", headers=fresh).status_code == 200
        assert login(client, reader.username, "a-new-password-1").status_code == 200
        assert login(client, reader.username).status_code == 401

    def test_is_audited(self, client, reader, auth, db):
        self.change(client, auth(reader))
        assert audit_actions(db) == ["user.password_set"]
