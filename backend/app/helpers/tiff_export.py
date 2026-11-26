"""One resolution level of a volume, written out as a stack of TIFF slices inside a ZIP archive.

The level is read brick by brick with the reader the slice views use (helpers/slices.py), one layer of bricks
along Z at a time: a layer is decoded into a slab of `brick_size` slices (so about 64 x height x width voxels
are held in RAM), and its slices are then written as TIFF files straight into the archive. Nothing but the
archive is written to disk. The slices are stored in it without further compression: voxel data hardly
compresses, and deflating a multi-gigabyte archive would cost far more time than it saves bytes.

The archive holds
    <name>/slice_0000.tif, slice_0001.tif, ...   one 2D TIFF per Z position (the physical voxel size is in the
                                                 TIFF's resolution tags when the volume's unit is a length)
    <name>/metadata.json                         dimensions, voxel size, unit, data type, level
It is written under a ".partial" name and renamed when it is complete, so a file that exists is a whole one.

(scripts/volume_to_tiff_stack.py does the same for the command line, with more options; it stands on its own.)
"""
import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
import tifffile

from .slices import SliceError, VolumeSlicesKiln

# Length units a volume's "unit" may name -> centimetres (what a TIFF's resolution is given in)
_UNIT_CM = {"nm": 1e-7, "um": 1e-4, "\u00b5m": 1e-4, "\u03bcm": 1e-4, "mm": 0.1, "cm": 1.0, "m": 100.0}
_PER_SLICE_OVERHEAD = 1024  # bytes: TIFF header and tags, and the archive's entry for the slice


def read_meta(vol_dir: Path) -> dict:
    """The volume's volume.json. Raises SliceError (404) when it cannot be read."""
    try:
        meta = json.loads((Path(vol_dir) / "volume.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SliceError(f"Cannot read volume.json: {exc}", 404) from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("levels"), list) or not meta["levels"]:
        raise SliceError("volume.json lists no levels", 404)
    return meta


def _levels(meta: dict) -> List[dict]:
    return sorted((lv for lv in meta["levels"] if isinstance(lv, dict) and isinstance(lv.get("lod"), int)),
                  key=lambda lv: lv["lod"])


def level_spacing(meta: dict, lod: int) -> List[float]:
    """Voxel size (x, y, z) of a level, in the volume's unit. It doubles with each step down the pyramid along
    an axis whose size changed (the same rule routers/volumes.py applies when a share link caps the detail)."""
    levels = _levels(meta)
    spacing = [float(v) for v in (meta.get("voxelSpacing") or [1.0, 1.0, 1.0])]
    for axis in range(3):
        steps = sum(1 for i in range(1, lod + 1) if levels[i]["dimensions"][axis] != levels[i - 1]["dimensions"][axis])
        spacing[axis] *= 2 ** steps
    return spacing


def level_options(meta: dict) -> List[dict]:
    """What a person chooses from: every level with its size in voxels, voxel size and the size of the stack
    (voxel data plus the files' own overhead), finest first."""
    itemsize = np.dtype(meta.get("format", "uint8")).itemsize
    out = []
    for lv in _levels(meta):
        w, h, d = (int(v) for v in lv["dimensions"])
        out.append({
            "level": lv["lod"],
            "dimensions": [w, h, d],
            "voxel_size": level_spacing(meta, lv["lod"]),
            "slices": d,
            "bytes": w * h * d * itemsize + d * _PER_SLICE_OVERHEAD,
        })
    return out


def download_name(slug: str, level: int) -> str:
    return f"{slug}_lod{level}_tiff.zip"


def _tiff_bytes(plane: np.ndarray, resolution: Optional[tuple]) -> bytes:
    buf = io.BytesIO()
    kwargs = {"photometric": "minisblack", "metadata": None, "bigtiff": plane.nbytes >= 3 * 1024 ** 3}
    if resolution:
        kwargs.update(resolution=resolution, resolutionunit="CENTIMETER")
    tifffile.imwrite(buf, plane, **kwargs)
    return buf.getvalue()


def export_tiff_zip(
    vol_dir: Path,
    level: int,
    out_path: Path,
    *,
    name: str,
    title: str = "",
    threads: int = 4,
    progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Writes level `level` of the volume in `vol_dir` to the ZIP archive `out_path` and returns its size in bytes.
    `name` names the folder inside the archive. `progress(done, total)` is called as slices are written (in
    slices); an exception it raises stops the export (the partial archive is deleted). Raises SliceError when the level does not exist or cannot be read."""
    vol_dir, out_path = Path(vol_dir), Path(out_path)
    meta = read_meta(vol_dir)
    vs = VolumeSlicesKiln(vol_dir, level)
    width, height, depth = vs.dims["x"], vs.dims["y"], vs.dims["z"]
    brick = vs.brick_size
    dtype = np.dtype(vs.source_dtype)
    spacing = level_spacing(meta, level)
    unit = str(meta.get("unit") or "").strip()
    cm = _UNIT_CM.get(unit.lower())
    resolution = (1.0 / (spacing[0] * cm), 1.0 / (spacing[1] * cm)) if cm and spacing[0] > 0 and spacing[1] > 0 else None
    digits = max(4, len(str(depth - 1)))
    stamp = time.gmtime()[:6]

    info = {
        "title": title,
        "level": level,
        "levelCount": vs.level_count,
        "dimensions": [width, height, depth],
        "dataType": dtype.name,
        "voxelSize": spacing,
        "unit": unit or None,
        "sliceAxis": "z",
        "slices": depth,
        "note": "Slices are 2D TIFF files, one per Z position, in order. "
                + ("Level 0 is the full resolution." if level == 0
                   else f"Level {level} is the full resolution reduced {2 ** level} times along each axis "
                        "(each step averages 2x2x2 voxels)."),
    }

    partial = out_path.with_name(out_path.name + ".partial")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf, \
                ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
            def add(filename: str, data: bytes) -> None:
                entry = zipfile.ZipInfo(f"{name}/{filename}", date_time=stamp)
                entry.compress_type = zipfile.ZIP_STORED
                entry.external_attr = 0o644 << 16
                zf.writestr(entry, data)

            add("metadata.json", (json.dumps(info, indent=2) + "\n").encode("utf-8"))
            if progress:
                progress(0, depth)
            for bz in range(vs.bricks["z"]):
                z0 = bz * brick
                z1 = min(depth, z0 + brick)
                slab = np.zeros((z1 - z0, height, width), dtype=dtype)  # empty bricks stay zero

                def fill(coord, bz=bz, slab=slab, z1=z1, z0=z0):
                    bx, by = coord
                    core = vs.read_brick_core(bx, by, bz)
                    if core is None:
                        return
                    y0, x0 = by * brick, bx * brick
                    y1, x1 = min(height, y0 + brick), min(width, x0 + brick)
                    slab[:, y0:y1, x0:x1] = core[: z1 - z0, : y1 - y0, : x1 - x0]

                list(pool.map(fill, ((bx, by) for by in range(vs.bricks["y"]) for bx in range(vs.bricks["x"]))))

                step = max(1, threads * 2)  # (bounded: only this many encoded slices are in RAM at once)
                for first in range(0, z1 - z0, step):
                    last = min(z1 - z0, first + step)
                    encoded = pool.map(lambda k: _tiff_bytes(slab[k], resolution), range(first, last))
                    for k, data in zip(range(first, last), encoded):
                        add(f"slice_{z0 + k:0{digits}d}.tif", data)
                    done = z0 + last
                    if progress:
                        progress(done, depth)
        os.replace(partial, out_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return out_path.stat().st_size
