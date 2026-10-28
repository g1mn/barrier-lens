"""Offline tests for app/geometry.py on synthetic scenes (scripts/synth_scene.py). Run with -s to see the table."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import geometry  # noqa: E402
from synth_scene import ground_ref_for, render_scene  # noqa: E402

GAPS = [50, 70, 90, 120]
DISTS = [1.5, 3.0, 5.0]
REFS = ["a4x4", "block"]
ROWS = []


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.parametrize("dist", DISTS)
@pytest.mark.parametrize("gap", GAPS)
def test_true_gap_inside_band(gap, dist, ref):
    s = render_scene(gap, dist, ref)
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"], s["frame_size"])
    ROWS.append((ref, dist, gap, r))
    if r["verdict"] == "unknown":
        # allowed only as an honest refusal for a reference a few pixels tall (one block at 5 m)
        side = np.hypot(*np.diff(geometry.order_corners(s["ref_corners_true"])[[0, 3]], axis=0)[0])
        assert side < 15 and "too small" in r["why"], (side, r["why"])
        return
    lo, hi = r["band_cm"]
    assert lo <= gap <= hi, (gap, r["gap_cm"], r["band_cm"])
    if hi < 70:
        assert r["verdict"] == "blocked"
    elif lo >= 80:
        assert r["verdict"] == "passes"


def test_band_widens_with_smaller_reference():
    widths = []
    for ref, dist in [("a4x4", 1.5), ("a4x4", 3.0), ("block", 3.0)]:
        s = render_scene(90, dist, ref)
        lo, hi = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"], s["frame_size"])["band_cm"]
        widths.append(hi - lo)
    assert widths[0] < widths[1] < widths[2], widths


def test_corners_from_mask_close_to_truth():
    s = render_scene(90, 3.0, "a4x4")
    c = geometry.find_reference_corners(s["ref_mask"])
    assert np.abs(c - geometry.order_corners(s["ref_corners_true"])).max() < 1.0


def test_unknown_without_reference():
    s = render_scene(90, 3.0, "a4x4")
    r = geometry.measure_gap({}, s["barrier_masks"], s["frame_size"])
    assert r["verdict"] == "unknown" and r["gap_cm"] is None and "reference" in r["why"]


def test_unknown_when_reference_tiny():
    s = render_scene(90, 3.0, "a4x4")
    tiny = [[600, 400], [604, 400], [605, 403], [599, 403]]
    r = geometry.measure_gap({"kind": "a4x4", "corners": tiny}, s["barrier_masks"], s["frame_size"])
    assert r["verdict"] == "unknown" and "too small" in r["why"]


def test_unknown_with_one_barrier_and_no_edge():
    s = render_scene(90, 3.0, "a4x4")
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"][:1], s["frame_size"])
    assert r["verdict"] == "unknown"


def test_unknown_when_corners_degenerate():
    s = render_scene(90, 3.0, "a4x4")
    bad = [[500, 400], [700, 400], [600, 401], [650, 450]]  # not convex
    r = geometry.measure_gap({"kind": "a4x4", "corners": bad}, s["barrier_masks"], s["frame_size"])
    assert r["verdict"] == "unknown" and "ill-conditioned" in r["why"]


def test_sam_object_inputs_and_strip_size():
    from app.sam import encode_rle
    s = render_scene(90, 3.0, "a4x4")
    objs = [{"id": k, "score": 1.0, "mask_rle": encode_rle(m)} for k, m in enumerate(s["barrier_masks"])]
    ref = {"kind": "a4x4", "mask": {"mask_rle": encode_rle(s["ref_mask"])}}
    r = geometry.measure_gap(ref, objs, s["frame_size"])
    assert r["band_cm"][0] <= 90 <= r["band_cm"][1]
    assert geometry.ref_size_cm({"kind": "tactile_block", "n_blocks": 3}) == (30.0, 90.0)
    result = {"width": s["frame_size"][0], "height": s["frame_size"][1],
              "frames": [{"i": 0, "objects": [{"id": 7, "score": 0.9, "mask_rle": encode_rle(s["ref_mask"])}]}]}
    assert geometry.corners_from_sam(result, 0) is not None


def test_verdict_rule():
    assert geometry.verdict_for(80, 90) == "passes"
    assert geometry.verdict_for(60, 69.9) == "blocked"
    assert geometry.verdict_for(65, 85) == "tight"


def teardown_module(module):
    if not ROWS:
        return
    print("\nref    dist_m  true_cm  measured_cm  band_cm          verdict  ref_min_side_px")
    for ref, dist, gap, r in sorted(ROWS, key=lambda t: (t[0], t[1], t[2])):
        band = "-" if r["band_cm"] is None else f"{r['band_cm'][0]:.1f}-{r['band_cm'][1]:.1f}"
        side = r.get("detail", {}).get("ref_min_side_px", "-")
        print(f"{ref:6} {dist:6.1f}  {gap:7d}  {str(r['gap_cm']):>11}  {band:15}  {r['verdict']:7}  {side}")
