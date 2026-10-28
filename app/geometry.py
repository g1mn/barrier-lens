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


def verdict_for(lo: float, hi: float, wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM,
                clearance_cm: float = DEFAULT_CLEARANCE_CM) -> str:
    if lo >= wheelchair_cm + 2 * clearance_cm:
        return "passes"
    if hi < wheelchair_cm:
        return "blocked"
    return "tight"


# ---------------------------------------------------------------- main entry

def measure_gap(ground_ref: dict, barrier_masks: list, frame_size: tuple, *,
                wheelchair_cm: float = DEFAULT_WHEELCHAIR_CM, clearance_cm: float = DEFAULT_CLEARANCE_CM,
                walkway_edge=None, corner_px: float = 3.0, contact_px: float = 1.0,
                n_draws: int = 400, seed: int = 0) -> dict:
    """Clear gap between barriers on the ground plane, with a Monte Carlo band and a verdict. See module docstring."""
    W, Hpx = int(frame_size[0]), int(frame_size[1])
    if not ground_ref:
        return _unknown("no ground reference: a gap in centimetres needs a rectangle of known size on the ground")
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
        why = (f"narrowest point {gap:.0f} cm (95% band {lo:.0f}-{hi:.0f}) is between the {wheelchair_cm:.0f} cm "
               f"chair width and the {need:.0f} cm needed with clearance")
    return {
        "gap_cm": round(gap, 1), "band_cm": [round(lo, 1), round(hi, 1)], "verdict": verdict, "why": why,
        "detail": {"gaps": [{"between": lab, "cm": round(d, 1)} for lab, d in nominal], "chosen": nominal[best][0],
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
        head = (f"corridor {gap:.0f} cm across the walkway (95% band {lo:.0f}-{hi:.0f}) is between the "
                f"{wheelchair_cm:.0f} cm chair width and the {need:.0f} cm needed with clearance")
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
                   "extrapolated_px": round(extrap, 1), "extrapolated_pinch_px": round(extrap_pinch, 1),
                   "draws": len(draws), "bad_draws": bad, "dropped_contacts": dropped,
                   "wheelchair_cm": wheelchair_cm, "clearance_cm": clearance_cm},
    }
