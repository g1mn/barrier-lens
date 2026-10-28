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

