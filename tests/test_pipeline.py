"""SAM-shaped results for two concepts -> app.pipeline.measure_from_sam -> geometry, with no hand reshaping."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import pipeline  # noqa: E402
from app.sam import encode_rle  # noqa: E402
from synth_scene import render_scene  # noqa: E402


def _results(s, ref_frames=(0, 1), bar_frames=(1, 2)):
    w, h = s["frame_size"]
    bars = [{"id": k, "score": 0.8, "mask_rle": encode_rle(m)} for k, m in enumerate(s["barrier_masks"])]
    ref = [{"id": 0, "score": 0.9, "mask_rle": encode_rle(s["ref_mask"])}]
    base = {"fps": 10, "width": w, "height": h, "source": "replay"}
    return ({**base, "concept": "bollard", "frames": [{"i": i, "objects": bars} for i in bar_frames]},
            {**base, "concept": "paper", "frames": [{"i": i, "objects": ref} for i in ref_frames]})


def test_two_concepts_feed_measure_gap():
    s = render_scene(90, 3.0, "a4x4")
    bar, ref = _results(s)
    r = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})
    assert r["detail"]["frame"] == 1
    lo, hi = r["band_cm"]
    assert lo <= 90 <= hi and r["verdict"] == "passes"


def test_no_shared_frame_falls_back_to_camera_pose_or_size_mismatch_is_unknown():
    s = render_scene(90, 3.0, "a4x4")
    bar, ref = _results(s, ref_frames=(0,), bar_frames=(2,))
    # no reference with the barriers: measured from the default camera pose instead of refusing
    r = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})
    assert r["detail"]["fallback"] == "camera_pose" and "same frame" in r["detail"]["fallback_reason"]
    assert r["band_cm"][0] <= 90 <= r["band_cm"][1]
    assert pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"}, fallback_pose=False)["verdict"] == "unknown"
    bar, ref = _results(s)
    ref["width"] = 640
    r = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})
    assert r["verdict"] == "unknown" and "frame size" in r["why"]


def test_no_reference_uses_given_pose_and_ground_line_masks():
    from app import calibration
    s = render_scene(90, 3.0, "none", lines_x_m=(-1.0, -0.45, 0.6, 1.1),
                     hfov_deg=calibration.hfov_from_f35(26.0, (1280, 720)))
    w, h = s["frame_size"]
    bars = [{"id": k, "score": 0.8, "mask_rle": encode_rle(m)} for k, m in enumerate(s["barrier_masks"])]
    lines = [{"id": k, "score": 0.7, "mask_rle": encode_rle(m)} for k, m in enumerate(s["line_masks"])]
    base = {"fps": 10, "width": w, "height": h, "source": "replay"}
    bar = {**base, "concept": "bollard", "frames": [{"i": 3, "objects": bars}]}
    ref = {"concept": "paper", "width": None, "height": None, "frames": []}   # SAM found no reference
    gl = {**base, "concept": "curb", "frames": [{"i": 3, "objects": lines}]}
    pose = calibration.camera_pose({}, None, {}, (w, h))
    r = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"}, pose=pose, ground_lines_result=gl)
    d = r["detail"]
    assert d["frame"] == 3 and d["ground_line_objects"] == 4 and d["pose"]["pitch_source"] == "ground lines"
    assert abs(d["pose"]["pitch_deg"] - s["camera"]["pitch_deg"]) < 1.0
    assert r["band_cm"][0] <= 90 <= r["band_cm"][1]
    # without lines and without a pose: default assumptions, wide tilt prior, still contains the truth
    r0 = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})
    assert r0["detail"]["pose"]["pitch_source"] == "prior" and r0["band_cm"][0] <= 90 <= r0["band_cm"][1]
    assert r0["band_cm"][1] - r0["band_cm"][0] > r["band_cm"][1] - r["band_cm"][0]
