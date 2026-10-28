"""Ground-plane geometry: a clear gap in centimetres, with an error band, from masks plus a reference of known size.

Shared interface:
    measure_gap(ground_ref: dict, barrier_masks: list, frame_size: tuple) -> dict
    -> {"gap_cm": float|None, "band_cm": [lo, hi]|None, "verdict": "passes"|"tight"|"blocked"|"unknown", "why": str,
        ...extra keys: "detail"}

Conventions
- frame_size is (width, height) in pixels, as in app/sam.py. Continuous image coordinates put pixel (row i, col j)
  at [j, j+1] x [i, i+1], so its centre is (j + 0.5, i + 0.5) -- the same convention app/sam.py uses to fill polygons.
- Ground coordinates are centimetres on the reference rectangle's plane: X across the walking direction (left to
  right in the image), Y along it (away from the camera). The reference's near-left corner is the origin.
- The reference is a rectangle lying flat on the ground. Its image corners are ordered far-left, far-right,
  near-right, near-left; size_cm is [across, along], i.e. [far edge length, side edge length].

Reference kinds (ground_ref["kind"]):
    "tactile_block"  Korean tactile paving block, 30 x 30 cm; "n_blocks": n makes a strip of n blocks laid along the
                     walking direction (30 x 30n cm). "along": false lays the strip across instead.
    "a4x4"           four A4 sheets in a 2 x 2 square, portrait: 42.0 across x 59.4 along.
    "rect"           any measured rectangle; "size_cm": [across, along] is required.
The image side comes from "corners" (4 points) or "mask" (bool array or a SAM object with mask_rle/polygon).
    "tactile_strip"  a straight strip of known width ("width_cm", default 30) and unknown length, running along the
                     walkway into the distance; "mask" is required. See measure_gap_strip: it measures the clear
                     corridor ACROSS the walkway only (no along-walkway scale exists without a known length).
    "camera_pose"    no reference at all: camera height, field of view, pitch and roll (each with a range) from
                     app/calibration.camera_pose; optional "ground_lines" fix the pitch. See measure_gap_pose.

Gap: barrier ground contact = the lowest mask pixel in each column (its bottom edge), mapped to the ground. The
reported gap is the widest passage among neighbouring barriers (sorted left to right by ground X), each passage
measured as the shortest ground distance between the two contact sets -- the narrowest point a wheelchair must
squeeze through. walkway_edge (two image points on a curb or wall line) adds the passages between the outermost
barrier and that edge. Every candidate passage is listed in detail["gaps"].

Error band: Monte Carlo. Each draw moves every reference corner by N(0, (corner_px/2)^2) per axis (about +-corner_px at
2 sigma) and shifts each barrier's whole contact line by N(0, contact_px^2) per axis (mask edges err as a whole, not
pixel by pixel), plus per-point jitter. band_cm is the 2.5th..97.5th percentile, widened to include gap_cm.
A small or distant reference makes the homography sensitive to those pixels, so the band widens by itself.

Verdict for wheelchair width W (default 70 cm) and side clearance C (default 5 cm):
    passes  if lo >= W + 2C      blocked if hi < W      tight otherwise      unknown if no trustworthy measurement.
Why C = 5 cm: hands on the push rims stick out past the wheels, so a chair 70 cm wide needs a few centimetres each
side; 70 + 2 x 5 = 80 cm, close to the 32-inch (81.3 cm) minimum clear width at a point that the 2010 ADA Standards
use (sections 404.2.3 and 403.5.1, from memory -- not checked against the text in this repo). It is a parameter.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

A4X4_CM = (42.0, 59.4)
TACTILE_BLOCK_CM = 30.0
DEFAULT_WHEELCHAIR_CM = 70.0
DEFAULT_CLEARANCE_CM = 5.0
MIN_REF_SIDE_PX = 6.0      # below this, a side of the reference is too few pixels to locate corners
MIN_REF_AREA_PX = 150.0
MAX_BAD_DRAW_FRACTION = 0.05


# ---------------------------------------------------------------- reference

def ref_size_cm(ground_ref: dict) -> tuple[float, float]:
    """[across, along] in cm for a reference description."""
    kind = ground_ref.get("kind", "rect" if "size_cm" in ground_ref else "tactile_block")
    if "size_cm" in ground_ref:
        a, b = (float(v) for v in ground_ref["size_cm"])
        return a, b
    if kind == "a4x4":
        return A4X4_CM
    if kind == "tactile_block":
        n = int(ground_ref.get("n_blocks", 1))
        long_side = TACTILE_BLOCK_CM * n
        return (TACTILE_BLOCK_CM, long_side) if ground_ref.get("along", True) else (long_side, TACTILE_BLOCK_CM)
    raise ValueError(f"reference kind {kind!r} needs size_cm=[across, along]")


def order_corners(points) -> np.ndarray:
    """4 image points in any order -> far-left, far-right, near-right, near-left (far = smaller y)."""
    p = np.asarray(points, dtype=float).reshape(4, 2)
    by_y = p[np.argsort(p[:, 1], kind="stable")]
    far = by_y[:2][np.argsort(by_y[:2, 0])]
    near = by_y[2:][np.argsort(by_y[2:, 0])]
    return np.array([far[0], far[1], near[1], near[0]])


def _line_through(pts: np.ndarray) -> np.ndarray:
    vx, vy, x0, y0 = cv2.fitLine(pts.astype(np.float32), cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    n = np.array([-vy, vx], dtype=float)
    return np.array([n[0], n[1], -(n[0] * x0 + n[1] * y0)])  # a x + b y + c = 0, (a, b) unit


def _intersect(l1: np.ndarray, l2: np.ndarray) -> np.ndarray | None:
    p = np.cross(l1, l2)
    if abs(p[2]) < 1e-9:
        return None
    return p[:2] / p[2]


def find_reference_corners(mask) -> np.ndarray | None:
    """Bool mask of a flat rectangle seen in perspective -> 4 corners (far-left, far-right, near-right, near-left).

    Polygon approximation finds 4 vertices; each side is then refit as a line through the contour pixels between
    them (ends trimmed) and pushed out half a pixel, because boundary pixel centres sit inside the true edge. If no
    4-vertex approximation exists, falls back to cv2.minAreaRect. Returns None for an empty mask.
    """
    m = np.asarray(mask, dtype=np.uint8)
    if m.sum() == 0:
        return None
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cnt = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(cnt, True)
    approx = None
    for frac in (0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
        a = cv2.approxPolyDP(cnt, frac * peri, True)
        if len(a) == 4:
            approx = a
            break
    pts = cnt.reshape(-1, 2).astype(float) + 0.5  # pixel centres
    if approx is None or len(cnt) < 16:
        box = cv2.boxPoints(cv2.minAreaRect(cnt)) + 0.5
        return order_corners(box)
    # contour index of each approx vertex
    vidx = sorted(int(np.argmin(np.sum((cnt.reshape(-1, 2) - v.reshape(2)) ** 2, axis=1))) for v in approx)
    centre = pts.mean(axis=0)
    lines = []
    for k in range(4):
        s, e = vidx[k], vidx[(k + 1) % 4]
        seg = pts[s:e + 1] if e > s else np.vstack([pts[s:], pts[:e + 1]])
        trim = int(len(seg) * 0.15)
        core = seg[trim:len(seg) - trim] if len(seg) - 2 * trim >= 3 else seg
        if len(core) < 2:
            box = cv2.boxPoints(cv2.minAreaRect(cnt)) + 0.5
            return order_corners(box)
        ln = _line_through(core)
        if ln[0] * centre[0] + ln[1] * centre[1] + ln[2] > 0:  # orient normal outward
            ln = -ln
        ln[2] -= 0.5  # move the line 0.5 px outward
        lines.append(ln)
    corners = []
    for k in range(4):
        c = _intersect(lines[k - 1], lines[k])
        if c is None:
            box = cv2.boxPoints(cv2.minAreaRect(cnt)) + 0.5
            return order_corners(box)
        corners.append(c)
    return order_corners(np.array(corners))


def _as_mask(obj, frame_size) -> np.ndarray:
    if isinstance(obj, np.ndarray):
        return obj.astype(bool)
    from app.sam import decode_mask  # lazy: keep geometry importable without the API client
    return decode_mask(obj, frame_size)


def corners_from_sam(result: dict, frame_index: int, object_id: int | None = None) -> np.ndarray | None:
    """Reference corners from a segment_video result: one frame, one object (default: highest score)."""
    frame_size = (result["width"], result["height"])
    for fr in result.get("frames", []):
        if fr["i"] != frame_index or not fr.get("objects"):
            continue
        objs = fr["objects"]
        if object_id is not None:
            objs = [o for o in objs if o["id"] == object_id]
            if not objs:
                return None
        obj = max(objs, key=lambda o: o.get("score") or 0.0)
        return find_reference_corners(_as_mask(obj, frame_size))
    return None


def sam_frame_masks(result: dict, frame_index: int) -> list[np.ndarray]:
    """All object masks of one frame of a segment_video result (for barrier_masks)."""
    frame_size = (result["width"], result["height"])
    for fr in result.get("frames", []):
        if fr["i"] == frame_index:
            return [_as_mask(o, frame_size) for o in fr.get("objects", [])]
    return []


# ---------------------------------------------------------------- contacts and gaps

def contact_points(mask: np.ndarray, max_points: int = 160) -> np.ndarray:
    """Lowest mask pixel per column -> image points (x = column centre, y = bottom edge of that pixel).

    Columns whose lowest pixel touches the bottom of the frame are dropped (the object continues out of view).
    """
    m = np.asarray(mask, dtype=bool)
    h = m.shape[0]
    cols = np.flatnonzero(m.any(axis=0))
    if cols.size == 0:
        return np.empty((0, 2))
    lowest = h - 1 - np.argmax(m[::-1, cols], axis=0)
    keep = lowest < h - 1
    cols, lowest = cols[keep], lowest[keep]
    pts = np.column_stack([cols + 0.5, lowest + 1.0]).astype(float)
    if len(pts) > max_points:
        pts = pts[np.linspace(0, len(pts) - 1, max_points).round().astype(int)]
    return pts


def _to_ground(H: np.ndarray, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Image points -> ground cm, plus a bool array: True where the point is in front of the camera (w > 0)."""
    hom = np.column_stack([pts, np.ones(len(pts))]) @ H.T
    w = hom[:, 2]
    ok = w > 1e-12
    out = np.full((len(pts), 2), np.nan)
    out[ok] = hom[ok, :2] / w[ok, None]
    return out, ok


def _signed(H: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """A homography is defined up to scale, sign included; flip it so the reference corners get w > 0."""
    w = np.column_stack([corners, np.ones(len(corners))]) @ H[2]
    return -H if np.mean(w) < 0 else H


def _set_distance(a: np.ndarray, b: np.ndarray) -> float:
    d = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2))
    return float(d.min())


def _line_distance(a: np.ndarray, line_pts: np.ndarray) -> float:
    p, q = line_pts
    d = q - p
    n = np.array([-d[1], d[0]]) / (np.hypot(*d) + 1e-12)
    return float(np.abs((a - p) @ n).min())


def _gap_ends_px(label: str, contacts: list, labels: list, edge_img, to_ground) -> list | None:
    """Image points of the two ends of a measured passage at the nominal pose (for drawing a ruler on the frame).
    to_ground maps image points to ground points; contacts and edge are all in front of the horizon here."""
    try:
        a_lab, b_lab = label.split("|")
        sets = [edge_img if lab == "edge" else contacts[labels.index(lab)] for lab in (a_lab, b_lab)]
        if any(x is None or len(x) == 0 for x in sets):
            return None
        if "edge" in (a_lab, b_lab):  # sample the edge segment densely so the nearest point is a real image point
            e0, e1 = np.asarray(edge_img, float)
            dense = e0 + np.linspace(0.0, 1.0, 200)[:, None] * (e1 - e0)
            sets = [dense if lab == "edge" else st for lab, st in zip((a_lab, b_lab), sets)]
        ga, gb = to_ground(np.asarray(sets[0], float)), to_ground(np.asarray(sets[1], float))
        d = ((ga[:, None, :] - gb[None, :, :]) ** 2).sum(axis=2)
        i, j = np.unravel_index(int(np.argmin(d)), d.shape)
        return [np.round(sets[0][i], 1).tolist(), np.round(sets[1][j], 1).tolist()]
    except (ValueError, IndexError):
        return None


def _passages(ground_sets: list[np.ndarray], edge: np.ndarray | None, labels: list[str]):
    """Neighbour passages, left to right: [(label, distance)]."""
    order = np.argsort([np.median(g[:, 0]) for g in ground_sets])
    out = []
    for i, j in zip(order[:-1], order[1:]):
        out.append((f"{labels[i]}|{labels[j]}", _set_distance(ground_sets[i], ground_sets[j])))
    if edge is not None and len(ground_sets):
        side = "left" if np.median(edge[:, 0]) < np.median(ground_sets[order[0]][:, 0]) else "right"
        k = order[0] if side == "left" else order[-1]
        out.append((f"edge|{labels[k]}" if side == "left" else f"{labels[k]}|edge",
                    _line_distance(ground_sets[k], edge)))
    return out


def _unknown(why: str, **detail) -> dict:
    return {"gap_cm": None, "band_cm": None, "verdict": "unknown", "why": why, "detail": detail}


UNCERTAIN_BAND_CM = 40.0  # a band wider than this that straddles the chair width is reported as unknown


def verdict_for(lo: float, hi: float, wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM,
                clearance_cm: float = DEFAULT_CLEARANCE_CM) -> str:
    need = wheelchair_cm + 2 * clearance_cm
    if lo >= need:
        return "passes"
    if hi < wheelchair_cm:
        return "blocked"
    # ponytail: fixed width threshold; tune once real calibrated clips give an error distribution
    if lo < wheelchair_cm and hi >= need and hi - lo > UNCERTAIN_BAND_CM:
        return "unknown"  # the band spans both "blocked" and "passes": saying "tight" would be false comfort
    return "tight"


def middle_phrase(verdict: str, lo: float, hi: float, wheelchair_cm: float, need: float) -> str:
    """Wording for the two in-between verdicts, true whatever the point estimate is."""
    if verdict == "unknown":
        return (f"is too uncertain to decide: it runs from below the {wheelchair_cm:.0f} cm chair width to above "
                f"the {need:.0f} cm needed with clearance; calibrating or recording the phone tilt narrows it")
    return (f"overlaps the {wheelchair_cm:.0f} cm chair width or the {need:.0f} cm needed with clearance, "
            f"so it may or may not fit")


# ---------------------------------------------------------------- main entry

def measure_gap(ground_ref: dict, barrier_masks: list, frame_size: tuple, *,
                wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM, clearance_cm: float = DEFAULT_CLEARANCE_CM,
                walkway_edge=None, corner_px: float = 3.0, contact_px: float = 1.0,
                n_draws: int = 400, seed: int = 0) -> dict:
    """Clear gap between barriers on the ground plane, with a Monte Carlo band and a verdict. See module docstring."""
    W, Hpx = int(frame_size[0]), int(frame_size[1])
    if not ground_ref:
        return _unknown("no ground reference: a gap in centimetres needs a rectangle of known size on the ground")
    if ground_ref.get("kind") == "camera_pose":
        return measure_gap_pose(ground_ref, barrier_masks, frame_size, wheelchair_cm=wheelchair_cm,
                                clearance_cm=clearance_cm, walkway_edge=walkway_edge, contact_px=contact_px,
                                n_draws=n_draws, seed=seed)
    if ground_ref.get("kind") == "tactile_strip":
        return measure_gap_strip(ground_ref, barrier_masks, frame_size, wheelchair_cm=wheelchair_cm,
                                 clearance_cm=clearance_cm, walkway_edge=walkway_edge, contact_px=contact_px,
                                 n_draws=n_draws, seed=seed)

    # reference corners
    try:
        size = ref_size_cm(ground_ref)
    except ValueError as e:
        return _unknown(str(e))
    if ground_ref.get("corners") is not None:
        corners = order_corners(ground_ref["corners"])
    elif ground_ref.get("mask") is not None:
        corners = find_reference_corners(_as_mask(ground_ref["mask"], (W, Hpx)))
        if corners is None:
            return _unknown("reference mask is empty")
    else:
        return _unknown("ground reference has neither corners nor mask")

    sides = np.hypot(*(np.roll(corners, -1, axis=0) - corners).T)
    area = abs(cv2.contourArea(corners.astype(np.float32)))
    if sides.min() < MIN_REF_SIDE_PX or area < MIN_REF_AREA_PX:
        return _unknown(f"reference too small in the image: shortest side {sides.min():.1f} px, area {area:.0f} px^2 "
                        f"(need >= {MIN_REF_SIDE_PX:.0f} px and >= {MIN_REF_AREA_PX:.0f} px^2) -- film closer",
                        ref_px=corners.tolist())
    if not cv2.isContourConvex(corners.astype(np.float32).reshape(-1, 1, 2)):
        return _unknown("reference corners do not form a convex quadrilateral: homography ill-conditioned",
                        ref_px=corners.tolist())

    across, along = size
    ground_quad = np.array([[0, along], [across, along], [across, 0], [0, 0]], dtype=float)
    H, _ = cv2.findHomography(corners, ground_quad)
    if H is None or not np.all(np.isfinite(H)):
        return _unknown("homography failed (ill-conditioned reference corners)", ref_px=corners.tolist())
    H = _signed(H, corners)

    # barrier contacts
    masks = [_as_mask(m, (W, Hpx)) for m in barrier_masks]
    contacts, labels = [], []
    for k, m in enumerate(masks):
        c = contact_points(m)
        if len(c):
            contacts.append(c)
            labels.append(f"b{k}")
    edge_img = None if walkway_edge is None else np.asarray(walkway_edge, dtype=float).reshape(2, 2)
    if len(contacts) + (edge_img is not None) < 2:
        return _unknown("need two barriers with visible ground contact, or one barrier and a walkway edge",
                        n_barriers=len(contacts), ref_px=corners.tolist())

    def run(Hm, sets, edge):
        grounds = []
        for s in sets:
            g, ok = _to_ground(Hm, s)
            # a point within a pixel or two of the horizon can flip behind it in a perturbed draw; it lies far
            # out on its own side, so dropping it does not touch the gap
            if ok.sum() < max(1, len(s) // 2):
                return None
            grounds.append(g[ok])
        e = None
        if edge is not None:
            e, ok = _to_ground(Hm, edge)
            if not ok.all():
                return None
        return _passages(grounds, e, labels)

    # Column bottoms above the ground horizon belong to raised parts (a top that leans out past the base); drop them.
    dropped = 0
    for k, s in enumerate(contacts):
        _, ok = _to_ground(H, s)
        dropped += int((~ok).sum())
        contacts[k] = s[ok]
    if any(len(s) == 0 for s in contacts) or (edge_img is not None and not _to_ground(H, edge_img)[1].all()):
        return _unknown("barrier contact points lie above the reference plane's horizon: homography ill-conditioned "
                        "for this view (reference tilted or not on the same ground)", ref_px=corners.tolist())
    nominal = run(H, contacts, edge_img)
    best = int(np.argmax([d for _, d in nominal]))
    gap = nominal[best][1]
    gap_px = _gap_ends_px(nominal[best][0], contacts, labels, edge_img, lambda pts: _to_ground(H, pts)[0])

    rng = np.random.default_rng(seed)
    draws, bad = [], 0
    for _ in range(n_draws):
        cj = corners + rng.normal(0.0, corner_px / 2.0, corners.shape)
        Hj = _signed(cv2.getPerspectiveTransform(cj.astype(np.float32), ground_quad.astype(np.float32)), cj)
        sj = [s + rng.normal(0.0, contact_px, 2) + rng.normal(0.0, 0.5, s.shape) for s in contacts]
        ej = None if edge_img is None else edge_img + rng.normal(0.0, contact_px, edge_img.shape)
        r = run(Hj, sj, ej)
        if r is None or len(r) != len(nominal):
            bad += 1
            continue
        draws.append(r[best][1])
    if bad > MAX_BAD_DRAW_FRACTION * n_draws:
        return _unknown(f"homography ill-conditioned: {bad} of {n_draws} perturbed draws put contacts behind the "
                        f"horizon -- the reference ({sides.min():.0f} px shortest side) is too small or too far from the barriers; film closer or use a bigger reference (a block strip or four A4 sheets)", ref_px=corners.tolist())
    draws = np.asarray(draws)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    lo, hi = float(min(lo, gap)), float(max(hi, gap))
    verdict = verdict_for(lo, hi, wheelchair_cm, clearance_cm)
    need = wheelchair_cm + 2 * clearance_cm
    if verdict == "passes":
        why = f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) >= {need:.0f} cm needed"
    elif verdict == "blocked":
        why = f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) < {wheelchair_cm:.0f} cm chair width"
    else:
        why = (f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) "
               + middle_phrase(verdict, lo, hi, wheelchair_cm, need))
    return {
        "gap_cm": round(gap, 1), "band_cm": [round(lo, 1), round(hi, 1)], "verdict": verdict, "why": why,
        "detail": {"gaps": [{"between": lab, "cm": round(d, 1)} for lab, d in nominal], "chosen": nominal[best][0],
                   "gap_px": gap_px,
                   "ref_px": corners.round(2).tolist(), "ref_cm": [across, along], "ref_min_side_px":
                   round(float(sides.min()), 1), "draws": len(draws), "bad_draws": bad,
                   "dropped_contacts": dropped, "wheelchair_cm": wheelchair_cm, "clearance_cm": clearance_cm},
    }



# ---------------------------------------------------------------- tactile strip of known width (second reference mode)
#
# A straight strip of width w on the ground, running along the walkway (direction Y), seen in perspective: its two
# long edges are image lines that meet at the vanishing point V of the walkway direction. Every ground line parallel
# to the strip also passes through V, so the across-walkway coordinate X of a ground point depends only on WHICH line
# through V it lies on. Three lines of that pencil are known -- left edge (X = 0), right edge (X = w) and the horizon
# (X = infinity) -- which fixes X as a projective coordinate on the pencil:
#     X(p) = k * (L0 . p) / (Lh . p),   k chosen so that X = w on the right edge.
# The horizon is the one assumption: the camera is held level (no roll), so the horizon is the image row through V.
# Yaw (camera not square to the walkway) needs no assumption: X is measured across the walkway on the ground, not
# along an image row. The along-walkway scale is unknown (the strip's length is unknown), so the gap reported here is
# the clear corridor width across the walkway: min X of the right barrier's contact minus max X of the left one.
# For barriers side by side that is the narrowest point; for staggered barriers it is the straight-through corridor
# (a lower bound on the diagonal distance), which is what a wheelchair rolling along the walkway needs.
#
# s(y) = w / (x_right(y) - x_left(y)) is the local cm-per-pixel along image row y; it is reported in detail and used
# for a cross-check ("row_gap_cm") that is valid only when the camera is square to the walkway.

STRIP_WIDTH_CM = 30.0
MIN_STRIP_ROWS = 12          # usable mask rows needed to fit the two edges
MIN_STRIP_WIDTH_PX = 5.0     # strip width at the barriers' contact rows
STRIP_EXTRAP_REL = 0.05      # extra relative sigma per strip-span of extrapolation (heuristic: curvature, mask ends)


def _largest_component(m: np.ndarray) -> np.ndarray:
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    if n <= 2:
        return m
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return lab == k


def _robust_line(y: np.ndarray, x: np.ndarray, iters: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """Fit x = a*y + b, dropping residuals beyond max(3 sigma_MAD, 1 px) each round. Returns (a, b), inlier mask."""
    keep = np.ones(len(y), bool)
    for _ in range(iters):
        a, b = np.polyfit(y[keep], x[keep], 1)
        r = x - (a * y + b)
        sig = 1.4826 * np.median(np.abs(r[keep] - np.median(r[keep])))
        new = np.abs(r) <= max(3.0 * sig, 1.0)
        if new.sum() < 3 or np.array_equal(new, keep):
            break
        keep = new
    a, b = np.polyfit(y[keep], x[keep], 1)
    return np.array([a, b]), keep


def strip_edges(mask, frame_size=None) -> dict | None:
    """Bool mask (or SAM object) of a strip running away from the camera -> its two edge lines x = a*y + b.

    Per row, the leftmost pixel j gives the left edge at x = j and the rightmost pixel j gives the right edge at
    x = j + 1 (pixel-centre rasterisation: those are the unbiased edge positions). Rows clipped by the frame's side
    are dropped, as are the 5% of rows at each end of the strip (its end caps cut rows short); each edge is then fit
    with a robust line, so rows where a barrier hides part of the strip fall out as outliers.
    Returns None when there are fewer than MIN_STRIP_ROWS usable rows.
    """
    m = _as_mask(mask, frame_size) if frame_size is not None else np.asarray(mask, dtype=bool)
    if m.sum() == 0:
        return None
    m = _largest_component(m)
    W = m.shape[1]
    rows = np.flatnonzero(m.any(axis=1))
    left = np.argmax(m[rows], axis=1).astype(float)
    right = (W - 1 - np.argmax(m[rows, ::-1], axis=1)).astype(float) + 1.0
    ok = (left > 0) & (right < W)
    rows, left, right = rows[ok], left[ok], right[ok]
    if len(rows) < MIN_STRIP_ROWS:
        return None
    trim = max(2, int(round(0.05 * (rows[-1] - rows[0] + 1))))
    inner = (rows >= rows[0] + trim) & (rows <= rows[-1] - trim)
    rows, left, right = rows[inner], left[inner], right[inner]
    if len(rows) < MIN_STRIP_ROWS:
        return None
    y = rows + 0.5
    lf, lk = _robust_line(y, left)
    rf, rk = _robust_line(y, right)
    used = lk & rk
    if used.sum() < MIN_STRIP_ROWS:
        return None
    return {"left": lf, "right": rf, "y_top": float(y[used].min()), "y_bot": float(y[used].max()),
            "rows_used": int(used.sum()), "rows_seen": int(len(rows))}


def _line_h(ab: np.ndarray) -> np.ndarray:
    """x = a*y + b as a homogeneous line (1, -a, -b)."""
    return np.array([1.0, -ab[0], -ab[1]])


class _StripRuler:
    """Across-walkway coordinate X (cm) of image points, from the two edge lines and a level-camera horizon."""

    def __init__(self, left_ab, right_ab, width_cm, y_ref, roll_rad=0.0):
        self.L0, self.L1 = _line_h(left_ab), _line_h(right_ab)
        self.left_ab, self.right_ab = np.asarray(left_ab, float), np.asarray(right_ab, float)
        self.V = np.cross(self.L0, self.L1)  # vanishing point of the walkway direction (homogeneous)
        self.Lh = np.cross(self.V, np.array([math.cos(roll_rad), math.sin(roll_rad), 0.0]))
        q = np.array([right_ab[0] * y_ref + right_ab[1], y_ref, 1.0])  # a point on the right edge
        self.ground_sign = np.sign(self.Lh @ q)
        self.k = width_cm * (self.Lh @ q) / (self.L0 @ q)
        self.width_cm = width_cm

    def X(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        hom = np.column_stack([pts, np.ones(len(pts))])
        d = hom @ self.Lh
        ok = np.sign(d) == self.ground_sign
        ok &= np.abs(d) > 1e-9
        out = np.full(len(pts), np.nan)
        out[ok] = self.k * (hom[ok] @ self.L0) / d[ok]
        return out, ok

    def width_px(self, y):
        y = np.asarray(y, float)
        return (self.right_ab[0] - self.left_ab[0]) * y + (self.right_ab[1] - self.left_ab[1])

    def vanishing_point(self) -> list | None:
        return None if abs(self.V[2]) < 1e-12 else (self.V[:2] / self.V[2]).tolist()


def _strip_passages(xsets: list[np.ndarray], edge_x: np.ndarray | None, labels: list[str]):
    """Neighbour corridors across the walkway, left to right: [(label, cm)] (negative = overlapping, i.e. none)."""
    order = np.argsort([np.median(x) for x in xsets])
    out = [(f"{labels[i]}|{labels[j]}", float(xsets[j].min() - xsets[i].max())) for i, j in zip(order[:-1], order[1:])]
    if edge_x is not None and len(xsets):
        if np.median(edge_x) < np.median(xsets[order[0]]):
            k = order[0]
            out.append((f"edge|{labels[k]}", float(xsets[k].min() - edge_x.max())))
        else:
            k = order[-1]
            out.append((f"{labels[k]}|edge", float(edge_x.min() - xsets[k].max())))
    return out


def measure_gap_strip(ground_ref: dict, barrier_masks: list, frame_size: tuple, *,
                      wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM, clearance_cm: float = DEFAULT_CLEARANCE_CM,
                      walkway_edge=None, contact_px: float = 1.0, edge_px: float = 1.0, roll_deg: float = 1.0,
                      n_draws: int = 400, seed: int = 0) -> dict:
    """Clear corridor across the walkway, from a tactile strip of known width (see the section comment above).

    Monte Carlo per draw: each edge line is refit through its two end anchors (top and bottom used rows), each moved
    by N(0, edge_px^2) across; the horizon is tilted by N(0, roll_deg^2) about V (the level-camera assumption is not
    exact); each barrier's contact set shifts by N(0, contact_px^2) per axis. Contacts outside the strip's row range
    are extrapolated with the line model -- that is flagged in "why", and the draw gets an extra relative error of
    STRIP_EXTRAP_REL per strip-span of extrapolation on top of the line perturbation (which already grows with it).
    """
    W, Hpx = int(frame_size[0]), int(frame_size[1])
    width_cm = float(ground_ref.get("width_cm", STRIP_WIDTH_CM))
    if ground_ref.get("mask") is None:
        return _unknown("tactile_strip reference needs a mask of the strip")
    edges = strip_edges(_as_mask(ground_ref["mask"], (W, Hpx)))
    if edges is None:
        return _unknown(f"strip mask too short: fewer than {MIN_STRIP_ROWS} usable rows to fit its two edges -- "
                        "the strip must run into the distance and stay inside the frame's sides")
    y_top, y_bot = edges["y_top"], edges["y_bot"]
    span = y_bot - y_top
    ruler = _StripRuler(edges["left"], edges["right"], width_cm, (y_top + y_bot) / 2)
    vp = ruler.vanishing_point()
    if vp is None or vp[1] >= y_top or ruler.width_px(y_bot) <= ruler.width_px(y_top):
        return _unknown("strip edges do not converge away from the camera: cannot place the horizon "
                        "(strip not running along the walkway, or the mask is not a strip)",
                        strip_edges={"left": edges["left"].tolist(), "right": edges["right"].tolist()})

    masks = [_as_mask(m, (W, Hpx)) for m in barrier_masks]
    contacts, labels = [], []
    for k, m in enumerate(masks):
        c = contact_points(m)
        if len(c):
            contacts.append(c)
            labels.append(f"b{k}")
    edge_img = None if walkway_edge is None else np.asarray(walkway_edge, dtype=float).reshape(2, 2)
    if len(contacts) + (edge_img is not None) < 2:
        return _unknown("need two barriers with visible ground contact, or one barrier and a walkway edge",
                        n_barriers=len(contacts))
    edge_pts = None if edge_img is None else edge_img[0] + np.linspace(0, 1, 21)[:, None] * (edge_img[1] - edge_img[0])

    dropped = 0
    for k, s in enumerate(contacts):
        _, ok = ruler.X(s)
        dropped += int((~ok).sum())
        contacts[k] = s[ok]
    if any(len(s) == 0 for s in contacts) or (edge_pts is not None and not ruler.X(edge_pts)[1].all()):
        return _unknown("barrier contact points lie above the strip's horizon: the strip is not on the same ground")

    def run(r: _StripRuler, sets, epts):
        xs = []
        for s in sets:
            x, ok = r.X(s)
            if ok.sum() < max(1, len(s) // 2):
                return None
            xs.append(x[ok])
        ex = None
        if epts is not None:
            ex, ok = r.X(epts)
            if not ok.all():
                return None
        return _strip_passages(xs, ex, labels)

    nominal = run(ruler, contacts, edge_pts)
    best = int(np.argmax([d for _, d in nominal]))
    gap = nominal[best][1]

    # the pair (or barrier + edge) that makes the chosen corridor, and the image rows where it is pinched
    pair = nominal[best][0].split("|")
    pinch = []
    for side, lab in zip(("max", "min"), pair):
        if lab == "edge":
            continue
        s = contacts[labels.index(lab)]
        x = ruler.X(s)[0]
        pinch.append(s[int(np.nanargmax(x) if side == "max" else np.nanargmin(x))])
    pinch = np.array(pinch)
    rows_all = np.concatenate([s[:, 1] for s in contacts])
    extrap = float(max(0.0, y_top - rows_all.min(), rows_all.max() - y_bot))
    extrap_pinch = float(max(0.0, y_top - pinch[:, 1].min(), pinch[:, 1].max() - y_bot))
    if ruler.width_px(pinch[:, 1]).min() < MIN_STRIP_WIDTH_PX:
        return _unknown(f"strip only {ruler.width_px(pinch[:, 1]).min():.1f} px wide at the barriers' contact rows "
                        f"(need >= {MIN_STRIP_WIDTH_PX:.0f} px) -- film closer")

    rng = np.random.default_rng(seed)
    anchors = np.array([y_top, y_bot])
    xl = edges["left"][0] * anchors + edges["left"][1]
    xr = edges["right"][0] * anchors + edges["right"][1]
    ext_sig = STRIP_EXTRAP_REL * extrap_pinch / max(span, 1.0)
    draws, bad = [], 0
    for _ in range(n_draws):
        lj = np.polyfit(anchors, xl + rng.normal(0.0, edge_px, 2), 1)
        rj = np.polyfit(anchors, xr + rng.normal(0.0, edge_px, 2), 1)
        rr = _StripRuler(lj, rj, width_cm, (y_top + y_bot) / 2, math.radians(rng.normal(0.0, roll_deg)))
        sj = [s + rng.normal(0.0, contact_px, 2) for s in contacts]
        ej = None if edge_pts is None else edge_pts + rng.normal(0.0, contact_px, 2)
        r = run(rr, sj, ej)
        if r is None or len(r) != len(nominal):
            bad += 1
            continue
        draws.append(r[best][1] * (1.0 + rng.normal(0.0, ext_sig)) if ext_sig > 0 else r[best][1])
    if bad > MAX_BAD_DRAW_FRACTION * n_draws:
        return _unknown(f"strip ruler ill-conditioned: {bad} of {n_draws} perturbed draws put contacts behind the "
                        "horizon -- film closer or with more of the strip in view")
    lo, hi = np.percentile(np.asarray(draws), [2.5, 97.5])
    lo, hi = float(min(lo, gap)), float(max(hi, gap))
    verdict = verdict_for(lo, hi, wheelchair_cm, clearance_cm)
    need = wheelchair_cm + 2 * clearance_cm

    # cross-check along the image row (valid only when the camera is square to the walkway)
    row_gap = None
    if len(pinch) == 2:
        s_mid = width_cm / ruler.width_px(pinch[:, 1].mean())
        row_gap = float((pinch[1, 0] - pinch[0, 0]) * s_mid)
    vp_off = vp[0] - W / 2
    square = abs(vp_off) <= 0.02 * W

    if verdict == "passes":
        head = f"corridor {gap:.0f} cm across the walkway (95% band {lo:.0f}-{hi:.0f}) >= {need:.0f} cm needed"
    elif verdict == "blocked":
        head = f"corridor {gap:.0f} cm across the walkway (95% band {lo:.0f}-{hi:.0f}) < {wheelchair_cm:.0f} cm chair width"
    else:
        head = (f"corridor {gap:.0f} cm across the walkway (95% band {lo:.0f}-{hi:.0f}) "
                + middle_phrase(verdict, lo, hi, wheelchair_cm, need))
    why = [head, f"ruler: {width_cm:.0f} cm tactile strip, scale from its two edges converging at "
                 f"({vp[0]:.0f}, {vp[1]:.0f}); assumes a level camera (no roll, +-{roll_deg:g} deg in the band) so "
                 "the horizon is the image row through that point"]
    if square:
        why.append(f"camera roughly square to the walkway (vanishing point {vp_off:+.0f} px from centre), so "
                   "measuring along the image row is also valid"
                   + ("" if row_gap is None else f" (along-row estimate {row_gap:.0f} cm)"))
    else:
        why.append(f"camera not square to the walkway (vanishing point {vp_off:+.0f} px from centre): the gap is "
                   "measured across the walkway through the vanishing geometry, not along the image row"
                   + ("" if row_gap is None else f" (along-row estimate would say {row_gap:.0f} cm)"))
    if extrap_pinch > 0:
        why.append(f"EXTRAPOLATED: the pinch point is {extrap_pinch:.0f} px of rows outside the strip "
                   f"(strip covers rows {y_top:.0f}-{y_bot:.0f}); band widened")
    return {
        "gap_cm": round(gap, 1), "band_cm": [round(lo, 1), round(hi, 1)], "verdict": verdict, "why": "; ".join(why),
        "detail": {"gaps": [{"between": lab, "cm": round(d, 1)} for lab, d in nominal], "chosen": nominal[best][0],
                   "ref": "tactile_strip", "ref_cm": width_cm, "measure": "across-walkway corridor",
                   "strip_edges": {"left": edges["left"].round(5).tolist(), "right": edges["right"].round(5).tolist(),
                                   "rows": [round(y_top, 1), round(y_bot, 1)], "rows_used": edges["rows_used"],
                                   "rows_seen": edges["rows_seen"]},
                   "vanishing_point": [round(v, 1) for v in vp], "camera_square": bool(square),
                   "scale_cm_per_px": [[round(float(y), 1), round(float(width_cm / ruler.width_px(y)), 4)]
                                       for y in pinch[:, 1]],
                   "pinch_px": pinch.round(1).tolist(), "row_gap_cm": None if row_gap is None else round(row_gap, 1),
                   "gap_px": pinch.round(1).tolist() if len(pinch) == 2 else None,
                   "extrapolated_px": round(extrap, 1), "extrapolated_pinch_px": round(extrap_pinch, 1),
                   "draws": len(draws), "bad_draws": bad, "dropped_contacts": dropped,
                   "wheelchair_cm": wheelchair_cm, "clearance_cm": clearance_cm},
    }


# ---------------------------------------------------------------- camera pose: no reference in the clip (third mode)
#
# Without a reference, a ground point is where its image ray meets the ground plane. That needs the camera's height
# above the ground h, its focal length f (from the horizontal field of view, principal point at the frame centre), its
# pitch below the horizon and its roll. Yaw is not needed: distances on the ground do not depend on the heading.
#     pixel (u, v) -> centred (x', y') -> un-rolled (x, y) -> direction (x, -y sin p + f cos p, -y cos p - f sin p)
# in X right, Y forward, Z up; the ground point is t * direction with t = h / -direction_z (below the horizon only).
# Distances scale with h, so a height range is a proportional band; pitch moves the horizon and dominates for far
# contacts; detail["error_sources_cm"] measures each source alone.
#
# Pitch, best source first:
#   given     pitch_deg with pitch_sd_deg (phone motion sensor, calibration bias): N(pitch, sd).
#   lines     ground_ref["ground_lines"]: SAM masks (both long edges of each mask are fitted, as for the tactile strip)
#             or explicit segments [[x1, y1], [x2, y2]] of lines running ALONG the walkway (curb edges, paving joints,
#             painted lines). They meet at a vanishing point on the horizon; its un-rolled image row is -f tan(pitch).
#             The sd comes from the data: a bootstrap of the edge fits (rows resampled with replacement, line ends
#             jittered by LINE_ANCHOR_PX for mask-edge bias; segment ends by SEGMENT_END_PX), plus the leave-one-out
#             spread between sources when there are 3 or more, floored at LINE_PITCH_SD_FLOOR_DEG for what a bootstrap
#             cannot see (lines not quite parallel, a walkway that changes slope). Each Monte Carlo draw takes one
#             bootstrap vanishing point and converts it with that draw's f and roll, so the couplings are kept.
#   prior     no usable lines: uniform POSE_PITCH_PRIOR_DEG, truncated per draw so every barrier base stays at least
#             HORIZON_MARGIN_DEG below the horizon (the bases are visible on the ground, so the camera points below
#             them). gap_cm is then the median draw, not a single nominal pose.
# Roll: roll_deg with roll_sd_deg when given, else N(0, POSE_ROLL_PRIOR_SD_DEG) -- one family of parallel ground lines
# fixes the pitch but not the roll.

POSE_PITCH_PRIOR_DEG = (0.0, 30.0)
POSE_PITCH_SD_DEG = 3.0          # when pitch_deg comes without pitch_sd_deg
POSE_ROLL_PRIOR_SD_DEG = 3.0
LINE_PITCH_SD_FLOOR_DEG = 1.5
LINE_PITCH_SD_MAX_DEG = 10.0     # above this the lines say little; use the prior and say why
LINE_ANCHOR_PX = 1.0
SEGMENT_END_PX = 2.0
MIN_SEGMENT_PX = 20.0
PITCH_VALID_DEG = (-15.0, 85.0)
HORIZON_MARGIN_DEG = 1.0
MAX_BAD_POSE_FRACTION = 0.025
POSE_SENS_DRAWS = 150


def focal_px(hfov_deg: float, width_px: float) -> float:
    return (width_px / 2) / math.tan(math.radians(hfov_deg) / 2)


def ray_to_ground(pts, frame_size, f: float, pitch_deg: float, roll_deg: float, height_m: float):
    """Image points -> ground points in cm (X right, Y forward from the camera's foot), plus a bool array: True where
    the ray goes below the horizon. See the section comment for the conventions (pitch positive = down)."""
    W, H = frame_size
    p = np.asarray(pts, float).reshape(-1, 2)
    x0, y0 = p[:, 0] - W / 2, p[:, 1] - H / 2
    r, t = math.radians(roll_deg), math.radians(pitch_deg)
    x = math.cos(r) * x0 + math.sin(r) * y0
    y = -math.sin(r) * x0 + math.cos(r) * y0
    dy = -y * math.sin(t) + f * math.cos(t)
    dz = -y * math.cos(t) - f * math.sin(t)
    ok = dz < -1e-9 * f
    k = np.where(ok, height_m * 100.0 / np.where(ok, -dz, 1.0), np.nan)
    return np.column_stack([k * x, k * dy]), ok


def _edge_point_sets(m: np.ndarray) -> list[np.ndarray]:
    """Mask of something running along the walkway -> its left and right edges as (x, y) samples, one per usable row,
    robust-line inliers only. Same row scan as strip_edges."""
    if m.sum() == 0:
        return []
    m = _largest_component(m)
    W = m.shape[1]
    rows = np.flatnonzero(m.any(axis=1))
    left = np.argmax(m[rows], axis=1).astype(float)
    right = (W - 1 - np.argmax(m[rows, ::-1], axis=1)).astype(float) + 1.0
    ok = (left > 0) & (right < W)
    rows, left, right = rows[ok], left[ok], right[ok]
    if len(rows) < MIN_STRIP_ROWS:
        return []
    trim = max(2, int(round(0.05 * (rows[-1] - rows[0] + 1))))
    inner = (rows >= rows[0] + trim) & (rows <= rows[-1] - trim)
    rows, left, right = rows[inner], left[inner], right[inner]
    if len(rows) < MIN_STRIP_ROWS:
        return []
    y = rows + 0.5
    out = []
    for x in (left, right):
        _, keep = _robust_line(y, x)
        if keep.sum() >= MIN_STRIP_ROWS:
            out.append(np.column_stack([x[keep], y[keep]]))
    return out


def _is_segment(item) -> bool:
    if isinstance(item, dict):
        return False
    try:
        return np.asarray(item, dtype=float).shape == (2, 2)
    except (TypeError, ValueError):
        return False


def ground_line_sources(items, frame_size) -> tuple[list, int]:
    """ground_lines -> sources (one per mask or segment), each a list of ("pts", Nx2) edge samples or ("seg", 2x2)."""
    sources, skipped = [], 0
    for it in items or []:
        if _is_segment(it):
            p = np.asarray(it, dtype=float)
            if np.hypot(*(p[1] - p[0])) < MIN_SEGMENT_PX:
                skipped += 1
                continue
            sources.append([("seg", p)])
            continue
        try:
            m = _as_mask(it, frame_size)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        sets = _edge_point_sets(m)
        if sets:
            sources.append([("pts", s) for s in sets])
        else:
            skipped += 1
    return sources, skipped


def _fit_lines(sources, frame_size, rng=None) -> np.ndarray:
    """Homogeneous lines (unit normal) in centred, width-scaled coordinates; with rng, one bootstrap replicate."""
    W, H = frame_size
    c = np.array([W / 2, H / 2])
    out = []
    for src in sources:
        for kind, p in src:
            if kind == "seg":
                q = p + (rng.normal(0.0, SEGMENT_END_PX, p.shape) if rng is not None else 0.0)
                q = (q - c) / W
                ln = np.cross([q[0, 0], q[0, 1], 1.0], [q[1, 0], q[1, 1], 1.0])
            else:
                q = p[rng.integers(0, len(p), len(p))] if rng is not None else p
                q = (q - c) / W
                if np.ptp(q[:, 1]) <= 0:
                    continue
                a, b = np.polyfit(q[:, 1], q[:, 0], 1)
                if rng is not None:
                    ys = np.array([p[:, 1].min(), p[:, 1].max()])
                    ys = (ys - c[1]) / W
                    a, b = np.polyfit(ys, a * ys + b + rng.normal(0.0, LINE_ANCHOR_PX / W, 2), 1)
                ln = np.array([1.0, -a, -b])
            n = math.hypot(ln[0], ln[1])
            if n > 1e-12:
                out.append(ln / n)
    return np.array(out)


def _vanishing_point(lines: np.ndarray, ref: np.ndarray) -> np.ndarray | None:
    """Least-squares common point of the lines (homogeneous, so a point at infinity is fine), signed so that it lies
    on the far side of ref (the mean line sample), i.e. up the image."""
    if len(lines) < 2:
        return None
    v = np.linalg.svd(lines)[2][-1]
    return -v if ref[1] * v[2] - v[1] < 0 else v


def _pitch_from_vp(v: np.ndarray, f_scaled: float, roll_deg: float) -> float:
    r = math.radians(roll_deg)
    y_u = -math.sin(r) * v[0] + math.cos(r) * v[1]
    return math.degrees(math.atan2(-y_u, f_scaled * v[2]))


def line_pitch(sources, frame_size, f: float, roll_deg: float, n_boot: int, rng) -> tuple[dict | None, str | None]:
    """Pitch below the horizon from ground lines along the walkway, with a data-driven sd. (estimate, None) or
    (None, reason the lines were not used). See the section comment."""
    n_edges = sum(len(s) for s in sources)
    if n_edges < 2:
        return None, "no ground lines found" if not sources else "fewer than two usable ground-line edges"
    W, H = frame_size
    pts = np.vstack([p for s in sources for _, p in s])
    ref = (pts.mean(axis=0) - np.array([W / 2, H / 2])) / W
    fs = f / W
    v0 = _vanishing_point(_fit_lines(sources, frame_size), ref)
    p0 = _pitch_from_vp(v0, fs, roll_deg)
    if not PITCH_VALID_DEG[0] <= p0 <= PITCH_VALID_DEG[1]:
        return None, f"ground lines do not meet up the image (tilt from them {p0:.0f} deg): not lines along the walkway"
    vps, pb = [], []
    for _ in range(n_boot):
        v = _vanishing_point(_fit_lines(sources, frame_size, rng), ref)
        if v is None:
            continue
        p = _pitch_from_vp(v, fs, roll_deg)
        if PITCH_VALID_DEG[0] <= p <= PITCH_VALID_DEG[1]:
            vps.append(v)
            pb.append(p)
    if len(pb) < 0.9 * n_boot:
        return None, (f"ground-line vanishing point unstable ({n_boot - len(pb)} of {n_boot} bootstrap fits gave no "
                      "valid tilt)")
    boot_sd = float(np.std(pb))
    jack_sd = 0.0
    if len(sources) >= 3:
        pj = []
        for k in range(len(sources)):
            rest = sources[:k] + sources[k + 1:]
            v = _vanishing_point(_fit_lines(rest, frame_size), ref)
            if v is not None:
                pj.append(_pitch_from_vp(v, fs, roll_deg))
        if len(pj) >= 2:
            pj = np.asarray(pj)
            jack_sd = float(math.sqrt((len(pj) - 1) / len(pj) * np.sum((pj - pj.mean()) ** 2)))
    raw = math.hypot(boot_sd, jack_sd)
    if raw > LINE_PITCH_SD_MAX_DEG:
        return None, (f"ground lines too nearly parallel or inconsistent to fix the tilt (bootstrap sd {raw:.1f} "
                      "deg)")
    sd = max(raw, LINE_PITCH_SD_FLOOR_DEG)
    vp_px = None if abs(v0[2]) < 1e-12 else [float(v0[0] / v0[2] * W + W / 2), float(v0[1] / v0[2] * W + H / 2)]
    return {"vp": v0, "vps": vps, "pitch_deg": p0, "boot_sd_deg": boot_sd, "jack_sd_deg": jack_sd, "sd_deg": sd,
            "extra_sd_deg": math.sqrt(max(0.0, sd ** 2 - boot_sd ** 2)), "floored": raw < LINE_PITCH_SD_FLOOR_DEG,
            "n_edges": n_edges, "n_sources": len(sources), "vp_px": vp_px}, None


POSE_SOURCES = ("height", "field of view", "tilt", "roll", "mask edges")


def measure_gap_pose(ground_ref: dict, barrier_masks: list, frame_size: tuple, *,
                     wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM, clearance_cm: float = DEFAULT_CLEARANCE_CM,
                     walkway_edge=None, contact_px: float = 1.0, n_draws: int = 400, seed: int = 0) -> dict:
    """Clear gap from the camera pose alone (no reference in the clip). See the section comment above.

    ground_ref: {"kind": "camera_pose", "height_m", "height_range_m", "hfov_deg", "hfov_range_deg", "pitch_deg"
    (None -> ground lines or prior), "pitch_sd_deg", "roll_deg", "roll_sd_deg", "source", "sources",
    optional "ground_lines"}. Monte Carlo: height and field of view uniform over their ranges, pitch and roll as above,
    each barrier's contact set shifted by N(0, contact_px^2) plus 0.5 px per point.
    """
    W, Hpx = int(frame_size[0]), int(frame_size[1])
    g = ground_ref
    try:
        h0 = float(g["height_m"])
        hfov0 = float(g["hfov_deg"])
    except (KeyError, TypeError, ValueError):
        return _unknown("camera_pose needs height_m and hfov_deg")
    if not (0.1 <= h0 <= 3.0 and 20 <= hfov0 <= 150):
        return _unknown(f"camera_pose out of range: height {h0} m, field of view {hfov0} deg")
    h_rng = sorted(float(v) for v in (g.get("height_range_m") or [h0, h0]))
    f_rng = sorted(float(v) for v in (g.get("hfov_range_deg") or [hfov0, hfov0]))
    roll_given = g.get("roll_deg") is not None
    roll0 = float(g["roll_deg"]) if roll_given else 0.0
    roll_sd = float(g.get("roll_sd_deg") or POSE_ROLL_PRIOR_SD_DEG)
    f0 = focal_px(hfov0, W)
    rng = np.random.default_rng(seed)

    masks = [_as_mask(m, (W, Hpx)) for m in barrier_masks]
    contacts, labels = [], []
    for k, m in enumerate(masks):
        c = contact_points(m)
        if len(c):
            contacts.append(c)
            labels.append(f"b{k}")
    edge_img = None if walkway_edge is None else np.asarray(walkway_edge, dtype=float).reshape(2, 2)
    if len(contacts) + (edge_img is not None) < 2:
        return _unknown("need two barriers with visible ground contact, or one barrier and a walkway edge",
                        n_barriers=len(contacts))

    # pitch source
    line_est, line_note, n_skipped = None, None, 0
    if g.get("pitch_deg") is not None:
        mode, p_given = "given", float(g["pitch_deg"])
        p_sd = float(g.get("pitch_sd_deg") or POSE_PITCH_SD_DEG)
    else:
        sources, n_skipped = ground_line_sources(g.get("ground_lines"), (W, Hpx))
        line_est, line_note = line_pitch(sources, (W, Hpx), f0, roll0, n_draws, rng)
        mode = "lines" if line_est else "prior"

    def prior_range(f, roll):
        # each barrier's base (median contact; the highest contacts are often raised parts that lean out) must sit
        # below the horizon
        r = math.radians(roll)
        yu = min(float(np.median(-math.sin(r) * (s[:, 0] - W / 2) + math.cos(r) * (s[:, 1] - Hpx / 2)))
                 for s in contacts)
        need = math.degrees(math.atan2(-yu, f)) + HORIZON_MARGIN_DEG
        lo = max(POSE_PITCH_PRIOR_DEG[0], need)
        return lo, max(POSE_PITCH_PRIOR_DEG[1], lo + 5.0)

    def pitch_for(f, roll, vary):
        if mode == "given":
            return p_given + (rng.normal(0.0, p_sd) if vary else 0.0)
        if mode == "lines":
            if not vary:
                return _pitch_from_vp(line_est["vp"], f / W, roll)
            v = line_est["vps"][int(rng.integers(len(line_est["vps"])))]
            return _pitch_from_vp(v, f / W, roll) + rng.normal(0.0, line_est["extra_sd_deg"])
        lo, hi = prior_range(f, roll)
        return rng.uniform(lo, hi) if vary else (lo + hi) / 2

    def run(h, f, pitch, roll, sets, edge):
        grounds = []
        for s in sets:
            gp, ok = ray_to_ground(s, (W, Hpx), f, pitch, roll, h)
            if ok.sum() < max(1, len(s) // 2):
                return None
            grounds.append(gp[ok])
        e = None
        if edge is not None:
            e, ok = ray_to_ground(edge, (W, Hpx), f, pitch, roll, h)
            if not ok.all():
                return None
        return _passages(grounds, e, labels)

    # nominal pose; contact columns above its horizon are raised parts (see measure_gap) and are dropped
    p_nom = pitch_for(f0, roll0, False)
    dropped = 0
    for k, s in enumerate(contacts):
        _, ok = ray_to_ground(s, (W, Hpx), f0, p_nom, roll0, h0)
        dropped += int((~ok).sum())
        contacts[k] = s[ok]
    if any(len(s) == 0 for s in contacts):
        return _unknown(f"barrier bases lie above the horizon for a camera tilted {p_nom:.1f} deg down: the tilt is "
                        "wrong or the barriers are not on the same ground", pitch_deg=round(p_nom, 2))
    nominal = run(h0, f0, p_nom, roll0, contacts, edge_img)
    if nominal is None:
        return _unknown("walkway edge lies above the horizon for the nominal camera pose")
    best = int(np.argmax([d for _, d in nominal]))
    gap_px = _gap_ends_px(nominal[best][0], contacts, labels, edge_img,
                          lambda pts: ray_to_ground(pts, (W, Hpx), f0, p_nom, roll0, h0)[0])

    def simulate(n, vary):
        out, bad = [], 0
        for _ in range(n):
            h = rng.uniform(*h_rng) if "height" in vary else h0
            f = focal_px(rng.uniform(*f_rng), W) if "field of view" in vary else f0
            roll = roll0 + rng.normal(0.0, roll_sd) if "roll" in vary else roll0
            pitch = pitch_for(f, roll, "tilt" in vary)
            if "mask edges" in vary:
                sets = [s + rng.normal(0.0, contact_px, 2) + rng.normal(0.0, 0.5, s.shape) for s in contacts]
                e = None if edge_img is None else edge_img + rng.normal(0.0, contact_px, edge_img.shape)
            else:
                sets, e = contacts, edge_img
            r = run(h, f, pitch, roll, sets, e)
            if r is None or len(r) != len(nominal):
                bad += 1
                continue
            out.append(r[best][1])
        return np.asarray(out), bad

    draws, bad = simulate(n_draws, set(POSE_SOURCES))
    if bad > MAX_BAD_POSE_FRACTION * n_draws or len(draws) == 0:
        return _unknown(f"{bad} of {n_draws} draws of the camera pose put the barrier bases at or above the horizon: "
                        "the barriers are too far away for the tilt range -- film closer, or let the phone record its "
                        "tilt", pitch_mode=mode)
    gap = float(np.median(draws)) if mode == "prior" else nominal[best][1]
    lo, hi = np.percentile(draws, [2.5, 97.5])
    lo, hi = float(min(lo, gap)), float(max(hi, gap))

    sens = {}
    for name in POSE_SOURCES:
        d, _ = simulate(POSE_SENS_DRAWS, {name})
        sens[name] = float(np.ptp(np.percentile(d, [2.5, 97.5]))) if len(d) else float("nan")
    dominant = max(sens, key=lambda k: -1.0 if math.isnan(sens[k]) else sens[k])

    verdict = verdict_for(lo, hi, wheelchair_cm, clearance_cm)
    need = wheelchair_cm + 2 * clearance_cm
    if verdict == "passes":
        head = f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) >= {need:.0f} cm needed"
    elif verdict == "blocked":
        head = f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) < {wheelchair_cm:.0f} cm chair width"
    else:
        head = (f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) "
                + middle_phrase(verdict, lo, hi, wheelchair_cm, need))
    srcs = g.get("sources") or {}
    basis = {"default": "default assumptions", "calibrated": "your calibration", "sensor": "phone sensor"}.get(
        g.get("source"), g.get("source") or "given pose")
    if mode == "given":
        tilt = f"tilt {p_given:.1f} +- {p_sd:.1f} deg down ({srcs.get('pitch', 'given')})"
        p_sd_eff, p_range = p_sd, None
    elif mode == "lines":
        p_sd_eff, p_range = line_est["sd_deg"], None
        tilt = (f"tilt {p_nom:.1f} +- {p_sd_eff:.1f} deg down, estimated from {line_est['n_edges']} ground-line "
                f"edges (sd from a bootstrap of the edge fits" + (", raised to the floor" if line_est["floored"]
                                                                   else "") + ")")
    else:
        p_range = prior_range(f0, roll0)
        p_sd_eff = None
        tilt = (f"tilt not measured, assumed anywhere {p_range[0]:.0f}-{p_range[1]:.0f} deg down"
                + (f" ({line_note})" if line_note else ""))
    roll_txt = (f"roll {roll0:.1f} +- {roll_sd:.1f} deg" if roll_given else
                f"roll assumed level (+- {2 * roll_sd:.0f} deg)")
    why = "; ".join([
        head,
        f"no reference in view, so measured from the camera pose ({basis}): camera {h_rng[0] * 100:.0f}-"
        f"{h_rng[1] * 100:.0f} cm above the ground ({srcs.get('height', 'given')}), field of view {f_rng[0]:.0f}-"
        f"{f_rng[1]:.0f} deg ({srcs.get('hfov', 'given')}), {tilt}, {roll_txt}",
        f"largest error source: {dominant} (alone it spans {sens[dominant]:.0f} cm of the band)"])
    pose_out = {"height_m": h0, "height_range_m": h_rng, "hfov_deg": hfov0, "hfov_range_deg": f_rng,
                "pitch_deg": round(p_nom, 2), "pitch_sd_deg": None if p_sd_eff is None else round(p_sd_eff, 2),
                "pitch_range_deg": None if p_range is None else [round(v, 2) for v in p_range],
                "pitch_source": {"given": "given", "lines": "ground lines", "prior": "prior"}[mode],
                "roll_deg": roll0, "roll_sd_deg": roll_sd, "roll_source": "given" if roll_given else "prior",
                "source": g.get("source")}
    lines_out = None
    if line_est:
        lines_out = {"used": True, "edges": line_est["n_edges"], "sources": line_est["n_sources"],
                     "skipped": n_skipped, "vanishing_point_px": None if line_est["vp_px"] is None else
                     [round(v, 1) for v in line_est["vp_px"]], "bootstrap_sd_deg": round(line_est["boot_sd_deg"], 3),
                     "between_lines_sd_deg": round(line_est["jack_sd_deg"], 3)}
    elif mode == "prior":
        lines_out = {"used": False, "why": line_note, "skipped": n_skipped}
    return {
        "gap_cm": round(gap, 1), "band_cm": [round(lo, 1), round(hi, 1)], "verdict": verdict, "why": why,
        "detail": {"mode": "camera_pose", "gaps": [{"between": lab, "cm": round(d, 1)} for lab, d in nominal],
                   "chosen": nominal[best][0], "gap_px": gap_px, "pose": pose_out, "ground_lines": lines_out,
                   "error_sources_cm": {k: round(v, 1) for k, v in sens.items()}, "dominant_error": dominant,
                   "draws": len(draws), "bad_draws": bad, "dropped_contacts": dropped,
                   "wheelchair_cm": wheelchair_cm, "clearance_cm": clearance_cm},
    }
