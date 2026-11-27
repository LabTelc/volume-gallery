"""Browser sign-in by session cookie: the cookie itself, /api/auth/me and logout, what ends a session, the CSRF checks,
guests, the sign-in lockout and the security headers. (The older login / link / password flows are in
test_auth_flows.py.)"""
import base64
import hashlib
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlmodel import Session, select

from app import sessions as vg_sessions
from app import web_security
from app.config import settings
from app.database import engine
from app.models import AuditEvent, Role, UserSession
from app.utils import get_utc_now

from conftest import PASSWORD, session_headers

COOKIE = "__Host-vg_session"


def login(client, username, password=PASSWORD, **extra):
    return client.post("/api/auth/login", data={"username": username, "password": password, **extra})


def set_cookies(response):
    return response.headers.get_list("set-cookie")


def cookie_attributes(header):
    """'name=value; HttpOnly; Max-Age=0' -> (name, value, {'httponly': True, 'max-age': '0'})"""
    first, *rest = [part.strip() for part in header.split(";")]
    name, _, value = first.partition("=")
    attrs = {}
    for part in rest:
        key, _, val = part.partition("=")
        attrs[key.lower()] = val or True
    return name, value, attrs


def rows():
    with Session(engine) as db:
        return db.exec(select(UserSession)).all()


class TestTheCookie:
    def test_login_sets_exactly_the_expected_cookie(self, client, reader):
        response = login(client, reader.username)
        assert response.status_code == 200
        [header] = set_cookies(response)
        name, value, attrs = cookie_attributes(header)
        assert name == COOKIE
        assert value and "." not in value  # an opaque id, not a JWT
        assert attrs == {"httponly": True, "secure": True, "samesite": "Lax", "path": "/"}
        # (no Domain, no Max-Age, no Expires: a session cookie that only this exact host gets)

    def test_the_body_says_who_but_carries_no_credential(self, client, reader, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", False)
        body = login(client, reader.username).json()
        assert body == {"username": reader.username, "role": "reader"}

    def test_the_server_keeps_only_a_hash_of_the_id(self, client, reader):
        raw = cookie_attributes(set_cookies(login(client, reader.username))[0])[1]
        [row] = rows()
        assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest() and raw not in row.token_hash
        assert row.user_id == reader.id and row.remember is False

    def test_remember_me_adds_a_max_age(self, client, reader):
        response = login(client, reader.username, remember="true")
        _, _, attrs = cookie_attributes(set_cookies(response)[0])
        assert attrs["max-age"] == str(settings.REMEMBER_DAYS * 86400)
        assert rows()[0].remember is True

    def test_local_http_development_flag(self, client, reader, monkeypatch):
        monkeypatch.setattr(settings, "COOKIE_SECURE", False)
        name, _, attrs = cookie_attributes(set_cookies(login(client, reader.username))[0])
        assert name == "vg_session"  # (the __Host- prefix needs Secure)
        assert "secure" not in attrs and attrs["httponly"] is True and attrs["samesite"] == "Lax"

    def test_every_login_starts_a_new_session_and_ends_the_one_it_was_sent(self, client, reader):
        first = cookie_attributes(set_cookies(login(client, reader.username))[0])[1]
        second = cookie_attributes(set_cookies(
            client.post("/api/auth/login", data={"username": reader.username, "password": PASSWORD},
                        headers={"Cookie": f"{COOKIE}={first}"})
        )[0])[1]
        assert first != second
        assert len(rows()) == 1  # the first was ended, not left behind
        assert client.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={first}"}).status_code == 401

    def test_a_failed_login_sets_no_cookie(self, client, reader):
        response = login(client, reader.username, "wrong-password-1")
        assert response.status_code == 401 and set_cookies(response) == [] and rows() == []

    def test_a_disabled_or_pending_account_gets_no_session(self, client, make_user):
        make_user("off", disabled=True)
        make_user("new", pending=True)
        for name in ("off", "new"):
            assert login(client, name).status_code == 401
        assert rows() == []


class TestMe:
    def test_200_with_the_cookie_401_without(self, client, editor, auth):
        assert client.get("/api/auth/me").status_code == 401
        response = client.get("/api/auth/me", headers=auth(editor))
        assert response.status_code == 200
        assert response.json() == {"username": editor.username, "role": "editor"}

    def test_the_answer_is_not_cached(self, client, editor, auth):
        headers = client.get("/api/auth/me", headers=auth(editor)).headers
        assert headers["cache-control"] == "no-store"
        assert client.post("/api/auth/login", data={"username": "x", "password": "y"}).headers["cache-control"] == "no-store"

    def test_a_jwt_in_the_cookie_is_not_a_session(self, client, reader):
        from app.security import create_access_token
        token = create_access_token(reader.username, "reader")
        assert client.get("/api/auth/me", headers={"Cookie": f"{COOKIE}={token}"}).status_code == 401

    def test_the_plain_cookie_name_is_ignored_in_secure_mode(self, client, reader, auth):
        raw = auth(reader)["Cookie"].split("=", 1)[1]
        assert client.get("/api/auth/me", headers={"Cookie": f"vg_session={raw}"}).status_code == 401

    def test_the_session_cookie_authenticates_ordinary_endpoints(self, client, reader, auth):
        assert client.get("/api/volumes", headers=auth(reader)).status_code == 200
        assert client.get("/api/volumes").status_code == 401


class TestLogout:
    def test_the_old_cookie_no_longer_works(self, client, reader, auth):
        headers = auth(reader)
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        response = client.post("/api/auth/logout", headers=headers)
        assert response.status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401  # replayed: refused
        assert client.get("/api/volumes", headers=headers).status_code == 401
        assert rows() == []

    def test_the_cookie_is_expired_with_the_same_attributes(self, client, reader, auth):
        response = client.post("/api/auth/logout", headers=auth(reader))
        [header] = set_cookies(response)
        name, value, attrs = cookie_attributes(header)
        assert name == COOKIE and value in ("", '""')
        assert attrs["max-age"] == "0"
        assert attrs["path"] == "/" and attrs["secure"] is True and attrs["httponly"] is True and attrs["samesite"] == "Lax"

    def test_other_sessions_of_the_user_are_not_touched(self, client, reader, auth):
        here, there = auth(reader), auth(reader)
        client.post("/api/auth/logout", headers=here)
        assert client.get("/api/auth/me", headers=there).status_code == 200

    def test_it_needs_no_valid_session_and_still_clears_the_cookie(self, client):
        response = client.post("/api/auth/logout", headers={"Cookie": f"{COOKIE}=stale"})
        assert response.status_code == 200 and cookie_attributes(set_cookies(response)[0])[2]["max-age"] == "0"

    def test_it_is_audited(self, client, reader, auth, db):
        client.post("/api/auth/logout", headers=auth(reader))
        assert [e.actor_name for e in db.exec(select(AuditEvent).where(AuditEvent.action == "auth.logout"))] == [reader.username]

    def test_a_get_does_not_log_out(self, client, reader, auth):
        headers = auth(reader)
        assert client.get("/api/auth/logout", headers=headers).status_code in (404, 405)  # (no such GET route)
        assert client.get("/api/auth/me", headers=headers).status_code == 200


class TestWhatEndsASession:
    def test_a_disabled_account_stops_at_once(self, client, admin, reader, auth):
        headers = auth(reader)
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        assert client.patch(f"/api/users/{reader.id}", json={"disabled": True}, headers=auth(admin)).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_disabling_in_the_database_alone_is_enough(self, client, reader, auth, db):
        headers = auth(reader)
        reader.disabled = True
        db.add(reader)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert rows() == []  # (and the row is gone)

    def test_a_role_change_takes_effect_at_once(self, client, admin, editor, auth):
        headers = auth(editor)
        assert client.get("/api/shares", headers=headers).status_code == 200  # an editor's endpoint
        assert client.patch(f"/api/users/{editor.id}", json={"role": "reader"}, headers=auth(admin)).status_code == 200
        # the sign-in is ended: the user has to sign in again, and then is a reader
        assert client.get("/api/shares", headers=headers).status_code == 401
        client.cookies.clear()
        assert login(client, editor.username).json()["role"] == "reader"

    def test_the_role_is_read_from_the_user_not_from_the_session(self, client, editor, auth, db):
        headers = auth(editor)
        editor.role = Role.reader  # changed behind the app's back: no session was revoked
        db.add(editor)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).json()["role"] == "reader"
        assert client.get("/api/shares", headers=headers).status_code == 403

    def test_deleting_the_account(self, client, admin, reader, auth):
        headers = auth(reader)
        assert client.delete(f"/api/users/{reader.id}", headers=auth(admin)).status_code == 200
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert rows() and all(r.user_id != reader.id for r in rows())

    def test_changing_the_password_ends_the_others_and_keeps_this_browser_in(self, client, reader, auth):
        here, there = auth(reader), auth(reader)
        response = client.post("/api/users/me/password", headers=here,
                               json={"current_password": PASSWORD, "new_password": "a-brand-new-password"})
        assert response.status_code == 200
        new_raw = cookie_attributes(set_cookies(response)[0])[1]
        assert client.get("/api/auth/me", headers=there).status_code == 401
        assert client.get("/api/auth/me", headers=here).status_code == 401  # the id it had is replaced ...
        assert client.get("/api/auth/me", headers=session_headers(new_raw)).status_code == 200  # ... by the new one

    def test_a_remembered_session_stays_remembered_after_a_password_change(self, client, reader, auth):
        response = client.post("/api/users/me/password", headers=auth(reader, remember=True),
                               json={"current_password": PASSWORD, "new_password": "a-brand-new-password"})
        assert "max-age" in cookie_attributes(set_cookies(response)[0])[2]

    def test_setting_a_password_from_a_link_signs_in_and_ends_old_sessions(self, client, admin, reader, auth):
        old = auth(reader)
        link = client.post(f"/api/users/{reader.id}/password-link", headers=auth(admin)).json()
        response = client.post("/api/auth/set-password", json={"token": link["token"], "password": "another-new-password"})
        assert response.status_code == 200
        name, value, attrs = cookie_attributes(set_cookies(response)[0])
        assert name == COOKIE and attrs["httponly"] is True
        assert client.get("/api/auth/me", headers=old).status_code == 401
        assert client.get("/api/auth/me", headers=session_headers(value)).status_code == 200

    def test_idle_timeout(self, client, reader, auth, db):
        headers = auth(reader)
        row = rows()[0]
        row.last_seen_at = get_utc_now() - timedelta(minutes=settings.SESSION_IDLE_MINUTES + 1)
        db.add(row)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_activity_keeps_a_session_alive_within_the_limit(self, client, reader, auth, db):
        headers = auth(reader)
        row = rows()[0]
        row.last_seen_at = get_utc_now() - timedelta(minutes=settings.SESSION_IDLE_MINUTES - 5)
        db.add(row)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        assert get_utc_now() - vg_sessions.aware(rows()[0].last_seen_at) < timedelta(minutes=1)  # refreshed

    def test_absolute_timeout_whatever_the_activity(self, client, reader, auth, db):
        headers = auth(reader)
        row = rows()[0]
        row.expires_at = get_utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_a_remembered_session_has_its_own_limits(self, client, reader, auth, db):
        headers = auth(reader, remember=True)
        row = rows()[0]
        row.last_seen_at = get_utc_now() - timedelta(minutes=settings.SESSION_IDLE_MINUTES + 60)  # idle for over 8 h
        db.add(row)
        db.commit()
        assert client.get("/api/auth/me", headers=headers).status_code == 200  # fine: the limit is days

    def test_expired_rows_are_purged(self, reader, auth, db):
        auth(reader)
        row = rows()[0]
        row.expires_at = get_utc_now() - timedelta(seconds=1)
        db.add(row)
        db.commit()
        vg_sessions.purge_expired(db)
        assert rows() == []


class TestDatasetTokens:
    def test_a_dataset_token_stops_when_the_session_that_minted_it_ends(self, client, editor, auth, make_volume):
        volume = make_volume()
        headers = auth(editor)
        url = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=headers).json()["url"]
        path = urlparse(url).path
        assert client.get(f"{path}/volume.json").status_code == 200  # no cookie, no header: the token is the credential
        client.post("/api/auth/logout", headers=headers)
        assert client.get(f"{path}/volume.json").status_code in (401, 403)

    def test_it_also_stops_when_the_account_is_disabled(self, client, editor, auth, make_volume, db):
        volume = make_volume()
        url = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=auth(editor)).json()["url"]
        editor.disabled = True
        db.add(editor)
        db.commit()
        assert client.get(f"{urlparse(url).path}/volume.json").status_code in (401, 403)

    def test_a_token_from_another_session_of_the_user_is_unaffected(self, client, editor, auth, make_volume):
        volume = make_volume()
        here, there = auth(editor), auth(editor)
        url = client.get(f"/api/volumes/{volume.id}/volume-data-access-url", headers=there).json()["url"]
        client.post("/api/auth/logout", headers=here)
        assert client.get(f"{urlparse(url).path}/volume.json").status_code == 200


class TestCsrf:
    def test_a_state_changing_request_without_the_header_is_refused(self, raw_client, admin, auth):
        headers = {k: v for k, v in auth(admin).items() if k != "X-Requested-With"}
        for method, path in (
            ("post", "/api/users"), ("put", "/api/volumes/1/report"), ("patch", "/api/users/1"), ("delete", "/api/users/1"),
        ):
            response = getattr(raw_client, method)(path, headers=headers)
            assert response.status_code == 403, (method, path)
            assert "X-Requested-With" in response.json()["detail"]

    def test_a_wrong_header_value_is_refused(self, raw_client, admin, auth):
        headers = {**auth(admin), "X-Requested-With": "XMLHttpRequest"}
        assert raw_client.post("/api/users", json={"username": "x"}, headers=headers).status_code == 403

    def test_nothing_changed_by_a_refused_request(self, raw_client, admin, auth, db):
        headers = {k: v for k, v in auth(admin).items() if k != "X-Requested-With"}
        raw_client.post("/api/users", json={"username": "mallory", "role": "admin"}, headers=headers)
        from app.models import User
        assert db.exec(select(User).where(User.username == "mallory")).first() is None

    @pytest.mark.parametrize("origin", ["https://evil.example", "http://evil.example", "null", "http://testserver.evil.example",
                                        "https://testserver", "http://testserver:8000", "not a url"])
    def test_a_foreign_origin_is_refused(self, client, admin, auth, origin):
        response = client.post("/api/users", json={"username": "x"}, headers={**auth(admin), "Origin": origin})
        assert response.status_code == 403 and "Cross-origin" in response.json()["detail"]

    def test_a_foreign_referer_is_refused_when_there_is_no_origin(self, client, admin, auth):
        headers = {k: v for k, v in auth(admin).items() if k != "Origin"}
        assert client.post("/api/users", json={"username": "x"}, headers={**headers, "Referer": "https://evil.example/page"}).status_code == 403
        assert client.post("/api/users", json={"username": "x1"}, headers={**headers, "Referer": "http://testserver/index.html"}).status_code == 200

    def test_the_own_origin_passes(self, client, admin, auth):
        assert client.post("/api/users", json={"username": "ok"}, headers=auth(admin)).status_code == 200

    def test_no_origin_at_all_is_decided_by_the_header(self, client, admin, auth):
        headers = {k: v for k, v in auth(admin).items() if k != "Origin"}
        assert client.post("/api/users", json={"username": "cli"}, headers=headers).status_code == 200

    def test_behind_nginx_the_forwarded_scheme_and_host_count_and_default_ports_are_left_out(self, client, admin, auth):
        # nginx sends Host "gallery.example:443" and X-Forwarded-Proto https; the browser's Origin has no :443
        forwarded = {**auth(admin), "Host": "gallery.example:443", "X-Forwarded-Proto": "https"}
        assert client.post("/api/users", json={"username": "a"}, headers={**forwarded, "Origin": "https://gallery.example"}).status_code == 200
        assert client.post("/api/users", json={"username": "b"}, headers={**forwarded, "Origin": "http://gallery.example"}).status_code == 403
        odd = {**auth(admin), "Host": "gallery.example:8443", "X-Forwarded-Proto": "https"}
        assert client.post("/api/users", json={"username": "c"}, headers={**odd, "Origin": "https://gallery.example:8443"}).status_code == 200
        assert client.post("/api/users", json={"username": "d"}, headers={**odd, "Origin": "https://gallery.example"}).status_code == 403

    def test_allowed_origins_setting(self, client, admin, auth, monkeypatch):
        headers = {**auth(admin), "Origin": "https://other.example"}
        assert client.post("/api/users", json={"username": "x"}, headers=headers).status_code == 403
        monkeypatch.setattr(settings, "ALLOWED_ORIGINS", "https://other.example, https://third.example")
        assert client.post("/api/users", json={"username": "x"}, headers=headers).status_code == 200

    def test_login_and_logout_are_checked_too(self, raw_client, reader):
        assert raw_client.post("/api/auth/login", data={"username": reader.username, "password": PASSWORD}).status_code == 403
        assert raw_client.post("/api/auth/logout").status_code == 403
        assert rows() == []

    def test_the_raw_upload_put_is_checked(self, raw_client, editor, auth):
        headers = {k: v for k, v in auth(editor).items() if k != "X-Requested-With"}
        assert raw_client.put("/api/uploads/volume?name=a.tif", content=b"x", headers=headers).status_code == 403

    def test_reads_are_never_checked(self, raw_client, reader, auth):
        headers = {k: v for k, v in auth(reader).items() if k not in ("X-Requested-With", "Origin")}
        assert raw_client.get("/api/volumes", headers={**headers, "Origin": "https://evil.example"}).status_code == 200

    def test_there_is_no_cors(self, raw_client, reader, auth):
        evil = {"Origin": "https://evil.example"}
        for response in (
            raw_client.get("/api/volumes", headers={**auth(reader), **evil}),
            raw_client.options("/api/volumes", headers={**evil, "Access-Control-Request-Method": "DELETE",
                                                          "Access-Control-Request-Headers": "x-requested-with"}),
        ):
            assert not any(name.lower().startswith("access-control-") for name in response.headers)

    def test_a_cross_site_form_post_cannot_do_anything(self, raw_client, admin, auth):
        # what a form on another site makes the browser send: the cookie (if SameSite allowed it), a form body, an Origin
        # of that site and no custom header
        headers = {"Cookie": auth(admin)["Cookie"], "Origin": "https://evil.example",
                   "Content-Type": "application/x-www-form-urlencoded"}
        assert raw_client.post("/api/users", content="username=mallory&role=admin", headers=headers).status_code == 403


class TestGuests:
    def test_a_share_token_cannot_reach_editor_or_admin_endpoints(self, client, make_volume, create_share, open_share):
        volume = make_volume()
        guest = open_share(create_share(volume.id)["code"])
        for method, path in (
            ("get", "/api/users"), ("post", "/api/users"), ("get", "/api/shares"), ("post", "/api/shares"),
            ("patch", f"/api/volumes/{volume.id}"), ("delete", f"/api/volumes/{volume.id}"),
            ("put", f"/api/volumes/{volume.id}/report"), ("get", "/api/uploads/auth"), ("get", "/api/audit"),
        ):
            response = getattr(client, method)(path, headers=guest)
            assert response.status_code in (403, 404, 405, 422), (method, path, response.status_code)
        assert client.get(f"/api/volumes/{volume.id}", headers=guest).status_code == 200  # what the link shows

    def test_me_says_guest(self, client, make_volume, create_share, open_share):
        guest = open_share(create_share(make_volume().id)["code"])
        body = client.get("/api/auth/me", headers=guest).json()
        assert body["role"] == "guest" and body["username"].startswith("guest:")

    def test_a_real_session_wins_over_a_share_token_sent_with_it(self, client, editor, auth, make_volume, create_share, open_share):
        guest = open_share(create_share(make_volume().id)["code"])
        both = {**auth(editor), "Authorization": guest["Authorization"]}
        assert client.get("/api/auth/me", headers=both).json() == {"username": editor.username, "role": "editor"}

    def test_an_expired_cookie_falls_back_to_the_share_token(self, client, make_volume, create_share, open_share):
        guest = open_share(create_share(make_volume().id)["code"])
        both = {**guest, "Cookie": f"{COOKIE}=stale"}
        assert client.get("/api/auth/me", headers=both).json()["role"] == "guest"


class TestBearerAccessTokens:
    def test_the_login_answer_carries_a_token_only_while_the_flag_is_on(self, client, reader, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", True)
        assert "access_token" in login(client, reader.username).json()
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", False)
        assert "access_token" not in login(client, reader.username).json()

    def test_the_flag_decides_whether_an_access_token_is_accepted(self, client, reader, monkeypatch):
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", True)
        token = login(client, reader.username).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", False)
        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_share_tokens_work_whatever_the_flag(self, client, make_volume, create_share, open_share, monkeypatch):
        guest = open_share(create_share(make_volume().id)["code"])
        monkeypatch.setattr(settings, "ALLOW_BEARER_AUTH", False)
        assert client.get("/api/auth/me", headers=guest).status_code == 200


class TestLoginLockout:
    def attempt(self, client, username, password="wrong-password-1", ip="198.51.100.7"):
        return client.post("/api/auth/login", data={"username": username, "password": password}, headers={"X-Real-IP": ip})

    def test_five_failures_lock_the_name_out_from_that_address(self, client, reader):
        for _ in range(settings.LOGIN_MAX_FAILURES):
            assert self.attempt(client, reader.username).status_code == 401
        response = self.attempt(client, reader.username, PASSWORD)  # even the right password
        assert response.status_code == 429 and "Retry-After" in response.headers
        assert rows() == []

    def test_unknown_names_are_locked_the_same_way(self, client, reader):
        # no enumeration: the lockout looks alike for a name that exists and one that does not
        for _ in range(settings.LOGIN_MAX_FAILURES):
            assert self.attempt(client, "no-such-user").status_code == 401
        assert self.attempt(client, "no-such-user").status_code == 429

    def test_another_address_is_not_locked_out(self, client, reader):
        for _ in range(settings.LOGIN_MAX_FAILURES):
            self.attempt(client, reader.username)
        assert self.attempt(client, reader.username, PASSWORD, ip="203.0.113.9").status_code == 200

    def test_another_name_from_the_same_address_is_not_locked_out(self, client, reader, editor):
        for _ in range(settings.LOGIN_MAX_FAILURES):
            self.attempt(client, reader.username)
        assert self.attempt(client, editor.username, PASSWORD).status_code == 200

    def test_a_successful_login_clears_the_count_for_that_name_and_address(self, client, reader):
        for _ in range(settings.LOGIN_MAX_FAILURES - 1):
            self.attempt(client, reader.username)
        assert self.attempt(client, reader.username, PASSWORD).status_code == 200
        for _ in range(settings.LOGIN_MAX_FAILURES - 1):
            assert self.attempt(client, reader.username).status_code == 401  # would have been locked without the reset

    def test_one_address_with_many_names(self, client, monkeypatch):
        monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_IP", 6)
        for i in range(6):
            assert self.attempt(client, f"guess-{i}").status_code == 401
        assert self.attempt(client, "guess-new").status_code == 429

    def test_one_name_from_many_addresses(self, client, reader, monkeypatch):
        monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES_PER_USER", 4)
        for i in range(4):
            assert self.attempt(client, reader.username, ip=f"192.0.2.{i}").status_code == 401
        assert self.attempt(client, reader.username, PASSWORD, ip="192.0.2.99").status_code == 429

    def test_the_lockout_ends_with_the_window(self, client, reader, db):
        for _ in range(settings.LOGIN_MAX_FAILURES):
            self.attempt(client, reader.username)
        assert self.attempt(client, reader.username, PASSWORD).status_code == 429
        for event in db.exec(select(AuditEvent)).all():
            event.at = get_utc_now() - timedelta(minutes=settings.LOGIN_WINDOW_MINUTES + 1)
            db.add(event)
        db.commit()
        assert self.attempt(client, reader.username, PASSWORD).status_code == 200

    def test_the_attempted_name_is_not_stored_for_unknown_accounts(self, client, db):
        self.attempt(client, "hunter2-pasted-by-mistake")
        dump = " ".join(f"{e.target_name} {e.detail}" for e in db.exec(select(AuditEvent)))
        assert "hunter2" not in dump

    def test_blocked_attempts_are_audited(self, client, reader, db):
        for _ in range(settings.LOGIN_MAX_FAILURES + 1):
            self.attempt(client, reader.username)
        assert db.exec(select(AuditEvent).where(AuditEvent.action == "auth.login_blocked")).first() is not None


class TestSecurityHeaders:
    def test_every_response_has_them(self, client, reader, auth):
        for response in (client.get("/api/auth/me"), client.get("/api/volumes", headers=auth(reader)), client.get("/")):
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "same-origin"
            policy = response.headers["content-security-policy"]
            for directive in ("object-src 'none'", "frame-ancestors 'none'", "base-uri 'self'", "default-src 'self'"):
                assert directive in policy
            assert "unsafe-inline" not in policy.split("style-src")[0]  # (scripts: none; only styles use it)
            assert "'unsafe-eval'" not in policy  # ('wasm-unsafe-eval' is a different, narrower thing)

    def test_refused_requests_have_them_too(self, raw_client):
        response = raw_client.post("/api/users")
        assert response.status_code == 403 and response.headers["x-content-type-options"] == "nosniff"

    def test_the_inline_theme_script_is_allowed_by_its_hash(self, client, monkeypatch):
        # (not pytest's tmp_path: the suite's TMPDIR is wiped between tests)
        tmp_path = Path(tempfile.mkdtemp())
        snippet = "(function(){ document.documentElement.dataset.theme = 'dark'; })();"
        (tmp_path / "viewers").mkdir()
        (tmp_path / "index.html").write_text(f"<html><head><script>{snippet}</script></head></html>", encoding="utf-8")
        (tmp_path / "viewers" / "v.html").write_text(
            f'<script>{snippet}</script><script src="/vendor/x.js"></script><script src="/a.js"> </script>', encoding="utf-8")
        monkeypatch.setattr(settings, "FRONTEND_DIR", str(tmp_path))
        web_security.csp_value.cache_clear()
        try:
            policy = client.get("/api/auth/me").headers["content-security-policy"]
        finally:
            web_security.csp_value.cache_clear()
        digest = base64.b64encode(hashlib.sha256(snippet.encode()).digest()).decode()
        script_src = [d for d in policy.split("; ") if d.startswith("script-src")][0]
        assert script_src.split().count(f"'sha256-{digest}'") == 1  # once, though two pages carry it
        assert "'self'" in script_src and len(script_src.split()) == 4  # + wasm-unsafe-eval; the src= scripts add nothing

    def test_csp_modes(self, client, monkeypatch):
        monkeypatch.setattr(settings, "CSP_MODE", "report-only")
        headers = client.get("/api/auth/me").headers
        assert "content-security-policy" not in headers and "content-security-policy-report-only" in headers
        monkeypatch.setattr(settings, "CSP_MODE", "off")
        headers = client.get("/api/auth/me").headers
        assert "content-security-policy" not in headers and "content-security-policy-report-only" not in headers

    def test_hsts_only_when_configured_and_https(self, client, monkeypatch):
        assert "strict-transport-security" not in client.get("/api/auth/me").headers
        monkeypatch.setattr(settings, "HSTS_MAX_AGE", 31536000)
        assert "strict-transport-security" not in client.get("/api/auth/me").headers  # plain http
        secure = client.get("/api/auth/me", headers={"X-Forwarded-Proto": "https"}).headers
        assert secure["strict-transport-security"] == "max-age=31536000"

    def test_the_frontend_pages_are_not_marked_no_store(self, client):
        assert "no-store" not in client.get("/").headers.get("cache-control", "")
