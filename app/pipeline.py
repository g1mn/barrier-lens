"""Glue: two segment_video results (barriers + ground reference) -> geometry.measure_gap.

measure_gap already takes SAM objects ({"mask_rle"} or {"polygon"}) as masks, so the only work here is picking one
frame where both concepts were found and checking that both results describe the same frame size.
"""
from __future__ import annotations

from app import geometry


def _frames_with_objects(result: dict) -> dict[int, list]:
    return {fr["i"]: fr["objects"] for fr in result.get("frames", []) if fr.get("objects")}


def pick_frame(barrier_result: dict, ref_result: dict) -> int | None:
    """Shared frame with the most barrier objects (ties: earliest). None if the concepts never share a frame."""
    bars, refs = _frames_with_objects(barrier_result), _frames_with_objects(ref_result)
    shared = sorted(set(bars) & set(refs))
    if not shared:
        return None
    return max(shared, key=lambda i: (len(bars[i]), -i))


def measure_from_sam(barrier_result: dict, ref_result: dict, ref_kind: dict, frame_index: int | None = None,
                     **measure_kw) -> dict:
    """Measure the gap on one frame.

    ref_kind describes the reference size, e.g. {"kind": "a4x4"} or {"kind": "tactile_block", "n_blocks": 2}.
    The reference object is the highest-score one in that frame. Extra keywords go to measure_gap.
    """
    size_b = (barrier_result.get("width"), barrier_result.get("height"))
    size_r = (ref_result.get("width"), ref_result.get("height"))
    if None in size_b or None in size_r:
        return geometry._unknown("SAM result has no frame size (zero matches or empty replay)")
    if size_b != size_r:
        return geometry._unknown(f"barrier and reference results differ in frame size: {size_b} vs {size_r}")
    i = pick_frame(barrier_result, ref_result) if frame_index is None else frame_index
    if i is None:
        return geometry._unknown("barriers and reference are never visible in the same frame")
    refs = _frames_with_objects(ref_result).get(i)
    bars = _frames_with_objects(barrier_result).get(i, [])
    if not refs:
        return geometry._unknown(f"reference not found in frame {i}")
    ref_obj = max(refs, key=lambda o: o.get("score") or 0.0)
    out = geometry.measure_gap({**ref_kind, "mask": ref_obj}, bars, size_b, **measure_kw)
    out.setdefault("detail", {})
    out["detail"].update({"frame": i, "ref_object_id": ref_obj.get("id"), "barrier_ids": [o.get("id") for o in bars]})
    return out
