"""camera_pose mode of app/geometry.py (no reference in the clip) on synthetic scenes with a known camera pose.

Scenes come from scripts/synth_scene.py (ref="none"): two box barriers, optional ground lines along the walkway, a
camera with known height, field of view (26 mm equivalent), pitch, roll and a 4 deg yaw. Run with -s for the table.
"""
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import calibration, geometry  # noqa: E402
from synth_scene import camera, project, render_scene  # noqa: E402

SIZE = (1280, 720)
HFOV = calibration.hfov_from_f35(calibration.DEFAULT_F35_MM, SIZE)   # inside the default 23-28 mm range
LINES = (-1.0, -0.45, 0.6, 1.1)
SCENES = [(90, 3.0, 0.0), (70, 2.0, 2.0), (120, 4.0, -1.5)]          # (gap cm, barrier distance m, roll deg)
TABLE = {}


@lru_cache(maxsize=None)
def scene(gap, dist, roll, lines=LINES, line_kw=()):
    return render_scene(gap, dist, "none", lines_x_m=lines, line_kw=dict(line_kw), hfov_deg=HFOV, roll_deg=roll)


def poses(s):
    """The four inputs of the table, all through app/calibration.camera_pose so the field names are the real ones."""
    t = s["camera"]
    default = calibration.camera_pose({}, None, {}, SIZE)
    # phone sensor: off by 1 deg in pitch and roll, quoted at +-1.5 deg
    sensor_in = {"beta": 90 - (t["pitch_deg"] + 1.0), "gamma": t["roll_deg"] + 1.0, "angle": 0, "n": 20}
    sensor = {**calibration.camera_pose({}, sensor_in, {}, SIZE), "pitch_sd_deg": 1.5}
    cal = calibration.clean_profile({"calibration": {"height_m": t["height_m"] + 0.01, "pitch_bias_deg": 0.0,
                                                     "hfov_deg": HFOV}})
    calibrated = {**calibration.camera_pose(cal, sensor_in, {}, SIZE), "pitch_sd_deg": 1.5,
                  "height_range_m": [t["height_m"] + 0.01 - 0.03, t["height_m"] + 0.01 + 0.03]}
    return {"default": default, "default+lines": {**default, "ground_lines": s["line_masks"]},
            "sensor": sensor, "calibrated": calibrated}


@lru_cache(maxsize=None)
def measured(gap, dist, roll):
    s = scene(gap, dist, roll)
    out = {k: geometry.measure_gap(p, s["barrier_masks"], SIZE) for k, p in poses(s).items()}
    TABLE[(gap, dist, roll)] = (s["camera"]["pitch_deg"], out)
    return out


def width(r):
    return r["band_cm"][1] - r["band_cm"][0]


def inside(r, gap):
    return r["band_cm"] is not None and r["band_cm"][0] <= gap <= r["band_cm"][1]


def test_ray_to_ground_inverts_the_synthetic_camera():
    for pitch, roll in [(15.0, 0.0), (22.0, 3.0), (8.0, -2.0)]:
        cam = camera(hfov_deg=HFOV, pitch_deg=pitch, roll_deg=roll)          # yawed 4 deg, 5 cm off-centre
        P = np.array([[-0.45, 3.0, 0.0], [0.45, 3.0, 0.0], [0.2, 6.0, 0.0]])
        g, ok = geometry.ray_to_ground(project(cam, P), SIZE, cam["f"], pitch, roll, 0.75)
        assert ok.all()
        d_img = np.hypot(*(g[1] - g[0])), np.hypot(*(g[2] - g[0]))
        d_true = 100 * np.hypot(*(P[1, :2] - P[0, :2])), 100 * np.hypot(*(P[2, :2] - P[0, :2]))
        assert np.allclose(d_img, d_true, atol=1e-6), (d_img, d_true)


@pytest.mark.parametrize("gap,dist,roll", SCENES)
def test_a_default_ranges_contain_the_truth(gap, dist, roll):
    r = measured(gap, dist, roll)
    for k in ("default", "default+lines"):
        assert inside(r[k], gap), (k, r[k]["band_cm"])
        assert r[k]["verdict"] == geometry.verdict_for(*r[k]["band_cm"])
    d = r["default"]["detail"]["pose"]
    assert d["pitch_source"] == "prior" and d["height_range_m"] == [0.65, 0.90]
    assert "assumed anywhere" in r["default"]["why"] and "no ground lines" in r["default"]["why"]
    s = scene(gap, dist, roll)
    dl = r["default+lines"]["detail"]
    assert dl["pose"]["pitch_source"] == "ground lines"
    assert abs(dl["pose"]["pitch_deg"] - s["camera"]["pitch_deg"]) < 0.5 + abs(roll) * 0.1
    assert dl["pose"]["pitch_sd_deg"] >= geometry.LINE_PITCH_SD_FLOOR_DEG
    assert width(r["default+lines"]) < 0.5 * width(r["default"])


@pytest.mark.parametrize("gap,dist,roll", SCENES)
def test_b_sensor_tilt_is_much_narrower(gap, dist, roll):
    r = measured(gap, dist, roll)
    assert inside(r["sensor"], gap), r["sensor"]["band_cm"]
    assert width(r["sensor"]) < 0.25 * width(r["default"])
    assert "phone sensor" in r["sensor"]["why"]


@pytest.mark.parametrize("gap,dist,roll", SCENES)
def test_c_calibrated_height_is_narrower_still(gap, dist, roll):
    r = measured(gap, dist, roll)
    assert inside(r["calibrated"], gap), r["calibrated"]["band_cm"]
    assert width(r["calibrated"]) < width(r["sensor"])
    assert r["calibrated"]["detail"]["error_sources_cm"]["height"] < r["sensor"]["detail"]["error_sources_cm"]["height"]
    assert "largest error source: " + r["calibrated"]["detail"]["dominant_error"] in r["calibrated"]["why"]


BIASED_LINES = [
    # lines turned 2 deg apart from each other (a path that narrows): the vanishing point is biased by about 2 deg
    ("splayed 2 deg, 4 lines", LINES, (("splay_deg", 2.0),)),
    # one curb: two close, ragged, short edges -- nearly parallel lines
    ("near-parallel ragged pair", (0.55, 0.70), (("rough_px", 3), ("far_m", 4.0))),
    ("splayed 3 deg, 2 ragged lines", (-0.9, 0.9), (("splay_deg", 3.0), ("rough_px", 2))),
]


@pytest.mark.parametrize("name,lines,kw", BIASED_LINES)
@pytest.mark.parametrize("gap,dist", [(90, 3.0), (75, 2.5)])
def test_d_biased_line_tilt_stays_honest(gap, dist, name, lines, kw):
    s = scene(gap, dist, 0.0, lines, kw)
    pose = {**calibration.camera_pose({}, None, {}, SIZE), "ground_lines": s["line_masks"]}
    r = geometry.measure_gap(pose, s["barrier_masks"], SIZE)
    assert inside(r, gap) or r["verdict"] in ("tight", "unknown"), (name, r["band_cm"], r["verdict"])
    if r["verdict"] != "unknown":
        p = r["detail"]["pose"]
        assert p["pitch_sd_deg"] >= geometry.LINE_PITCH_SD_FLOOR_DEG
        TABLE.setdefault("biased", []).append((name, gap, dist, s["camera"]["pitch_deg"], p, r))


def test_d_biased_explicit_segments():
    """Explicit line segments, one end of each moved 4 px inward (lines converge too fast: tilt biased)."""
    s = scene(90, 3.0, 0.0)
    cam = camera(hfov_deg=HFOV, pitch_deg=s["camera"]["pitch_deg"])
    segs = []
    for x in (-0.9, 0.9):
        p = project(cam, np.array([[x, 1.2, 0.0], [x, 7.0, 0.0]]))
        p[1, 0] -= 4.0 * np.sign(x)
        segs.append(p.tolist())
    r = geometry.measure_gap({**calibration.camera_pose({}, None, {}, SIZE), "ground_lines": segs},
                             s["barrier_masks"], SIZE)
    assert r["detail"]["pose"]["pitch_source"] == "ground lines"
    assert inside(r, 90) or r["verdict"] in ("tight", "unknown")


def test_a_small_fixed_sd_on_a_wrong_tilt_misses_the_truth():
    """Why the sd must be honest: a pitch 3 deg off quoted at +-0.3 deg gives a narrow band that misses."""
    s = scene(90, 3.0, 0.0)
    t = s["camera"]
    pose = {"kind": "camera_pose", "height_m": 0.75, "height_range_m": [0.74, 0.76], "hfov_deg": HFOV,
            "hfov_range_deg": [HFOV - 0.5, HFOV + 0.5], "pitch_deg": t["pitch_deg"] + 3.0, "pitch_sd_deg": 0.3,
            "roll_deg": 0.0, "roll_sd_deg": 0.3, "source": "test"}
    r = geometry.measure_gap(pose, s["barrier_masks"], SIZE)
    assert not inside(r, 90), r["band_cm"]


def test_unusable_lines_fall_back_to_the_prior_and_say_why():
    s = scene(90, 3.0, 0.0)
    flat = np.zeros(SIZE[::-1], bool)
    flat[600:604, 100:1200] = True                       # a line ACROSS the view: no rows to fit
    r = geometry.measure_gap({**calibration.camera_pose({}, None, {}, SIZE), "ground_lines": [flat]},
                             s["barrier_masks"], SIZE)
    gl = r["detail"]["ground_lines"]
    assert r["detail"]["pose"]["pitch_source"] == "prior" and gl["used"] is False and gl["skipped"] == 1
    assert inside(r, 90) and "assumed anywhere" in r["why"]


def test_pose_needs_two_barriers_and_a_height():
    s = scene(90, 3.0, 0.0)
    p = calibration.camera_pose({}, None, {}, SIZE)
    assert geometry.measure_gap(p, s["barrier_masks"][:1], SIZE)["verdict"] == "unknown"
    assert geometry.measure_gap({**p, "height_m": None}, s["barrier_masks"], SIZE)["verdict"] == "unknown"


def teardown_module(module):
    rows = [(k, v) for k, v in TABLE.items() if k != "biased"]
    if rows:
        print("\ntrue_cm dist_m roll  pitch | band width cm: default  default+lines  sensor+-1.5  calibrated+-3cm"
              " | bands")
        for (gap, dist, roll), (pitch, r) in sorted(rows):
            ws = "  ".join(f"{width(r[k]):6.1f}" for k in ("default", "default+lines", "sensor", "calibrated"))
            bands = "  ".join(f"{k}:{r[k]['band_cm'][0]:.0f}-{r[k]['band_cm'][1]:.0f}"
                              for k in ("default", "default+lines", "sensor", "calibrated"))
            print(f"{gap:7d} {dist:6.1f} {roll:4.1f} {pitch:6.2f} | {ws} | {bands}")
    if TABLE.get("biased"):
        print("\nbiased lines                    true_cm dist  true_pitch  est_pitch  sd   band       verdict")
        for name, gap, dist, tp, p, r in TABLE["biased"]:
            print(f"{name:31s} {gap:7d} {dist:4.1f} {tp:10.2f} {p['pitch_deg']:10.2f} {p['pitch_sd_deg']:4.2f} "
                  f"{r['band_cm'][0]:.0f}-{r['band_cm'][1]:.0f}  {r['verdict']}")
