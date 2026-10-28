"""Synthetic test scene for app/geometry.py: a pinhole camera 0.75 m above a flat sidewalk, looking forward-down,
a reference rectangle on the ground, and two box barriers with a known clear gap between them.

Masks are rasterised exactly (a pixel is in when its centre is inside the projected shape), the same convention as
app/sam.py, so the only measurement error comes from pixels.

    .venv/bin/python scripts/synth_scene.py --gap 90 --dist 3 --ref a4x4 --out /path/to/dir
writes scene.json there: the reference and barrier masks as SAM-style objects (mask_rle) plus the ground truth.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REFS = {"a4x4": (0.420, 0.594), "block": (0.30, 0.30)}  # metres, [across, along]


def camera(width=1280, height=720, hfov_deg=65.0, cam_h=0.75, cam_x=0.05, pitch_deg=20.0, yaw_deg=4.0, roll_deg=0.0):
    f = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
    th, ps = math.radians(pitch_deg), math.radians(yaw_deg)
    r = np.array([1.0, 0.0, 0.0])
    d = np.array([0.0, math.cos(th), -math.sin(th)])
    u = np.cross(d, r)  # image down
    rz = np.array([[math.cos(ps), -math.sin(ps), 0], [math.sin(ps), math.cos(ps), 0], [0, 0, 1]])
    R = np.vstack([rz @ r, rz @ u, rz @ d])
    if roll_deg:
        c, s = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ R  # rotate about the optical axis
    return {"f": f, "R": R, "C": np.array([cam_x, 0.0, cam_h]), "w": width, "h": height}


def project(cam, P) -> np.ndarray:
    pc = (np.asarray(P, float) - cam["C"]) @ cam["R"].T
    if np.any(pc[:, 2] <= 1e-6):
        raise ValueError("point behind the camera")
    return np.column_stack([cam["f"] * pc[:, 0] / pc[:, 2] + cam["w"] / 2,
                            cam["f"] * pc[:, 1] / pc[:, 2] + cam["h"] / 2])


def raster_convex(cam, pts2d) -> np.ndarray:
    idx = cv2.convexHull(pts2d.astype(np.float32).reshape(-1, 1, 2), returnPoints=False).ravel()
    hull = pts2d[idx].astype(np.float64)  # float32 only to pick the hull; the edges use the exact points
    xs = np.arange(cam["w"]) + 0.5
    ys = np.arange(cam["h"]) + 0.5
    X, Y = np.meshgrid(xs, ys)
    inside = np.ones((cam["h"], cam["w"]), bool)
    area = 0.5 * np.sum(hull[:, 0] * np.roll(hull[:, 1], -1) - np.roll(hull[:, 0], -1) * hull[:, 1])
    s = 1.0 if area > 0 else -1.0
    for p, q in zip(hull, np.roll(hull, -1, axis=0)):
        inside &= s * ((q[0] - p[0]) * (Y - p[1]) - (q[1] - p[1]) * (X - p[0])) >= 0
    return inside


def box_corners(x0, x1, y0, y1, h):
    return np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (0.0, h)])


def render_scene(gap_cm: float, dist_m: float, ref: str = "a4x4", box_w=0.40, box_d=0.40, box_h=0.80,
                 ref_ahead_m=0.15, **cam_kw) -> dict:
    """Barriers' near faces at Y = dist_m, inner faces at X = -gap/2 and +gap/2. Reference centred on the gap,
    its far edge ref_ahead_m in front of the barriers. The camera pitches to frame the reference and the barriers.
    ref="strip" lays a 30 cm tactile strip along the walkway instead (see render_strip_scene)."""
    if ref == "strip":
        return render_strip_scene(gap_cm, dist_m, box_w=box_w, box_d=box_d, box_h=box_h, **cam_kw)
    g = gap_cm / 100.0
    ra, rl = REFS[ref]
    ref_far = dist_m - ref_ahead_m
    ref_near = ref_far - rl
    cam_h = cam_kw.get("cam_h", 0.75)
    mid = (ref_near + dist_m + box_d) / 2
    cam_kw.setdefault("pitch_deg", math.degrees(math.atan2(cam_h, mid)))
    cam = camera(**cam_kw)
    ref_pts = np.array([[-ra / 2, ref_far, 0], [ra / 2, ref_far, 0], [ra / 2, ref_near, 0], [-ra / 2, ref_near, 0]])
    ref_img = project(cam, ref_pts)
    ref_mask = raster_convex(cam, ref_img)
    left = box_corners(-g / 2 - box_w, -g / 2, dist_m, dist_m + box_d, box_h)
    right = box_corners(g / 2, g / 2 + box_w, dist_m, dist_m + box_d, box_h)
    barriers = [raster_convex(cam, project(cam, b)) for b in (left, right)]
    return {"frame_size": (cam["w"], cam["h"]), "ref_mask": ref_mask, "ref_corners_true": ref_img,
            "ref_kind": ref, "barrier_masks": barriers, "true_gap_cm": gap_cm, "dist_m": dist_m,
            "pitch_deg": cam_kw["pitch_deg"]}


def render_strip_scene(gap_cm: float, dist_m: float, strip_x_m: float = 0.0, strip_w_m: float = 0.30,
                       strip_near_m: float = 0.3, strip_far_m: float | None = None, box_w=0.40, box_d=0.40,
                       box_h=0.80, **cam_kw) -> dict:
    """Same two barriers as render_scene; the reference is a strip_w_m wide strip centred at X = strip_x_m running
    from Y = strip_near_m (usually below the frame) to strip_far_m (default: 1 m past the barriers). The barriers
    occlude the strip where they stand in front of it, as they would in a real frame."""
    g = gap_cm / 100.0
    far = dist_m + 1.0 if strip_far_m is None else strip_far_m
    cam_h = cam_kw.get("cam_h", 0.75)
    cam_kw.setdefault("pitch_deg", math.degrees(math.atan2(cam_h, (1.0 + dist_m + box_d) / 2)))
    cam = camera(**cam_kw)
    x0, x1 = strip_x_m - strip_w_m / 2, strip_x_m + strip_w_m / 2
    strip_pts = np.array([[x0, far, 0], [x1, far, 0], [x1, strip_near_m, 0], [x0, strip_near_m, 0]])
    left = box_corners(-g / 2 - box_w, -g / 2, dist_m, dist_m + box_d, box_h)
    right = box_corners(g / 2, g / 2 + box_w, dist_m, dist_m + box_d, box_h)
    barriers = [raster_convex(cam, project(cam, b)) for b in (left, right)]
    strip = raster_convex(cam, project(cam, strip_pts)) & ~(barriers[0] | barriers[1])
    return {"frame_size": (cam["w"], cam["h"]), "ref_mask": strip, "ref_kind": "strip", "strip_width_cm":
            strip_w_m * 100, "barrier_masks": barriers, "true_gap_cm": gap_cm, "dist_m": dist_m,
            "pitch_deg": cam_kw["pitch_deg"], "strip_far_m": far}


def ground_ref_for(scene: dict, use_mask: bool = True) -> dict:
    if scene["ref_kind"] == "strip":
        return {"kind": "tactile_strip", "width_cm": scene["strip_width_cm"], "mask": scene["ref_mask"]}
    ref ={"kind": "a4x4"} if scene["ref_kind"] == "a4x4" else {"kind": "tactile_block", "n_blocks": 1}
    if use_mask:
        ref["mask"] = scene["ref_mask"]
    else:
        ref["corners"] = scene["ref_corners_true"].tolist()
    return ref


def main() -> None:
    from app.sam import encode_rle
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gap", type=float, default=90.0, help="true clear gap, cm")
    ap.add_argument("--dist", type=float, default=3.0, help="camera to barrier near faces, m")
    ap.add_argument("--ref", choices=sorted(REFS) + ["strip"], default="a4x4")
    ap.add_argument("--out", required=True, help="directory for scene.json")
    a = ap.parse_args()
    s = render_scene(a.gap, a.dist, a.ref)
    w, h = s["frame_size"]
    doc = {"width": w, "height": h, "truth": {"gap_cm": a.gap, "dist_m": a.dist, "ref": a.ref,
                                              "pitch_deg": round(s["pitch_deg"], 2)},
           "reference": {"id": 0, "score": 1.0, "mask_rle": encode_rle(s["ref_mask"])},
           "barriers": [{"id": k + 1, "score": 1.0, "mask_rle": encode_rle(m)} for k, m in enumerate(s["barrier_masks"])]}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scene.json").write_text(json.dumps(doc))
    print(f"wrote {out / 'scene.json'} ({w}x{h}, gap {a.gap} cm at {a.dist} m, ref {a.ref})")


if __name__ == "__main__":
    main()
