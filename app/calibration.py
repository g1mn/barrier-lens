"""Per-user camera profile, phone motion sensors and clip metadata -> the camera_pose ground reference.

No physical reference is required by default. When the clip has none, the gap is measured by intersecting image
rays with the ground plane, using a camera pose built here from (best source first):

    height   calibration (solved from a reference filmed once)  > profile armrest height  > DEFAULT_HEIGHT_M
    hfov     profile hfov_deg / focal_35mm                      > clip metadata (ffprobe)  > DEFAULT_F35_MM
    pitch    sensor + calibration bias                          > sensor                   > None (geometry
             estimates it from ground lines, or falls back to its own wide prior)

Everything uncertain is passed as a range (height_range_m, hfov_range_deg, pitch_sd_deg, roll_sd_deg) so the Monte
Carlo band in geometry covers it, and the result says which mode it used:
    "reference"   a reference of known size was in the clip (homography; the profile only sets the chair width)
    "calibrated"  no reference in the clip, but the user calibrated once (height / pitch bias solved by solve_pose)
    "default"     no reference, no calibration: "Estimated with default assumptions"

Assumptions (not measured in this repo, stated so they can be checked):
- DEFAULT_HEIGHT_M 0.75, range 0.65-0.90: a phone held at wheelchair armrest height. Typical armrest heights are
  roughly 65-80 cm above the ground; 0.90 allows for holding the phone a little above the armrest.
- DEFAULT_F35_MM 26 (range 23-28): main phone cameras are marketed at about 24-26 mm 35 mm-equivalent focal length
  (e.g. iPhone main cameras 26 mm, recent Pro models 24 mm -- from memory, not checked against spec sheets here).
  The 35 mm frame is taken as 36 mm across the long side of the video; video stabilisation crops a little more,
  which the range absorbs.
- Sensor pitch sd 3 deg (uncalibrated) / 1.5 deg (after calibration): the phone's own tilt estimate plus how the
  camera module sits in the body.
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil

import cv2
import numpy as np

DEFAULT_HEIGHT_M = 0.75
HEIGHT_RANGE_M = (0.65, 0.90)
PROFILE_HEIGHT_SPREAD_M = (-0.07, 0.10)   # stated armrest height: the phone sits somewhere around it
CALIBRATED_HEIGHT_SD_M = 0.04
DEFAULT_F35_MM = 26.0
F35_RANGE_MM = (23.0, 28.0)
SENSOR_PITCH_SD_DEG = 3.0
CALIBRATED_PITCH_SD_DEG = 1.5
SENSOR_ROLL_SD_DEG = 3.0
CHAIR_PRESETS_CM = {"manual": 65.0, "power": 70.0}   # mid-points of the UI presets (manual 60-70, power 65-75)
MIN_SENSOR_SAMPLES = 5

REF_SIZES_M = {"a4x4": (0.420, 0.594), "tactile_block": (0.30, 0.30)}


# ---------------------------------------------------------------- inputs

def _num(v, lo: float, hi: float) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and lo <= x <= hi else None


def parse_json(text: str | None) -> dict | None:
    """Form field -> dict, or None for empty / invalid / not an object."""
    if not text:
        return None
    try:
        d = json.loads(text)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def clean_profile(p: dict | None) -> dict:
    """Keep only known fields within sane bounds. Unknown or out-of-range values are dropped, never guessed."""
    p = p or {}
    out: dict = {}
    t = p.get("chair_type")
    if t in ("manual", "power", "custom"):
        out["chair_type"] = t
    w = _num(p.get("chair_width_cm"), 40, 100)
    if w is None and t in CHAIR_PRESETS_CM:
        w = CHAIR_PRESETS_CM[t]
    if w is not None:
        out["chair_width_cm"] = w
    h = _num(p.get("armrest_height_cm"), 40, 130)
    if h is not None:
        out["armrest_height_cm"] = h
    if isinstance(p.get("phone_model"), str) and p["phone_model"].strip():
        out["phone_model"] = p["phone_model"].strip()[:60]
    f = _num(p.get("focal_35mm"), 10, 80)
    if f is not None:
        out["focal_35mm"] = f
    v = _num(p.get("hfov_deg"), 30, 130)
    if v is not None:
        out["hfov_deg"] = v
    c = p.get("calibration")
    if isinstance(c, dict):
        cal = {k: _num(c.get(k), lo, hi) for k, lo, hi in
               (("height_m", 0.3, 1.6), ("pitch_bias_deg", -20, 20), ("hfov_deg", 30, 130), ("pitch_deg", -10, 89))}
        cal = {k: v for k, v in cal.items() if v is not None}
        if "height_m" in cal:
            cal["ref_kind"] = c.get("ref_kind") if c.get("ref_kind") in REF_SIZES_M else None
            cal["at"] = str(c.get("at") or "")[:40]
            out["calibration"] = cal
    return out


def sensor_pose(s: dict | None) -> dict | None:
    """Averaged DeviceOrientationEvent beta/gamma (+ screen angle) -> camera pitch below horizontal and roll, degrees.

    Portrait (angle 0/180): beta is the front-back tilt; upright = 90, so pitch_down = 90 - beta, roll = gamma.
    Landscape (angle 90/270): the tilt shows in gamma; pitch_down = 90 - |gamma|, roll ~ beta. Approximate: Euler
    angles near upright are ill-conditioned, which the sensor sd covers. Too few samples -> None.
    """
    if not s:
        return None
    n = _num(s.get("n"), 0, 1e7) or 0
    beta, gamma = _num(s.get("beta"), -180, 180), _num(s.get("gamma"), -90, 90)
    if n < MIN_SENSOR_SAMPLES or beta is None or gamma is None:
        return None
    angle = int(_num(s.get("angle"), -360, 360) or 0) % 360
    if angle in (90, 270):
        pitch, roll = 90.0 - abs(gamma), beta if angle == 90 else -beta
    else:
        pitch, roll = 90.0 - beta, gamma
    if not -30 <= pitch <= 89:
        return None
    return {"pitch_deg": round(pitch, 2), "roll_deg": round(roll, 2), "samples": int(n)}


# ---------------------------------------------------------------- clip metadata

def _tags_f35(tags: dict) -> float | None:
    for k, v in tags.items():
        kl = k.lower()
        if "focal_length" in kl and "35" in kl:
            return _num(v, 10, 80)
    return None


def _tags_model(tags: dict) -> str | None:
    for k in ("com.apple.quicktime.model", "com.android.model", "model"):
        if isinstance(tags.get(k), str) and tags[k].strip():
            return tags[k].strip()[:60]
    return None


async def clip_hints(path) -> dict:
    """ffprobe the ORIGINAL upload (normalising strips metadata): 35 mm-equivalent focal length and phone model when
    the container carries them (iPhone QuickTime tags; Android rarely). Empty dict when nothing is there."""
    if not shutil.which("ffprobe"):
        return {}
    p = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                                             "-show_streams", str(path), stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    try:
        d = json.loads(out or b"{}")
    except ValueError:
        return {}
    tags: dict = {}
    for src in [d.get("format") or {}, *(d.get("streams") or [])]:
        tags.update({str(k): v for k, v in (src.get("tags") or {}).items()})
    hints = {}
    f = _tags_f35(tags)
    if f:
        hints["focal_35mm"] = f
    m = _tags_model(tags)
    if m:
        hints["phone_model"] = m
    return hints


# ---------------------------------------------------------------- pose

def hfov_from_f35(f35: float, frame_size: tuple[int, int]) -> float:
    """Horizontal FOV of the delivered frame: the long side spans 36 mm at focal f35; a portrait frame's width is
    the short side."""
    w, h = frame_size
    half_long = math.atan(18.0 / f35)
    if w >= h:
        return math.degrees(2 * half_long)
    return math.degrees(2 * math.atan(math.tan(half_long) * w / h))


def chair_width_cm(profile: dict, default: float) -> tuple[float, str]:
    w = profile.get("chair_width_cm")
    return (float(w), "profile") if w else (default, "default")


def camera_pose(profile: dict, sensor: dict | None, hints: dict, frame_size: tuple[int, int]) -> dict:
    """The camera_pose ground reference geometry samples from. See the module docstring for sources and ranges."""
    cal = profile.get("calibration") or {}
    sources = {}
    if cal.get("height_m"):
        h = cal["height_m"]
        h_rng = [round(h - 2 * CALIBRATED_HEIGHT_SD_M, 3), round(h + 2 * CALIBRATED_HEIGHT_SD_M, 3)]
        sources["height"] = "calibration"
    elif profile.get("armrest_height_cm"):
        h = profile["armrest_height_cm"] / 100.0
        h_rng = [round(h + PROFILE_HEIGHT_SPREAD_M[0], 3), round(h + PROFILE_HEIGHT_SPREAD_M[1], 3)]
        sources["height"] = "profile armrest height"
    else:
        h, h_rng = DEFAULT_HEIGHT_M, list(HEIGHT_RANGE_M)
        sources["height"] = "default (armrest height)"

    if profile.get("hfov_deg"):
        hfov = profile["hfov_deg"]
        f_rng = [hfov - 2, hfov + 2]
        sources["hfov"] = "profile"
    elif cal.get("hfov_deg"):
        hfov = cal["hfov_deg"]
        f_rng = [hfov - 2, hfov + 2]
        sources["hfov"] = "calibration"
    else:
        f35 = profile.get("focal_35mm") or hints.get("focal_35mm")
        if f35:
            sources["hfov"] = "profile focal length" if profile.get("focal_35mm") else "clip metadata"
            lo_f, hi_f = f35 * 0.97, f35 * 1.10   # stabilisation crop narrows the view a little
        else:
            f35, (lo_f, hi_f) = DEFAULT_F35_MM, F35_RANGE_MM
            sources["hfov"] = "default (typical phone main camera)"
        hfov = hfov_from_f35(f35, frame_size)
        f_rng = sorted([hfov_from_f35(hi_f, frame_size), hfov_from_f35(lo_f, frame_size)])

    sp = sensor_pose(sensor)
    pitch = roll = pitch_sd = roll_sd = None
    if sp:
        pitch, roll = sp["pitch_deg"], sp["roll_deg"]
        roll_sd = SENSOR_ROLL_SD_DEG
        if cal.get("pitch_bias_deg") is not None:
            pitch += cal["pitch_bias_deg"]
            pitch_sd = CALIBRATED_PITCH_SD_DEG
            sources["pitch"] = "phone sensor + calibration"
        else:
            pitch_sd = SENSOR_PITCH_SD_DEG
            sources["pitch"] = "phone sensor"
    else:
        sources["pitch"] = "not recorded (estimated from ground lines or a wide prior)"

    mode = "calibrated" if cal.get("height_m") else "default"
    return {"kind": "camera_pose", "height_m": round(h, 3), "hfov_deg": round(hfov, 2),
            "pitch_deg": None if pitch is None else round(pitch, 2),
            "roll_deg": None if roll is None else round(roll, 2),
            "source": mode, "height_range_m": h_rng, "hfov_range_deg": [round(x, 2) for x in f_rng],
            "pitch_sd_deg": pitch_sd, "roll_sd_deg": roll_sd, "sources": sources,
            "sensor_samples": sp["samples"] if sp else 0}


LABELS = {"reference": "Measured against a reference in the clip",
          "calibrated": "Calibrated",
          "default": "Estimated with default assumptions"}


def assumption_text(mode: str, pose: dict | None, chair_cm: float, chair_src: str, ref_concept: str | None) -> str:
    chair = f"Chair width {chair_cm:.0f} cm ({'your profile' if chair_src == 'profile' else 'default'})."
    if mode == "reference":
        return f"Scale from the {ref_concept or 'reference'} on the ground. {chair}"
    lo, hi = pose["height_range_m"]
    s = pose["sources"]
    if mode == "calibrated":
        hs = f"Phone at about {pose['height_m'] * 100:.0f} cm, solved from your calibration clip."
    elif s["height"].startswith("profile"):
        hs = f"Phone held near your armrest height, {lo * 100:.0f}–{hi * 100:.0f} cm above the ground."
    else:
        hs = f"Phone assumed at armrest height, anywhere {lo * 100:.0f}–{hi * 100:.0f} cm above the ground."
    tilt = ("Tilt from the phone's motion sensor." if pose["pitch_deg"] is not None else
            "Tilt not recorded, so it is estimated from lines on the ground.")
    return f"{hs} {tilt} {chair}"


def measure_with_pose(geometry, barrier_result: dict, pose: dict, wheelchair_cm: float) -> dict:
    """Barrier masks + camera_pose -> geometry.measure_gap on the frame with the most barrier objects."""
    frames = {fr["i"]: fr["objects"] for fr in barrier_result.get("frames", []) if fr.get("objects")}
    size = (barrier_result.get("width"), barrier_result.get("height"))
    if not frames or None in size:
        return geometry._unknown("no barrier masks to measure")
    i = max(frames, key=lambda k: (len(frames[k]), -k))
    out = geometry.measure_gap(dict(pose), frames[i], size, wheelchair_cm=wheelchair_cm)
    if out.get("verdict") == "unknown" and "camera_pose" in (out.get("why") or "") and "size_cm" in out["why"]:
        out["why"] = ("No reference of known size was found, and measuring from the camera height alone is not "
                      "available in this build yet.")
    out.setdefault("detail", {}).update({"frame": i, "barrier_ids": [o.get("id") for o in frames[i]]})
    return out


# ---------------------------------------------------------------- one-time calibration

def solve_pose(corners_px, ref_kind: str, frame_size: tuple[int, int], hfov_deg: float) -> dict | None:
    """Reference corners (far-left, far-right, near-right, near-left) + known size -> camera height above the ground
    and pitch/roll, by cv2.solvePnP (IPPE, planar) with a pinhole K from hfov_deg (principal point at the centre)."""
    across, along = REF_SIZES_M[ref_kind]
    obj = np.array([[0, along, 0], [across, along, 0], [across, 0, 0], [0, 0, 0]], np.float64)
    img = np.asarray(corners_px, np.float64).reshape(4, 2)
    w, h = frame_size
    f = (w / 2) / math.tan(math.radians(hfov_deg) / 2)
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    C = (-R.T @ tvec).ravel()
    n = np.array([0, 0, 1.0]) * (1 if C[2] >= 0 else -1)   # ground normal on the camera's side
    d = R.T @ np.array([0, 0, 1.0])                        # optical axis in ground coordinates
    x = R.T @ np.array([1.0, 0, 0])                        # image right in ground coordinates
    proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
    return {"height_m": round(abs(float(C[2])), 4), "pitch_deg": round(math.degrees(math.asin(-float(d @ n))), 2),
            "roll_deg": round(math.degrees(math.asin(float(x @ n))), 2),
            "reproj_px": round(float(np.abs(proj.reshape(4, 2) - img).max()), 2)}


MAX_CAL_FRAMES = 15


async def calibrate(video_path, ref_kind: str, profile: dict | None, sensor: dict | None, replay: bool = False,
                    fixture_sha: str | None = None) -> dict:
    """One-time calibration clip of a known reference (four A4 sheets 2x2, or one 30 cm tactile block) ->
    {"ok", "calibration": {height_m, pitch_deg, pitch_bias_deg, hfov_deg, ...}} to store in the user's profile.

    Same privacy and money path as analyze: blurred clip only, SAM through analyze._segment (replay or spend cap).
    Solves the pose on up to MAX_CAL_FRAMES frames and reports the median. A tactile strip longer than one block
    gives a wrong quad; film a single block or the sheets."""
    from pathlib import Path

    from app import analyze, geometry
    from app.config import key

    if ref_kind not in REF_SIZES_M:
        raise ValueError(f"ref_kind must be one of {sorted(REF_SIZES_M)}")
    replay = bool(replay or not key("META_API_KEY"))
    src = Path(video_path)
    if not src.is_file():
        raise analyze.AnalyzeError("video file not found")
    hints = await clip_hints(src)
    clip, blur_counts, job = await analyze.prepare(src)
    info = analyze.clip_info(clip)
    size = (info["width"], info["height"])
    prof = clean_profile(profile)
    pose0 = camera_pose({k: v for k, v in prof.items() if k != "calibration"}, None, hints, size)
    hfov = pose0["hfov_deg"]
    concept = "paper" if ref_kind == "a4x4" else "tactile paving"
    res = await analyze._segment(clip, concept, info["frames"], replay, fixture_sha)
    base = {"job": job, "replay": replay, "ref_kind": ref_kind, "concept": concept, "hfov_deg": hfov,
            "hfov_source": pose0["sources"]["hfov"], "sam_source": res.get("source"), "blur": blur_counts}
    frames = [fr for fr in res.get("frames", []) if fr.get("objects")]
    if (res.get("width"), res.get("height")) != size or not frames:
        return {**base, "ok": False, "why": f"The {concept} was not found in the calibration clip"
                + (f" ({res['error']})" if res.get("error") else "") + ". Film it flat on the ground, fully in view."}
    step = max(1, len(frames) // MAX_CAL_FRAMES)
    sols = []
    for fr in frames[::step][:MAX_CAL_FRAMES]:
        obj = max(fr["objects"], key=lambda o: o.get("score") or 0.0)
        corners = geometry.find_reference_corners(geometry._as_mask(obj, size))
        if corners is None:
            continue
        s = solve_pose(corners, ref_kind, size, hfov)
        if s and 0.3 <= s["height_m"] <= 1.6:
            sols.append(s)
    if not sols:
        return {**base, "ok": False, "why": "The reference was found but its corners could not be solved into a "
                                             "camera height. Film closer, with all four corners in view."}
    hs = np.array([s["height_m"] for s in sols])
    ps = np.array([s["pitch_deg"] for s in sols])
    sp = sensor_pose(sensor)
    cal = {"height_m": round(float(np.median(hs)), 3), "pitch_deg": round(float(np.median(ps)), 2),
           "pitch_bias_deg": None if sp is None else round(float(np.median(ps)) - sp["pitch_deg"], 2),
           "hfov_deg": round(hfov, 2), "ref_kind": ref_kind, "frames_used": len(sols),
           "height_spread_m": round(float(hs.max() - hs.min()), 3),
           "reproj_px_max": max(s["reproj_px"] for s in sols)}
    return {**base, "ok": True, "calibration": cal,
            "note": "Height and tilt solved from the reference with the field of view above; if that field of view "
                    "is off, the solved height absorbs part of the error."}
