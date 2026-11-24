"""Tags, folders and asset editing rules (app/organize.py), plus the folders API."""
import pytest
from fastapi import HTTPException

from app.models import Folder
from app.organize import (
    MAX_FOLDER_DEPTH, MAX_TAG_LENGTH, MAX_TAGS, clean_folder_name, clean_tags, folder_depth, folder_height,
    is_inside, split_tags,
)


def folders(*rows):
    """folders((id, parent_id), ...) -> {id: Folder}"""
    return {i: Folder(id=i, name=f"f{i}", parent_id=p) for i, p in rows}


# --------------------------------------------------------------------------- tags

class TestCleanTags:
    def test_trims_and_collapses_whitespace(self):
        assert clean_tags(["  a   b  "]) == ["a b"]

    def test_commas_split_tags(self):
        assert clean_tags(["a, b ,c"]) == ["a", "b", "c"]

    def test_drops_empties(self):
        assert clean_tags(["", "  ", ",,", "x"]) == ["x"]

    def test_duplicates_are_dropped_case_insensitively_first_spelling_wins(self):
        assert clean_tags(["Bone", "bone", "BONE", "Skull"]) == ["Bone", "Skull"]

    def test_order_is_kept(self):
        assert clean_tags(["c", "a", "b"]) == ["c", "a", "b"]

    def test_tag_length_limit(self):
        assert clean_tags(["x" * MAX_TAG_LENGTH]) == ["x" * MAX_TAG_LENGTH]
        with pytest.raises(HTTPException) as exc:
            clean_tags(["x" * (MAX_TAG_LENGTH + 1)])
        assert exc.value.status_code == 400

    def test_tag_count_limit(self):
        assert len(clean_tags([f"t{i}" for i in range(MAX_TAGS)])) == MAX_TAGS
        with pytest.raises(HTTPException) as exc:
            clean_tags([f"t{i}" for i in range(MAX_TAGS + 1)])
        assert exc.value.status_code == 400

    def test_duplicates_do_not_count_towards_the_limit(self):
        assert len(clean_tags(["a"] * 100)) == 1

    def test_split_tags(self):
        assert split_tags("a,b,c") == ["a", "b", "c"]
        assert split_tags("") == [] and split_tags(None) == []
        assert split_tags("a,,b") == ["a", "b"]


# --------------------------------------------------------------------------- folder names

class TestCleanFolderName:
    def test_normalises_whitespace(self):
        assert clean_folder_name("  My   folder ") == "My folder"

    @pytest.mark.parametrize("bad", [None, "", "   ", "a/b", "a\\b", "x" * 81])
    def test_rejects_bad_names(self, bad):
        with pytest.raises(HTTPException) as exc:
            clean_folder_name(bad)
        assert exc.value.status_code == 400


# --------------------------------------------------------------------------- folder tree maths

class TestFolderTree:
    def test_depth(self):
        tree = folders((1, None), (2, 1), (3, 2))
        assert folder_depth(tree, None) == 0
        assert folder_depth(tree, 1) == 1
        assert folder_depth(tree, 3) == 3

    def test_depth_of_unknown_folder_is_zero(self):
        assert folder_depth(folders((1, None)), 99) == 0

    def test_height(self):
        tree = folders((1, None), (2, 1), (3, 2), (4, 1))
        assert folder_height(tree, 1) == 3
        assert folder_height(tree, 4) == 1

    def test_is_inside(self):
        tree = folders((1, None), (2, 1), (3, 2), (4, None))
        assert is_inside(tree, 3, 1) is True
        assert is_inside(tree, 1, 1) is True
        assert is_inside(tree, 1, 3) is False
        assert is_inside(tree, 4, 1) is False
        assert is_inside(tree, None, 1) is False

    def test_cycles_do_not_hang(self):
        loop = folders((1, 2), (2, 1))
        assert folder_depth(loop, 1) == 2
        assert folder_height(loop, 1) >= 1
        assert is_inside(loop, 1, 99) is False


# --------------------------------------------------------------------------- folders API

class TestFoldersApi:
    def make(self, client, headers, name, parent_id=None, expect=200):
        response = client.post("/api/folders", json={"name": name, "parent_id": parent_id}, headers=headers)
        assert response.status_code == expect, response.text
        return response.json()

    def test_editor_can_create_and_list(self, client, editor, auth):
        a = self.make(client, auth(editor), "Skulls")
        assert a["name"] == "Skulls" and a["parent_id"] is None
        listed = client.get("/api/folders", headers=auth(editor)).json()
        assert [f["name"] for f in listed] == ["Skulls"]

    def test_reader_cannot_create(self, client, reader, auth):
        assert client.post("/api/folders", json={"name": "x"}, headers=auth(reader)).status_code == 403

    def test_sibling_names_are_unique_case_insensitively(self, client, editor, auth):
        self.make(client, auth(editor), "Bones")
        self.make(client, auth(editor), "bones", expect=409)

    def test_same_name_is_fine_in_different_parents(self, client, editor, auth):
        a = self.make(client, auth(editor), "A")
        b = self.make(client, auth(editor), "B")
        self.make(client, auth(editor), "Inner", a["id"])
        self.make(client, auth(editor), "Inner", b["id"])

    def test_unknown_parent(self, client, editor, auth):
        self.make(client, auth(editor), "x", parent_id=999, expect=404)

    def test_nesting_depth_limit(self, client, editor, auth):
        parent = None
        for level in range(MAX_FOLDER_DEPTH):
            parent = self.make(client, auth(editor), f"level{level}", parent)["id"]
        self.make(client, auth(editor), "one-too-deep", parent, expect=400)

    def test_cannot_move_a_folder_into_itself_or_a_descendant(self, client, editor, auth):
        a = self.make(client, auth(editor), "A")
        b = self.make(client, auth(editor), "B", a["id"])
        for target in (a["id"], b["id"]):
            response = client.patch(f"/api/folders/{a['id']}", json={"parent_id": target}, headers=auth(editor))
            assert response.status_code == 400, response.text

    def test_move_to_top_level(self, client, editor, auth):
        a = self.make(client, auth(editor), "A")
        b = self.make(client, auth(editor), "B", a["id"])
        response = client.patch(f"/api/folders/{b['id']}", json={"parent_id": None}, headers=auth(editor))
        assert response.status_code == 200 and response.json()["parent_id"] is None

    def test_rename_collision(self, client, editor, auth):
        self.make(client, auth(editor), "A")
        b = self.make(client, auth(editor), "B")
        assert client.patch(f"/api/folders/{b['id']}", json={"name": "a"}, headers=auth(editor)).status_code == 409

    def test_deleting_a_folder_moves_its_contents_up(self, client, editor, auth, make_volume, db):
        a = self.make(client, auth(editor), "A")
        b = self.make(client, auth(editor), "B", a["id"])
        vol = make_volume("vol-in-b", folder_id=b["id"])
        assert client.delete(f"/api/folders/{b['id']}", headers=auth(editor)).status_code == 200
        db.refresh(vol)
        assert vol.folder_id == a["id"]

    def test_readers_only_see_folders_that_hold_something_they_may_view(self, client, editor, reader, auth, make_volume, db):
        from app.models import VolumeAccess
        outer = self.make(client, auth(editor), "Outer")
        inner = self.make(client, auth(editor), "Inner", outer["id"])
        self.make(client, auth(editor), "Secret")
        vol = make_volume("vol-in-inner", folder_id=inner["id"])
        assert client.get("/api/folders", headers=auth(reader)).json() == []
        db.add(VolumeAccess(user_id=reader.id, volume_id=vol.id))
        db.commit()
        names = sorted(f["name"] for f in client.get("/api/folders", headers=auth(reader)).json())
        assert names == ["Inner", "Outer"]  # the folder above is shown too; "Secret" stays hidden

    def test_guests_see_no_folders(self, client, editor, auth, make_volume, create_share, open_share):
        vol = make_volume()
        share = create_share(vol.id)
        assert client.get("/api/folders", headers=open_share(share["code"])).json() == []


# --------------------------------------------------------------------------- asset details

class TestAssetUpdate:
    def patch(self, client, headers, volume_id, **body):
        return client.patch(f"/api/volumes/{volume_id}", json=body, headers=headers)

    def test_updates_only_the_fields_sent(self, client, editor, auth, make_volume):
        vol = make_volume(title="Old", description="keep me")
        response = self.patch(client, auth(editor), vol.id, title="  New  ")
        assert response.status_code == 200
        assert response.json()["title"] == "New"
        assert response.json()["description"] == "keep me"

    def test_empty_title_is_refused(self, client, editor, auth, make_volume):
        vol = make_volume()
        assert self.patch(client, auth(editor), vol.id, title="   ").status_code == 400

    def test_title_and_description_limits(self, client, editor, auth, make_volume):
        vol = make_volume()
        assert self.patch(client, auth(editor), vol.id, title="x" * 201).status_code == 400
        assert self.patch(client, auth(editor), vol.id, description="x" * 5001).status_code == 400
        assert self.patch(client, auth(editor), vol.id, title="x" * 200, description="y" * 5000).status_code == 200

    def test_tags_are_cleaned(self, client, editor, auth, make_volume):
        vol = make_volume()
        response = self.patch(client, auth(editor), vol.id, tags=["Bone, bone", " skull "])
        assert response.status_code == 200
        assert response.json()["tags"] == ["Bone", "skull"]

    def test_unknown_folder(self, client, editor, auth, make_volume):
        vol = make_volume()
        assert self.patch(client, auth(editor), vol.id, folder_id=999).status_code == 404

    def test_file_in_folder_and_back_to_top_level(self, client, editor, auth, make_volume):
        vol = make_volume()
        folder = client.post("/api/folders", json={"name": "F"}, headers=auth(editor)).json()
        assert self.patch(client, auth(editor), vol.id, folder_id=folder["id"]).json()["folder_id"] == folder["id"]
        assert self.patch(client, auth(editor), vol.id, folder_id=None).json()["folder_id"] is None

    def test_reader_cannot_edit(self, client, reader, auth, make_volume):
        vol = make_volume()
        assert self.patch(client, auth(reader), vol.id, title="hacked").status_code == 403

    def test_unknown_volume(self, client, editor, auth):
        assert self.patch(client, auth(editor), 999, title="x").status_code == 404

    def test_markup_in_text_is_stored_verbatim_not_interpreted(self, client, editor, auth, make_volume):
        # The API stores text as is; the frontend escapes it (escapeHtml / textContent).
        vol = make_volume()
        payload = '<img src=x onerror=alert(1)>'
        response = self.patch(client, auth(editor), vol.id, title=payload, description=payload)
        assert response.json()["title"] == payload
