"""Crop shapes in the views of a saved camera path (PoseSpec.crop)."""
import pytest
from pydantic import ValidationError

from app.schemas import PlaybackSpec, PoseSpec

POSE = dict(back=[0, 0, 1], up=[0, 1, 0], distance=3, target=[0, 0, 0])
SPHERE = {"type": "sphere", "mode": "outside", "center": [0.5, 0.5, 0.5], "size": [0.25, 0, 0], "rotation": [0, 0, 0]}


def test_a_view_can_carry_crop_shapes_and_they_survive_a_round_trip():
    spec = PlaybackSpec(kind="path", keys=[PoseSpec(**POSE, crop=[SPHERE]), PoseSpec(**POSE, crop=[])])
    again = PlaybackSpec(**spec.model_dump(mode="json"))
    assert again.keys[0].crop[0].type == "sphere"
    assert again.keys[0].crop[0].mode == "outside"
    assert again.keys[0].crop[0].enabled is True
    assert again.keys[1].crop == []          # "no cropping at this view"


def test_a_view_without_crop_leaves_the_cropping_alone():
    assert PoseSpec(**POSE).crop is None


@pytest.mark.parametrize("bad", [
    [{**SPHERE, "type": "cone"}],                     # unknown kind
    [{**SPHERE, "mode": "sideways"}],                 # unknown side
    [{**SPHERE, "center": [2, 0.5, 0.5]}],            # centre outside the volume
    [{**SPHERE, "size": [1.5, 0, 0]}],                # size above the longest side
    [{**SPHERE, "rotation": [0, 0, float("nan")]}],   # not a number
    [{**SPHERE, "center": [0.5, 0.5]}],               # two numbers
    [SPHERE, SPHERE],                                 # two shapes of one kind
    [SPHERE] * 5,                                     # more shapes than kinds
])
def test_broken_crop_shapes_are_refused(bad):
    with pytest.raises(ValidationError):
        PoseSpec(**POSE, crop=bad)


# ------------------------------------------------------------------ bookmarks

def _bookmark(crop):
    from app.schemas import BookmarkSpec
    return BookmarkSpec(options={"mode": "dvr", "crop": crop})


def test_a_bookmark_keeps_all_crop_shapes_switched_on_or_off():
    shapes = [{**SPHERE, "enabled": True}, {**SPHERE, "type": "box", "mode": "inside", "enabled": False}]
    spec = _bookmark(shapes)
    again = type(spec)(**spec.model_dump(mode="json"))
    assert [(s.type, s.enabled, s.mode) for s in again.options.crop] == [("sphere", True, "outside"), ("box", False, "inside")]


def test_a_bookmark_without_crop_shapes_leaves_the_cropping_alone():
    from app.schemas import BookmarkSpec
    assert BookmarkSpec(options={"mode": "dvr"}).options.crop is None


@pytest.mark.parametrize("bad", [[SPHERE, SPHERE], [{**SPHERE, "center": [3, 0, 0]}], [{**SPHERE, "type": "cone"}], [SPHERE] * 5])
def test_a_bookmark_with_broken_crop_shapes_is_refused(bad):
    with pytest.raises(ValidationError):
        _bookmark(bad)
