"""Processing tasks: TIFF stacks of a volume, prepared in the background, kept for a while, downloaded as a ZIP."""
import io
import json
import shutil
import zipfile
from datetime import timedelta

import numpy as np
import pytest
import tifffile

from app import processing
from app.config import settings
from app.helpers import tiff_export
from app.helpers.volume import decompose_tiff_stack
from app.models import AssetStatus, ProcessingTask, TaskStatus, VolumeAccess
from app.utils import get_utc_now

W, H, D = 130, 100, 70   # not multiples of the brick size: the edges are the interesting part


@pytest.fixture(scope="module")
def real_volume():
    """A real brick volume (2 levels) built from TIFF slices, and the voxels it was built from.
    (Built beside the data folder: it lives in the test run's own temp directory, which is removed at exit.)"""
    root = settings.DATA_DIR.parent / "real-volume"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    rng = np.random.default_rng(7)
    voxels = rng.integers(0, 60000, size=(D, H, W)).astype(np.uint16)
    voxels[:, :64, :64] = 0  # one brick that is all zero (stored as empty)
    (root / "src").mkdir()
    for z in range(D):
        tifffile.imwrite(root / "src" / f"s_{z:04d}.tif", voxels[z])
    decompose_tiff_stack(root / "src", root / "vol", spacing=(10, 10, 20), max_lod=1, log=lambda _m: None)
    meta = json.loads((root / "vol" / "volume.json").read_text())
    meta["unit"] = "um"
    (root / "vol" / "volume.json").write_text(json.dumps(meta))
    return root / "vol", voxels


@pytest.fixture
def make_real_volume(db, real_volume):
    folder, _ = real_volume

    def _make(slug="scan-a", **fields):
        from app.models import Volume
        shutil.copytree(folder, settings.VOLUMES_DIR / slug)
        volume = Volume(slug=slug, title=fields.pop("title", "Scan A"), status=AssetStatus.ready, num_lod_levels=1, **fields)
        db.add(volume)
        db.commit()
        db.refresh(volume)
        return volume
    return _make


def request(client, headers, volume, level=0):
    return client.post(f"/api/processing/tiff-export/{volume.id}", json={"level": level}, headers=headers)


def grant(db, volume, user):
    db.add(VolumeAccess(volume_id=volume.id, user_id=user.id))
    db.commit()


@pytest.fixture
def tmp_path():
    """A scratch folder of this module's own (other tests clear the shared pytest temp directory)."""
    folder = settings.DATA_DIR.parent / "export-out"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


class TestTiffExportHelper:
    def test_level_zero_is_the_original_voxels(self, real_volume, tmp_path):
        folder, voxels = real_volume
        calls = []
        size = tiff_export.export_tiff_zip(folder, 0, tmp_path / "out.zip", name="scan_lod0", threads=2,
                                           progress=lambda done, total: calls.append((done, total)))
        assert size == (tmp_path / "out.zip").stat().st_size
        assert calls[0] == (0, D) and calls[-1] == (D, D) and [c[0] for c in calls] == sorted(c[0] for c in calls)
        with zipfile.ZipFile(tmp_path / "out.zip") as zf:
            assert zf.namelist()[0] == "scan_lod0/metadata.json"
            assert len(zf.namelist()) == D + 1
            for z in (0, 31, 32, 63, 64, D - 1):  # both sides of the brick borders
                got = tifffile.imread(io.BytesIO(zf.read(f"scan_lod0/slice_{z:04d}.tif")))
                assert got.dtype == np.uint16 and np.array_equal(got, voxels[z]), z
            assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())

    def test_the_voxel_size_is_in_the_tiff_tags(self, real_volume, tmp_path):
        folder, _ = real_volume
        tiff_export.export_tiff_zip(folder, 0, tmp_path / "out.zip", name="s")
        with zipfile.ZipFile(tmp_path / "out.zip") as zf, tifffile.TiffFile(io.BytesIO(zf.read("s/slice_0000.tif"))) as tf:
            page = tf.pages[0]
            assert page.tags["XResolution"].value == (1000, 1)  # 10 um = 1000 pixels per cm
            assert page.tags["ResolutionUnit"].value == 3
            info = json.loads(zf.read("s/metadata.json"))
        assert info["dimensions"] == [W, H, D] and info["voxelSize"] == [10.0, 10.0, 20.0] and info["unit"] == "um"

    def test_a_coarser_level(self, real_volume, tmp_path):
        folder, _ = real_volume
        tiff_export.export_tiff_zip(folder, 1, tmp_path / "out.zip", name="s")
        with zipfile.ZipFile(tmp_path / "out.zip") as zf:
            assert len(zf.namelist()) == 35 + 1
            assert tifffile.imread(io.BytesIO(zf.read("s/slice_0000.tif"))).shape == (50, 65)
            assert json.loads(zf.read("s/metadata.json"))["voxelSize"] == [20.0, 20.0, 40.0]

    def test_options_list_every_level_with_its_size(self, real_volume):
        meta = tiff_export.read_meta(real_volume[0])
        options = tiff_export.level_options(meta)
        assert [o["level"] for o in options] == [0, 1] and options[0]["dimensions"] == [W, H, D]
        assert options[0]["bytes"] >= W * H * D * 2 and options[1]["bytes"] < options[0]["bytes"] / 4

    def test_a_failing_export_leaves_no_file(self, real_volume, tmp_path):
        def stop(done, _total):
            if done >= 64:
                raise RuntimeError("stop")
        with pytest.raises(RuntimeError):
            tiff_export.export_tiff_zip(real_volume[0], 0, tmp_path / "out.zip", name="s", progress=stop)
        assert list(tmp_path.iterdir()) == []


class TestRequest:
    def test_options(self, client, make_real_volume, editor, auth):
        volume = make_real_volume()
        body = client.get(f"/api/processing/tiff-export/{volume.id}", headers=auth(editor)).json()
        assert body["data_type"] == "uint16" and body["unit"] == "um"
        assert body["retention_hours"] == settings.EXPORT_RETENTION_HOURS
        assert [(l["level"], l["slices"], l["allowed"]) for l in body["levels"]] == [(0, D, True), (1, 35, True)]

    def test_a_request_is_prepared_and_downloaded(self, client, make_real_volume, editor, auth, real_volume):
        volume = make_real_volume()
        task = request(client, auth(editor), volume).json()
        # (with no job queue the work ran as a background task before the response came back to the test)
        task = client.get(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).json()
        assert task["status"] == "ready", task["status_log"]
        assert task["params"] == {"level": 0} and task["created_by_name"] == editor.username
        assert task["progress_done"] == task["progress_total"] == D
        assert task["download_name"] == "scan-a_lod0_tiff.zip" and task["result_bytes"] > W * H * D * 2
        remaining = (processing.aware(__import__("datetime").datetime.fromisoformat(task["expires_at"])) - get_utc_now()).total_seconds()
        assert 23.9 * 3600 < remaining <= 24 * 3600

        link = client.get(f"/api/processing/tasks/{task['id']}/download-url", headers=auth(editor)).json()
        got = client.get(link["url"])
        assert got.status_code == 200 and got.headers["content-type"] == "application/octet-stream"
        assert "scan-a_lod0_tiff.zip" in got.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
            assert np.array_equal(tifffile.imread(io.BytesIO(zf.read("scan-a_lod0/slice_0010.tif"))), real_volume[1][10])
        assert (settings.VOLUMES_DIR / "scan-a" / "exports" / f"t{task['id']}.zip").is_file()
        assert not list((settings.VOLUMES_DIR / "scan-a" / "exports").glob("*.partial"))

    def test_asking_again_returns_the_same_task(self, client, make_real_volume, editor, auth):
        volume = make_real_volume()
        first = request(client, auth(editor), volume).json()
        again = request(client, auth(editor), volume).json()
        other_level = request(client, auth(editor), volume, 1).json()
        assert again["id"] == first["id"] and other_level["id"] != first["id"]
        levels = client.get(f"/api/processing/tiff-export/{volume.id}", headers=auth(editor)).json()["levels"]
        assert [(l["task_id"], l["task_status"]) for l in levels] == [(first["id"], "ready"), (other_level["id"], "ready")]

    def test_a_level_the_volume_does_not_have(self, client, make_real_volume, editor, auth):
        volume = make_real_volume()
        assert request(client, auth(editor), volume, 5).status_code == 400
        assert client.post(f"/api/processing/tiff-export/{volume.id}", json={"level": -1}, headers=auth(editor)).status_code == 422

    def test_the_size_limit_closes_the_fine_levels(self, client, make_real_volume, editor, auth, monkeypatch):
        volume = make_real_volume()
        monkeypatch.setattr(settings, "EXPORT_MAX_GB", 1_000_000 / 1024 ** 3)  # about 1 MB: level 1 (0.5 MB) fits
        levels = client.get(f"/api/processing/tiff-export/{volume.id}", headers=auth(editor)).json()["levels"]
        assert [l["allowed"] for l in levels] == [False, True] and "limit" in levels[0]["reason"]
        assert request(client, auth(editor), volume, 0).status_code == 413
        assert request(client, auth(editor), volume, 1).status_code == 200

    def test_a_volume_that_is_not_ready(self, client, make_real_volume, editor, auth, db):
        volume = make_real_volume()
        volume.status = AssetStatus.processing
        db.add(volume)
        db.commit()
        assert request(client, auth(editor), volume).status_code == 409

    def test_not_enough_disk_space_fails_the_task_with_a_message(self, client, make_real_volume, editor, auth, monkeypatch):
        volume = make_real_volume()
        monkeypatch.setattr(processing.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(100, 99, 1))
        task = request(client, auth(editor), volume).json()
        task = client.get(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).json()
        assert task["status"] == "failed" and "Not enough free disk space" in "".join(task["status_log"])
        assert not list((settings.VOLUMES_DIR / "scan-a" / "exports").iterdir())


class TestWho:
    def test_a_reader_with_access_may_ask_and_download(self, client, make_real_volume, reader, auth, db):
        volume = make_real_volume()
        assert request(client, auth(reader), volume).status_code == 404  # cannot see the volume
        grant(db, volume, reader)
        task = request(client, auth(reader), volume).json()
        assert task["created_by_name"] is None  # readers are not told who asked
        link = client.get(f"/api/processing/tasks/{task['id']}/download-url", headers=auth(reader))
        assert link.status_code == 200 and client.get(link.json()["url"]).status_code == 200

    def test_a_reader_sees_only_the_tasks_of_their_volumes(self, client, make_real_volume, editor, reader, auth, db):
        mine, other = make_real_volume("scan-a"), make_real_volume("scan-b")
        request(client, auth(editor), mine)
        request(client, auth(editor), other)
        assert client.get("/api/processing/tasks", headers=auth(reader)).json() == []
        grant(db, mine, reader)
        listed = client.get("/api/processing/tasks", headers=auth(reader)).json()
        assert [t["volume_slug"] for t in listed] == ["scan-a"]
        assert len(client.get("/api/processing/tasks", headers=auth(editor)).json()) == 2
        task_b = client.get("/api/processing/tasks", headers=auth(editor)).json()[0]["id"]
        assert client.get(f"/api/processing/tasks/{task_b}", headers=auth(reader)).status_code == 404

    def test_a_guest_of_a_share_link_has_no_processing(self, client, make_real_volume, create_share, open_share):
        volume = make_real_volume()
        guest = open_share(create_share(volume.id, can_download=True)["code"])
        assert request(client, guest, volume).status_code == 403
        assert client.get("/api/processing/tasks", headers=guest).status_code == 403

    def test_anonymous(self, client, make_real_volume):
        volume = make_real_volume()
        assert client.get("/api/processing/tasks").status_code == 401
        assert client.post(f"/api/processing/tiff-export/{volume.id}", json={"level": 0}).status_code == 401

    def test_a_reader_may_have_only_so_many_tasks_waiting(self, client, make_real_volume, reader, auth, db, monkeypatch):
        volume = make_real_volume()
        grant(db, volume, reader)
        monkeypatch.setattr(settings, "TASK_MAX_ACTIVE_PER_USER", 1)
        db.add(ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug=volume.slug, params='{"level": 7}',
                              status=TaskStatus.running, created_by=reader.id))
        db.commit()
        refused = request(client, auth(reader), volume, 1)
        assert refused.status_code == 429

    def test_only_editors_delete(self, client, make_real_volume, editor, reader, auth, db):
        volume = make_real_volume()
        grant(db, volume, reader)
        task = request(client, auth(editor), volume).json()
        assert client.delete(f"/api/processing/tasks/{task['id']}", headers=auth(reader)).status_code == 403
        assert client.delete(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).status_code == 200
        assert not list((settings.VOLUMES_DIR / "scan-a" / "exports").iterdir())
        assert client.get(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).status_code == 404

    def test_a_download_link_works_for_its_task_only(self, client, make_real_volume, editor, auth):
        a, b = make_real_volume("scan-a"), make_real_volume("scan-b")
        ta, tb = request(client, auth(editor), a).json(), request(client, auth(editor), b).json()
        url = client.get(f"/api/processing/tasks/{ta['id']}/download-url", headers=auth(editor)).json()["url"]
        token = url.rsplit("/", 1)[1]
        assert client.get(f"/api/processing/tasks/{tb['id']}/file/{token}").status_code == 403
        assert client.get(f"/api/processing/tasks/{ta['id']}/file/garbage").status_code == 403


class TestRetention:
    def test_a_result_is_deleted_when_its_time_is_over(self, client, make_real_volume, editor, auth, db):
        volume = make_real_volume()
        task = request(client, auth(editor), volume).json()
        path = settings.VOLUMES_DIR / "scan-a" / "exports" / f"t{task['id']}.zip"
        assert path.is_file()
        assert processing.cleanup_expired() == 0 and path.is_file()
        row = db.get(ProcessingTask, task["id"])
        row.expires_at = get_utc_now() - timedelta(minutes=1)
        db.add(row)
        db.commit()
        assert processing.cleanup_expired() == 1
        assert not path.exists()
        body = client.get(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).json()
        assert body["status"] == "expired"
        assert client.get(f"/api/processing/tasks/{task['id']}/download-url", headers=auth(editor)).status_code == 410
        # ... and it can be asked for again
        again = request(client, auth(editor), volume).json()
        assert again["id"] != task["id"] and again["status"] in ("queued", "running", "ready")

    def test_the_retention_is_a_setting(self, client, make_real_volume, editor, auth, monkeypatch):
        monkeypatch.setattr(settings, "EXPORT_RETENTION_HOURS", 2)
        task = request(client, auth(editor), make_real_volume()).json()
        task = client.get(f"/api/processing/tasks/{task['id']}", headers=auth(editor)).json()
        remaining = (processing.aware(__import__("datetime").datetime.fromisoformat(task["expires_at"])) - get_utc_now()).total_seconds()
        assert 1.9 * 3600 < remaining <= 2 * 3600

    def test_old_rows_of_dead_tasks_are_forgotten(self, make_real_volume, db):
        volume = make_real_volume()
        old = get_utc_now() - processing.FORGET_AFTER - timedelta(days=1)
        for status in (TaskStatus.expired, TaskStatus.failed):
            db.add(ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug="scan-a", status=status,
                                  created_at=old, finished_at=old))
        db.add(ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug="scan-a", status=TaskStatus.failed,
                              finished_at=get_utc_now()))
        db.commit()
        processing.cleanup_expired()
        db.expire_all()
        assert db.query(ProcessingTask).count() == 1

    def test_files_no_task_owns_are_swept(self, make_real_volume):
        make_real_volume()
        folder = settings.VOLUMES_DIR / "scan-a" / "exports"
        folder.mkdir()
        stale, fresh = folder / "t99.zip.partial", folder / "t100.zip.partial"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"x")
        import os, time
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        processing.cleanup_expired()
        assert not stale.exists() and fresh.exists()  # (a recent partial file may belong to a task that is running)


class TestRestart:
    def test_a_task_that_was_running_when_the_server_stopped_failed(self, make_real_volume, db):
        volume = make_real_volume()
        folder = settings.VOLUMES_DIR / "scan-a" / "exports"
        folder.mkdir()
        task = ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug="scan-a", status=TaskStatus.running)
        db.add(task)
        db.commit()
        db.refresh(task)
        (folder / f"t{task.id}.zip.partial").write_bytes(b"half")
        processing.recover_interrupted()
        db.expire_all()
        task = db.get(ProcessingTask, task.id)
        assert task.status == TaskStatus.failed and "restart" in task.status_log
        assert not list(folder.iterdir())

    def test_a_task_whose_job_is_still_alive_is_left_alone(self, make_real_volume, db):
        volume = make_real_volume()
        task = ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug="scan-a", status=TaskStatus.queued)
        db.add(task)
        db.commit()
        db.refresh(task)
        processing.recover_interrupted({task.id})
        db.expire_all()
        assert db.get(ProcessingTask, task.id).status == TaskStatus.queued

    def test_a_task_deleted_while_it_runs_stops_and_leaves_nothing(self, make_real_volume, db):
        volume = make_real_volume()
        task = ProcessingTask(kind=processing.KIND_TIFF, volume_id=volume.id, volume_slug="scan-a",
                              params='{"level": 0}', status=TaskStatus.queued)
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
        real = tiff_export.export_tiff_zip

        def deleting(*args, progress=None, **kwargs):
            # the person deletes the task after the first slices were written
            from sqlmodel import Session
            from app.database import engine
            with Session(engine) as other:
                other.delete(other.get(ProcessingTask, task_id))
                other.commit()
            processing._PROGRESS_EVERY = 0
            return real(*args, progress=progress, **kwargs)

        original = processing._PROGRESS_EVERY
        try:
            tiff_export.export_tiff_zip = deleting
            processing.run_task(task_id)
        finally:
            tiff_export.export_tiff_zip = real
            processing._PROGRESS_EVERY = original
        db.expire_all()
        assert db.get(ProcessingTask, task_id) is None
        assert not list((settings.VOLUMES_DIR / "scan-a" / "exports").iterdir())

    def test_deleting_the_volume_deletes_its_tasks_and_results(self, client, make_real_volume, editor, auth, db):
        volume = make_real_volume()
        request(client, auth(editor), volume)
        assert client.delete(f"/api/volumes/{volume.id}", headers=auth(editor)).status_code == 200
        assert db.query(ProcessingTask).count() == 0 and not (settings.VOLUMES_DIR / "scan-a").exists()
