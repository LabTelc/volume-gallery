"""User administration, the role matrix, and the admin-only pages."""
import pytest
from sqlmodel import select

from app.models import AuditEvent, Mesh, MeshAccess, Role, ShareLink, User, UserToken, Volume, VolumeAccess


# --------------------------------------------------------------------------- who may call what

ADMIN_ONLY = [
    ("GET", "/api/users", None),
    ("POST", "/api/users", {"username": "x-new"}),
    ("PATCH", "/api/users/{other}", {"disabled": False}),
    ("DELETE", "/api/users/{other}", None),
    ("POST", "/api/users/{other}/password-link", None),
    ("GET", "/api/admin/shares", None),
    ("GET", "/api/admin/audit", None),
    ("GET", "/api/admin/storage", None),
]

EDITOR_AND_ADMIN = [
    ("GET", "/api/shares", None),
    ("GET", "/api/shares/suggest", None),
    ("POST", "/api/shares", {"asset_type": "volume", "asset_id": 999}),
    ("DELETE", "/api/shares/999", None),
    ("GET", "/api/uploads/auth", None),
    ("POST", "/api/folders", {"name": "x"}),
    ("PATCH", "/api/folders/999", {"name": "y"}),
    ("DELETE", "/api/folders/999", None),
    ("GET", "/api/volumes/{vol}/access", None),
    ("POST", "/api/volumes/{vol}/access", {"user_id": 1}),
    ("DELETE", "/api/volumes/{vol}/access/1", None),
    ("PATCH", "/api/volumes/{vol}", {"title": "t"}),
    ("DELETE", "/api/volumes/{vol}/thumbnail", None),
    ("DELETE", "/api/volumes/{vol}", None),
]


def call(client, method, path, body, headers):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, path, **kwargs)


class TestRoleMatrix:
    @pytest.mark.parametrize("method,path,body", ADMIN_ONLY)
    def test_admin_only_endpoints(self, client, admin, editor, reader, reader2, auth, method, path, body):
        path = path.format(other=reader2.id)
        assert call(client, method, path, body, {}).status_code == 401
        assert call(client, method, path, body, auth(reader)).status_code == 403
        assert call(client, method, path, body, auth(editor)).status_code == 403
        # an admin gets past the gate: whatever the endpoint answers, it is not an auth error
        assert call(client, method, path, body, auth(admin)).status_code not in (401, 403)

    @pytest.mark.parametrize("method,path,body", EDITOR_AND_ADMIN)
    def test_editor_endpoints(self, client, admin, editor, reader, auth, make_volume, method, path, body):
        volume = make_volume("vol-gate")
        path = path.format(vol=volume.id)
        assert call(client, method, path, body, {}).status_code == 401
        assert call(client, method, path, body, auth(reader)).status_code == 403
        assert call(client, method, path, body, auth(editor)).status_code not in (401, 403)

    @pytest.mark.parametrize("method,path,body", EDITOR_AND_ADMIN)
    def test_admins_may_do_what_editors_do(self, client, admin, auth, make_volume, method, path, body):
        volume = make_volume("vol-gate")
        path = path.format(vol=volume.id)
        assert call(client, method, path, body, auth(admin)).status_code not in (401, 403)

    def test_guests_are_not_editors(self, client, make_volume, create_share, open_share):
        volume = make_volume()
        guest = open_share(create_share(volume.id)["code"])
        assert client.post("/api/shares", json={"asset_type": "volume", "asset_id": volume.id}, headers=guest).status_code == 403
        assert client.get("/api/users", headers=guest).status_code == 403
        assert client.patch(f"/api/volumes/{volume.id}", json={"title": "x"}, headers=guest).status_code == 403
        assert client.delete(f"/api/volumes/{volume.id}", headers=guest).status_code == 403

    def test_anonymous_users_see_the_public_pages_only(self, client):
        assert client.get("/api/branding").status_code == 200
        for path in ("/api/volumes", "/api/meshes", "/api/pointclouds", "/api/folders", "/api/users/me"):
            assert client.get(path).status_code == 401, path


# --------------------------------------------------------------------------- admin operations

class TestCreateUser:
    def test_creates_a_pending_account_and_returns_a_link(self, client, admin, auth, db):
        response = client.post("/api/users", json={"username": "  newbie  ", "role": "editor"}, headers=auth(admin))
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["username"] == "newbie"
        assert body["user"]["role"] == "editor"
        assert body["user"]["pending"] is True
        assert body["token"] and body["purpose"] == "setup"
        assert db.exec(select(User).where(User.username == "newbie")).one().password_hash == ""

    def test_role_defaults_to_reader(self, client, admin, auth):
        body = client.post("/api/users", json={"username": "newbie"}, headers=auth(admin)).json()
        assert body["user"]["role"] == "reader"

    def test_duplicate_username(self, client, admin, reader, auth):
        assert client.post("/api/users", json={"username": reader.username}, headers=auth(admin)).status_code == 409

    @pytest.mark.parametrize("body", [{"username": ""}, {"username": "   "}, {"username": "x" * 151}, {"role": "admin"}, {"username": "u", "role": "root"}])
    def test_invalid_payloads(self, client, admin, auth, body):
        assert client.post("/api/users", json=body, headers=auth(admin)).status_code in (400, 422)

    def test_audited(self, client, admin, auth, db):
        client.post("/api/users", json={"username": "newbie"}, headers=auth(admin))
        event = db.exec(select(AuditEvent)).one()
        assert (event.action, event.actor_name, event.target_name) == ("user.create", admin.username, "newbie")


class TestUpdateUser:
    def test_change_role(self, client, admin, reader, auth):
        response = client.patch(f"/api/users/{reader.id}", json={"role": "editor"}, headers=auth(admin))
        assert response.status_code == 200 and response.json()["role"] == "editor"

    def test_disable_and_enable(self, client, admin, reader, auth):
        assert client.patch(f"/api/users/{reader.id}", json={"disabled": True}, headers=auth(admin)).json()["disabled"] is True
        assert client.get("/api/users/me", headers=auth(reader)).status_code == 401
        assert client.patch(f"/api/users/{reader.id}", json={"disabled": False}, headers=auth(admin)).json()["disabled"] is False
        assert client.get("/api/users/me", headers=auth(reader)).status_code == 200

    def test_admins_cannot_demote_or_disable_themselves(self, client, admin, auth):
        assert client.patch(f"/api/users/{admin.id}", json={"role": "editor"}, headers=auth(admin)).status_code == 400
        assert client.patch(f"/api/users/{admin.id}", json={"disabled": True}, headers=auth(admin)).status_code == 400

    def test_an_admin_may_re_set_their_own_role_to_admin(self, client, admin, auth):
        assert client.patch(f"/api/users/{admin.id}", json={"role": "admin"}, headers=auth(admin)).status_code == 200

    def test_passwords_cannot_be_set_through_this_endpoint(self, client, admin, reader, auth):
        response = client.patch(f"/api/users/{reader.id}", json={"password": "new-password-1"}, headers=auth(admin))
        assert response.status_code == 422

    def test_unknown_user(self, client, admin, auth):
        assert client.patch("/api/users/999", json={"disabled": True}, headers=auth(admin)).status_code == 404

    def test_only_real_changes_are_audited(self, client, admin, reader, auth, db):
        client.patch(f"/api/users/{reader.id}", json={"role": "reader"}, headers=auth(admin))  # no change
        assert db.exec(select(AuditEvent)).all() == []
        client.patch(f"/api/users/{reader.id}", json={"role": "editor"}, headers=auth(admin))
        assert [e.action for e in db.exec(select(AuditEvent)).all()] == ["user.update"]


class TestPasswordLinkRules:
    def test_not_for_yourself(self, client, admin, auth):
        assert client.post(f"/api/users/{admin.id}/password-link", headers=auth(admin)).status_code == 400

    def test_not_for_a_disabled_account(self, client, admin, make_user, auth):
        off = make_user("off", disabled=True)
        assert client.post(f"/api/users/{off.id}/password-link", headers=auth(admin)).status_code == 400

    def test_unknown_user(self, client, admin, auth):
        assert client.post("/api/users/999/password-link", headers=auth(admin)).status_code == 404


class TestDeleteUser:
    def test_admins_cannot_delete_themselves(self, client, admin, auth):
        assert client.delete(f"/api/users/{admin.id}", headers=auth(admin)).status_code == 400

    def test_unknown_user(self, client, admin, auth):
        assert client.delete("/api/users/999", headers=auth(admin)).status_code == 404

    def test_deleted_users_cannot_sign_in(self, client, admin, reader, auth):
        assert client.delete(f"/api/users/{reader.id}", headers=auth(admin)).status_code == 200
        response = client.post("/api/auth/login", data={"username": reader.username, "password": "correct-horse-battery"})
        assert response.status_code == 401

    def test_cleans_up_what_pointed_at_the_user(self, client, admin, editor, reader, auth, db, make_volume):
        volume = make_volume("vol-x", created_by=editor.id)
        mesh = Mesh(slug="mesh-x", title="M", created_by=editor.id)
        db.add(mesh)
        db.commit()
        db.add(VolumeAccess(user_id=reader.id, volume_id=volume.id, granted_by=editor.id))
        db.add(MeshAccess(user_id=reader.id, mesh_id=mesh.id, granted_by=editor.id))
        db.add(ShareLink(code="abcd1234", asset_type="volume", asset_id=volume.id, created_by=editor.id,
                         expires_at=volume.created_at))
        db.commit()
        client.post(f"/api/users/{editor.id}/password-link", headers=auth(admin))  # leaves a UserToken for the editor

        assert client.delete(f"/api/users/{reader.id}", headers=auth(admin)).status_code == 200
        assert client.delete(f"/api/users/{editor.id}", headers=auth(admin)).status_code == 200

        db.expire_all()
        assert db.exec(select(VolumeAccess)).all() == []           # the reader's grants went
        assert db.exec(select(MeshAccess)).all() == []
        assert db.exec(select(UserToken)).all() == []
        assert db.get(Volume, volume.id).created_by is None  # the editor's work stays, unowned
        assert db.get(Mesh, mesh.id).created_by is None
        assert db.exec(select(ShareLink)).one().created_by is None

    def test_is_audited_with_the_name_kept(self, client, admin, reader, auth, db):
        client.delete(f"/api/users/{reader.id}", headers=auth(admin))
        event = db.exec(select(AuditEvent)).one()
        assert (event.action, event.target_name) == ("user.delete", reader.username)


class TestMe:
    def test_returns_the_account(self, client, editor, auth):
        body = client.get("/api/users/me", headers=auth(editor)).json()
        assert body["username"] == editor.username and body["role"] == "editor"
        assert "password" not in str(body).lower().replace("pending", "")

    def test_listing_never_leaks_hashes(self, client, admin, reader, auth):
        text = client.get("/api/users", headers=auth(admin)).text
        assert "argon2" not in text and "password_hash" not in text


# --------------------------------------------------------------------------- admin pages

class TestAuditLogEndpoint:
    def test_lists_events_newest_first_with_filters(self, client, admin, auth, db):
        client.post("/api/users", json={"username": "one"}, headers=auth(admin))
        client.post("/api/users", json={"username": "two"}, headers=auth(admin))
        client.post("/api/auth/login", data={"username": "nobody", "password": "wrong-pass-1"})

        everything = client.get("/api/admin/audit", headers=auth(admin)).json()
        assert [e["action"] for e in everything["events"]] == ["auth.login_failed", "user.create", "user.create"]

        only_users = client.get("/api/admin/audit", params={"action": "user."}, headers=auth(admin)).json()
        assert [e["target_name"] for e in only_users["events"]] == ["two", "one"]

        exact = client.get("/api/admin/audit", params={"action": "auth.login_failed"}, headers=auth(admin)).json()
        assert len(exact["events"]) == 1

        by_target = client.get("/api/admin/audit", params={"target": "one"}, headers=auth(admin)).json()
        assert [e["target_name"] for e in by_target["events"]] == ["one"]

    def test_pagination(self, client, admin, auth):
        for i in range(5):
            client.post("/api/users", json={"username": f"user{i}"}, headers=auth(admin))
        first = client.get("/api/admin/audit", params={"limit": 2}, headers=auth(admin)).json()
        assert len(first["events"]) == 2 and first["next_before"] is not None
        second = client.get("/api/admin/audit", params={"limit": 2, "before": first["next_before"]}, headers=auth(admin)).json()
        assert [e["target_name"] for e in second["events"]] == ["user2", "user1"]
        last = client.get("/api/admin/audit", params={"limit": 5, "before": second["next_before"]}, headers=auth(admin)).json()
        assert [e["target_name"] for e in last["events"]] == ["user0"] and last["next_before"] is None

    @pytest.mark.parametrize("limit", [0, 501, -1])
    def test_limit_is_bounded(self, client, admin, auth, limit):
        assert client.get("/api/admin/audit", params={"limit": limit}, headers=auth(admin)).status_code == 422

    def test_like_wildcards_in_filters_are_literal(self, client, admin, auth):
        client.post("/api/users", json={"username": "plain"}, headers=auth(admin))
        body = client.get("/api/admin/audit", params={"target": "%"}, headers=auth(admin)).json()
        assert body["events"] == []


def test_admin_share_overview_lists_links(client, admin, make_volume, create_share, auth):
    volume = make_volume()
    create_share(volume.id, code="listed-link")
    response = client.get("/api/admin/shares", headers=auth(admin))
    assert response.status_code == 200
    assert "listed-link" in response.text


def test_roles_enum_is_what_the_matrix_assumes():
    assert {r.value for r in Role} == {"admin", "editor", "reader", "guest"}
