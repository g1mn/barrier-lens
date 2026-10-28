"""Glue: two segment_video results (barriers + ground reference) -> geometry.measure_gap.

measure_gap already takes SAM objects ({"mask_rle"} or {"polygon"}) as masks, so the only work here is picking one
frame where both concepts were found and checking that both results describe the same frame size.

No reference in view (the reference concept matched nothing, or never in a frame with barriers): the gap is measured
from the camera pose instead (geometry kind "camera_pose"), with the pose dict from app/calibration.camera_pose for
the user's profile, or that function's defaults when none is given. Ground-line masks (curb edges, paving joints;
an optional third SAM result) on the chosen frame go along so geometry can estimate the tilt from them.
"""
from __future__ import annotations

from app import geometry


def _frames_with_objects(result: dict | None) -> dict[int, list]:
    return {fr["i"]: fr["objects"] for fr in (result or {}).get("frames", []) if fr.get("objects")}


def pick_frame(barrier_result: dict, ref_result: dict) -> int | None:
    """Shared frame with the most barrier objects (ties: earliest). None if the concepts never share a frame."""
    bars, refs = _frames_with_objects(barrier_result), _frames_with_objects(ref_result)
    shared = sorted(set(bars) & set(refs))
    if not shared:
        return None
    return max(shared, key=lambda i: (len(bars[i]), -i))


def measure_with_pose(barrier_result: dict, pose: dict | None = None, ground_lines_result: dict | None = None,
                      frame_index: int | None = None, reason: str = "", **measure_kw) -> dict:
    """camera_pose measurement on the frame with the most barrier objects (or frame_index).

    pose: a calibration.camera_pose dict; None -> calibration.camera_pose({}, None, {}, size) (default assumptions).
    ground_lines_result: SAM-shaped result of a ground-line concept; its objects on the chosen frame are passed as
    ground_lines unless pose already has ground_lines.
    """
    size = (barrier_result.get("width"), barrier_result.get("height"))
    if None in size:
        return geometry._unknown("SAM result has no frame size (zero matches or empty replay)")
    bars = _frames_with_objects(barrier_result)
    if not bars:
        return geometry._unknown("no barrier masks to measure")
    i = max(bars, key=lambda k: (len(bars[k]), -k)) if frame_index is None else frame_index
    if pose is None:
        from app import calibration  # lazy: calibration is only needed for the default pose
        pose = calibration.camera_pose({}, None, {}, size)
    pose = dict(pose)
    n_lines = 0
    if ground_lines_result is not None and not pose.get("ground_lines"):
        g_size = (ground_lines_result.get("width"), ground_lines_result.get("height"))
        lines = _frames_with_objects(ground_lines_result).get(i, []) if g_size == size else []
        if lines:
            pose["ground_lines"] = lines
            n_lines = len(lines)
    out = geometry.measure_gap(pose, bars.get(i, []), size, **measure_kw)
    out.setdefault("detail", {})
    out["detail"].update({"frame": i, "barrier_ids": [o.get("id") for o in bars.get(i, [])],
                          "fallback": "camera_pose", "fallback_reason": reason, "ground_line_objects": n_lines})
    return out


def measure_from_sam(barrier_result: dict, ref_result: dict | None, ref_kind: dict, frame_index: int | None = None,
                     *, pose: dict | None = None, ground_lines_result: dict | None = None,
                     fallback_pose: bool = True, **measure_kw) -> dict:
    """Measure the gap on one frame.

    ref_kind describes the reference size, e.g. {"kind": "a4x4"} or {"kind": "tactile_block", "n_blocks": 2}.
    The reference object is the highest-score one in that frame. Extra keywords go to measure_gap.
    If no reference object is found with the barriers, falls back to the camera pose (see the module docstring);
    fallback_pose=False returns "unknown" instead.
    """
    size_b = (barrier_result.get("width"), barrier_result.get("height"))
    size_r = ((ref_result or {}).get("width"), (ref_result or {}).get("height"))
    if None in size_b:
        return geometry._unknown("SAM result has no frame size (zero matches or empty replay)")
    has_ref = bool(_frames_with_objects(ref_result))
    if has_ref and size_b != size_r:
        return geometry._unknown(f"barrier and reference results differ in frame size: {size_b} vs {size_r}")

    def fallback(why: str) -> dict:
        if not fallback_pose:
            return geometry._unknown(why)
        return measure_with_pose(barrier_result, pose, ground_lines_result, frame_index, reason=why, **measure_kw)

    if not has_ref:
        return fallback("no reference of known size in the clip")
    i = pick_frame(barrier_result, ref_result) if frame_index is None else frame_index
    if i is None:
        return fallback("barriers and reference are never visible in the same frame")
    refs = _frames_with_objects(ref_result).get(i)
    bars = _frames_with_objects(barrier_result).get(i, [])
    if not refs:
        return fallback(f"reference not found in frame {i}")
    ref_obj = max(refs, key=lambda o: o.get("score") or 0.0)
    out = geometry.measure_gap({**ref_kind, "mask": ref_obj}, bars, size_b, **measure_kw)
    out.setdefault("detail", {})
    out["detail"].update({"frame": i, "ref_object_id": ref_obj.get("id"), "barrier_ids": [o.get("id") for o in bars]})
    return out
