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


def test_no_shared_frame_or_size_mismatch_is_unknown():
    s = render_scene(90, 3.0, "a4x4")
    bar, ref = _results(s, ref_frames=(0,), bar_frames=(2,))
    assert pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})["verdict"] == "unknown"
    bar, ref = _results(s)
    ref["width"] = 640
    r = pipeline.measure_from_sam(bar, ref, {"kind": "a4x4"})
    assert r["verdict"] == "unknown" and "frame size" in r["why"]
