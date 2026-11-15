"""Team tests for the T06 release gate (src/pipeline.py). Input data only — never reads gt/."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.pipeline import analyze_release, decide_release

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "t06_release_gate"
pytestmark = pytest.mark.skipif(not (FIX / "releases/v1.0/annotations.json").exists(), reason="T06 fixture missing")


@pytest.fixture(scope="module")
def world():
    parent = json.loads((FIX / "releases/v1.0/annotations.json").read_text(encoding="utf-8"))
    maps = json.loads((FIX / "releases/label_maps.json").read_text(encoding="utf-8"))
    meta = json.loads((FIX / "releases/v1.0/release_meta.json").read_text(encoding="utf-8"))
    return parent, maps, meta


def _decide(ann, meta, parent, maps):
    return decide_release(analyze_release(ann, meta, parent, maps, {}))


def test_success_parent_like_release_passes_and_benign_change_does_not_block(world):
    parent, maps, meta = world
    assert _decide(copy.deepcopy(parent), meta, parent, maps)[0] == "PASS"
    benign = copy.deepcopy(parent)                               # one jittered box: benign diff
    benign["annotations"][0]["bbox"][0] += 1.0
    assert _decide(benign, meta, parent, maps)[:2] == ("PASS", "clean")


@pytest.mark.parametrize("cause", ["id_collision", "class_swap", "stale_map", "bbox_shift", "dropped_frames"])
def test_failure_silent_defects_are_blocked_with_root_cause(world, cause):
    parent, maps, meta = world
    bad = copy.deepcopy(parent)
    if cause == "id_collision":
        bad["annotations"][1]["id"] = bad["annotations"][0]["id"]
    elif cause == "class_swap":                                   # JSON still valid, ids swapped
        bad["categories"][0]["name"], bad["categories"][1]["name"] = "pedestrian", "car"
    elif cause == "stale_map":                                    # `truck` (lm-3) missing
        bad["categories"] = [c for c in bad["categories"] if c["name"] != "truck"]
    elif cause == "bbox_shift":
        for a in bad["annotations"][::3]:
            a["bbox"][0] = max(0.0, a["bbox"][0] - 40)
            a["bbox"][1] = max(0.0, a["bbox"][1] - 40)
    elif cause == "dropped_frames":
        drop = {i["id"] for i in bad["images"][10:20]}
        bad["images"] = [i for i in bad["images"] if i["id"] not in drop]
        bad["annotations"] = [a for a in bad["annotations"] if a["image_id"] not in drop]
    status, root, _ = _decide(bad, meta, parent, maps)
    assert status in {"HOLD", "REWORK"}
    assert root == cause


def test_edge_unknown_label_map_version_blocks(world):
    parent, maps, meta = world
    status, root, _ = _decide(copy.deepcopy(parent), {**meta, "label_map_version": "lm-99"}, parent, maps)
    assert (status, root) == ("HOLD", "stale_map")
