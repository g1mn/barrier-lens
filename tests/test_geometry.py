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


# ---------------------------------------------------------------- tactile strip (width known, length unknown)

STRIP_DISTS = [2.0, 3.0, 4.0]


@pytest.mark.parametrize("dist", STRIP_DISTS)
@pytest.mark.parametrize("gap", [70, 90, 120])
def test_strip_true_gap_inside_band(gap, dist):
    s = render_scene(gap, dist, "strip")  # camera yawed 4 deg and 5 cm off-centre: not square to the walkway
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"], s["frame_size"])
    ROWS.append(("strip", dist, gap, r))
    lo, hi = r["band_cm"]
    assert lo <= gap <= hi, (gap, r["gap_cm"], r["band_cm"])
    assert abs(r["gap_cm"] - gap) < 1.0
    assert r["verdict"] == geometry.verdict_for(lo, hi)
    assert "level camera" in r["why"] and "not square" in r["why"]
    assert r["detail"]["scale_cm_per_px"] and r["detail"]["measure"] == "across-walkway corridor"


def test_strip_square_camera_row_estimate_agrees():
    s = render_scene(90, 3.0, "strip", yaw_deg=0.0, cam_x=0.0)
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"], s["frame_size"])
    assert r["detail"]["camera_square"] and "roughly square" in r["why"]
    assert abs(r["detail"]["row_gap_cm"] - 90) < 1.0 and r["band_cm"][0] <= 90 <= r["band_cm"][1]


@pytest.mark.parametrize("roll", [-2.0, 1.0])
def test_strip_small_roll_still_inside_band(roll):
    s = render_scene(90, 3.0, "strip", roll_deg=roll)
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"], s["frame_size"])
    assert r["band_cm"][0] <= 90 <= r["band_cm"][1]


def test_strip_extrapolation_flagged_and_band_wider():
    full = render_scene(90, 3.0, "strip")
    short = render_scene(90, 3.0, "strip", strip_far_m=2.0)  # strip ends 1 m before the barriers
    rf = geometry.measure_gap(ground_ref_for(full), full["barrier_masks"], full["frame_size"])
    rs = geometry.measure_gap(ground_ref_for(short), short["barrier_masks"], short["frame_size"])
    assert "EXTRAPOLATED" not in rf["why"] and "EXTRAPOLATED" in rs["why"]
    assert rs["band_cm"][0] <= 90 <= rs["band_cm"][1]
    assert rs["band_cm"][1] - rs["band_cm"][0] > rf["band_cm"][1] - rf["band_cm"][0]


def test_strip_edges_close_to_truth():
    s = render_scene(90, 3.0, "strip", strip_x_m=0.0)
    e = geometry.strip_edges(s["ref_mask"])
    assert e is not None and e["rows_used"] > 100
    # strip width in px from the fit vs the projected truth at the bottom used row
    from synth_scene import camera, project
    import math
    cam = camera(pitch_deg=s["pitch_deg"])
    y = e["y_bot"]
    # ground Y whose projection lands on row y, along the strip's centre line (bisection)
    lo_y, hi_y = 0.3, 10.0
    for _ in range(60):
        mid = (lo_y + hi_y) / 2
        if project(cam, np.array([[0.0, mid, 0.0]]))[0, 1] > y:
            lo_y = mid
        else:
            hi_y = mid
    pl, pr = project(cam, np.array([[-0.15, mid, 0.0], [0.15, mid, 0.0]]))
    fit_w = (e["right"][0] - e["left"][0]) * y + (e["right"][1] - e["left"][1])
    assert abs(fit_w - math.hypot(*(pr - pl))) < 1.0


def test_strip_unknown_cases():
    s = render_scene(90, 3.0, "strip")
    empty = {"kind": "tactile_strip", "width_cm": 30, "mask": np.zeros_like(s["ref_mask"])}
    r = geometry.measure_gap(empty, s["barrier_masks"], s["frame_size"])
    assert r["verdict"] == "unknown" and "too short" in r["why"]
    r = geometry.measure_gap({"kind": "tactile_strip"}, s["barrier_masks"], s["frame_size"])
    assert r["verdict"] == "unknown" and "mask" in r["why"]
    r = geometry.measure_gap(ground_ref_for(s), s["barrier_masks"][:1], s["frame_size"])
    assert r["verdict"] == "unknown"
    # a square block is not a strip: its edges do not converge enough to place a horizon, or it is too short
    b = render_scene(90, 3.0, "block")
    r = geometry.measure_gap({"kind": "tactile_strip", "mask": b["ref_mask"]}, b["barrier_masks"], b["frame_size"])
    assert r["verdict"] == "unknown" or r["band_cm"][1] - r["band_cm"][0] > 10


def test_strip_sam_object_inputs():
    from app.sam import encode_rle
    s = render_scene(70, 3.0, "strip")
    objs = [{"id": k, "score": 1.0, "mask_rle": encode_rle(m)} for k, m in enumerate(s["barrier_masks"])]
    ref = {"kind": "tactile_strip", "width_cm": 30, "mask": {"mask_rle": encode_rle(s["ref_mask"])}}
    r = geometry.measure_gap(ref, objs, s["frame_size"])
    assert r["band_cm"][0] <= 70 <= r["band_cm"][1]


def test_strip_picks_the_corridor_straight_ahead():
    """Three barriers where the widest corridor sits left of the centre line: the narrower centre one is chosen."""
    xs = [np.array([0.0, 1.0]), np.array([30.0, 31.0]), np.array([33.0, 34.0])]  # gaps 29 vs 2
    p = geometry._strip_passages(xs, None, ["b0", "b1", "b2"], [True, True, False])
    assert [a for _, _, a in p] == [False, True]
    assert geometry._widest(p) == 1 and p[1][0] == "b1|b2"
    assert geometry._gaps_out(p)[1] == {"between": "b1|b2", "cm": 2.0, "ahead": True}
    e = geometry._strip_passages(xs, np.array([-5.0, -4.0]), ["b0", "b1", "b2"], [True, True, False])
    assert e[-1][0] == "edge|b0" and e[-1][2] is False  # edge on the closed side


def test_draw_passage_matched_by_label_not_position():
    """A perturbed draw that reorders the neighbours must not lend its distance to the nominal pair."""
    nominal = [("b0|b1", 90.0, True), ("b1|b2", 50.0, True)]
    assert geometry._by_label([("b1|b2", 51.0, True), ("b0|b1", 91.0, True)], nominal[0][0]) == 91.0
    assert geometry._by_label([("b0|b2", 40.0, True)], nominal[0][0]) is None  # pair gone: a bad draw


def teardown_module(module):
    if not ROWS:
        return
    print("\nref    dist_m  true_cm  measured_cm  band_cm          verdict  ref_min_side_px")
    for ref, dist, gap, r in sorted(ROWS, key=lambda t: (t[0], t[1], t[2])):
        band = "-" if r["band_cm"] is None else f"{r['band_cm'][0]:.1f}-{r['band_cm'][1]:.1f}"
        side = r.get("detail", {}).get("ref_min_side_px", "-")
        print(f"{ref:6} {dist:6.1f}  {gap:7d}  {str(r['gap_cm']):>11}  {band:15}  {r['verdict']:7}  {side}")
