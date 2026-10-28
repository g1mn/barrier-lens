"""One clip in, one verdict out: normalise -> blur -> triage -> SAM -> geometry -> explanation.

    async analyze(video_path, lat, lon, replay=False, **opts) -> dict  (shape in _result())

Steps
1. Normalise with ffmpeg: first MAX_SECONDS s, FPS fps, short side capped at SHORT_SIDE, H.264 MP4, no audio.
2. Blur faces on every frame (app.blur). The unblurred intermediate is deleted; only the blurred clip is kept,
   and it is the only thing sent to any API (triage frames are cut from it, SAM gets it whole).
3. Six evenly spaced frames -> spark.triage (frame_indices = clip frame numbers). Not usable -> early return.
4. SAM 3.1 once per barrier concept (at most MAX_BARRIERS) plus one reference concept: "paper" when triage saw a
   sheet, else "tactile paving". SAM wants one concept per request; CONCEPT_ALIASES maps names SAM missed on real
   clips ("e-scooter" -> 0 matches, "scooter" -> 2 tracked objects, 2026-10-28).
5. pipeline.measure_from_sam on the merged barrier masks + the reference -> gap, band, verdict (geometry decides).
6. spark.explain on the measurement.
Masks go to the page as simplified polygons (cv2.findContours + approxPolyDP), so the browser needs no decoder.

Money
- replay=True, or no META_API_KEY: cached / fixture results only. Spark is forced to its replay backend and SAM to
  replay mode, so no paid call can happen.
- Every paid call is appended to data/cache/ledger.jsonl. Before each one, the ledger total plus that call's
  estimate is checked against BL_SPEND_CAP_USD (default 5); over the cap -> SpendCapError, nothing is sent.
  Spark calls are logged with token usage and usd None: no per-token price is recorded in this repo.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from app import blur, calibration, geometry, pipeline, sam, spark
from app.config import key

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / "data" / "cache" / "analyze"
LEDGER = ROOT / "data" / "cache" / "ledger.jsonl"
SAMPLE_FALLBACK = ROOT / "data" / "fixtures" / "analyze" / "sample-scooters-row.json"
SAMPLE_FALLBACK_SHA = "69bee8c8ddaaabb410898f49f41c998200fbbef5ef97c35b56db09bba20cef19"  # scooters-row, first 3 s
# recorded live runs with their blurred clips; the first is the default sample
DEMO_RUNS = {"scooters": ROOT / "static" / "demo" / "scooters-city-live.json",
             "closed": ROOT / "static" / "demo" / "sidewalk-closed-live.json"}

MAX_SECONDS = 10
FPS = 10
SHORT_SIDE = 720
N_TRIAGE_FRAMES = 6
MAX_BARRIERS = 3
CONCEPT_ALIASES = {"e-scooter": "scooter", "electric scooter": "scooter", "kick scooter": "scooter"}
# Reference concept -> geometry ground_ref. A tactile strip's length in the image is unknown, so it is read as
# geometry's "tactile_strip" (known width BL_TACTILE_WIDTH_CM, default 30; measures the across-walkway corridor).
# Setting BL_TACTILE_BLOCKS=n instead treats the mask as n 30 x 30 cm blocks. "paper" follows geometry's "a4x4"
# (four A4 sheets in a 2 x 2 square) unless BL_PAPER_KIND=a4 (one portrait sheet, 21.0 x 29.7 cm).
REF_CONCEPTS = ("paper", "tactile paving")
POLY_EPS_PX = 1.5
MIN_POLY_AREA_PX = 12.0


class AnalyzeError(RuntimeError):
    """The upload is not a usable video (cannot be read or normalised)."""


class SpendCapError(RuntimeError):
    """A paid call would push the ledger over BL_SPEND_CAP_USD."""


# ---------------------------------------------------------------- ledger / spend cap

def spend_cap_usd() -> float:
    try:
        return float(os.environ.get("BL_SPEND_CAP_USD", "5"))
    except ValueError:
        return 5.0


def spent_usd() -> float:
    if not LEDGER.exists():
        return 0.0
    total = 0.0
    for line in LEDGER.read_text().splitlines():
        try:
            total += float(json.loads(line).get("usd") or 0.0)
        except (ValueError, AttributeError):
            continue
    return round(total, 6)


def _check_cap(estimate_usd: float, what: str) -> None:
    spent, cap = spent_usd(), spend_cap_usd()
    if spent + estimate_usd > cap or spent >= cap:
        raise SpendCapError(f"spend cap reached: ledger ${spent:.2f} + {what} ~${estimate_usd:.2f} > "
                            f"BL_SPEND_CAP_USD ${cap:.2f}; no call was made")


def _ledger(entry: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def _spark_replay():
    """Force app.spark's replay backend. spark reads SPARK_BACKEND on each call; its replay path never awaits I/O,
    so on one event loop no other request runs while the override is in place."""
    old = os.environ.get("SPARK_BACKEND")
    os.environ["SPARK_BACKEND"] = "replay"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SPARK_BACKEND", None)
        else:
            os.environ["SPARK_BACKEND"] = old


# ---------------------------------------------------------------- video

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _ffmpeg(*args: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec("ffmpeg", "-nostdin", "-y", "-v", "error", *args,
                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return p.returncode, err.decode(errors="replace")


async def normalise(src: Path, out: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise AnalyzeError("ffmpeg is not installed")
    s = SHORT_SIDE
    scale = (f"scale='if(lte(iw,ih),2*trunc(min({s},iw)/2),-2)':'if(lte(iw,ih),-2,2*trunc(min({s},ih)/2))'")
    rc, err = await _ffmpeg("-i", str(src), "-t", str(MAX_SECONDS), "-map", "0:v:0", "-an", "-sn", "-dn",
                            "-vf", f"fps={FPS},{scale}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out))
    if rc != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise AnalyzeError(f"could not read this video: {err.strip()[:200] or 'ffmpeg failed'}")


def clip_info(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    try:
        return {"width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), "fps": float(cap.get(cv2.CAP_PROP_FPS)) or FPS}
    finally:
        cap.release()


def triage_frames(path: Path, n_frames: int, n: int = N_TRIAGE_FRAMES) -> tuple[list[bytes], dict]:
    """n evenly spaced JPEG frames of the (blurred) clip + spark meta with times and frame numbers."""
    idx = sorted({min(n_frames - 1, int((k + 0.5) * n_frames / n)) for k in range(n)}) if n_frames else []
    cap = cv2.VideoCapture(str(path))
    jpgs, got = [], []
    try:
        for i in idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = cap.read()
            if not ok:
                continue
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                jpgs.append(buf.tobytes())
                got.append(i)
    finally:
        cap.release()
    return jpgs, {"frame_indices": got, "times_s": [round(i / FPS, 2) for i in got]}


async def prepare(src: Path) -> tuple[Path, dict, str]:
    """Normalised + blurred clip for src (cached by src hash). Returns (clip path, blur counts, job id)."""
    sha = await asyncio.to_thread(_sha256, src)
    job = sha[:16]
    d = WORK_DIR / job
    clip, meta = d / "clip.mp4", d / "blur.json"
    if clip.exists() and meta.exists():
        counts = json.loads(meta.read_text())
        if counts.get("version") == blur.VERSION:
            return clip, counts, job
    d.mkdir(parents=True, exist_ok=True)
    raw = d / "normalised.unblurred.mp4"
    try:
        await normalise(src, raw)
        counts = await asyncio.to_thread(blur.blur_video, raw, clip, FPS)
    finally:
        raw.unlink(missing_ok=True)  # never keep an unblurred copy
    counts.update({"source_sha256": sha, "version": blur.VERSION})
    meta.write_text(json.dumps(counts))
    return clip, counts, job


def clip_path(job: str) -> Path | None:
    """Blurred clip for a job id (16 hex chars), for serving."""
    if len(job) != 16 or any(c not in "0123456789abcdef" for c in job):
        return None
    p = WORK_DIR / job / "clip.mp4"
    return p if p.exists() else None


# ---------------------------------------------------------------- masks -> polygons

def polygons(obj: dict, frame_size: tuple[int, int]) -> list[list[list[float]]]:
    """Outer contours of one SAM object, simplified, in frame pixel coordinates (pixel centres), largest first."""
    m = sam.decode_mask(obj, frame_size).astype(np.uint8)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        if cv2.contourArea(c) < MIN_POLY_AREA_PX:
            continue
        p = cv2.approxPolyDP(c, POLY_EPS_PX, True).reshape(-1, 2) + 0.5
        if len(p) >= 3:
            out.append([[round(float(x), 1), round(float(y), 1)] for x, y in p])
    return out


def mask_track(res: dict, role: str) -> dict:
    size = (res.get("width"), res.get("height"))
    frames = []
    for fr in res.get("frames", []):
        objs = []
        for o in fr.get("objects", []):
            polys = polygons(o, size) if None not in size else []
            if polys:
                objs.append({"id": o.get("id"), "score": o.get("score"), "polygon": polys[0], "polygons": polys})
        if objs:
            frames.append({"i": fr["i"], "objects": objs})
    return {"concept": res.get("concept"), "role": role, "source": res.get("source"),
            "replay_fixture": res.get("replay_fixture"), "frames": frames}


def merge_barriers(results: list[dict]) -> dict:
    """Several barrier concepts -> one SAM-shaped result (ids made unique as concept_index * 1000 + id)."""
    sized = [r for r in results if r.get("width") and r.get("frames")]
    if not sized:
        return {"width": None, "height": None, "frames": []}
    by_i: dict[int, list] = {}
    for k, r in enumerate(sized):
        for fr in r["frames"]:
            by_i.setdefault(fr["i"], []).extend(
                {**o, "id": k * 1000 + int(o.get("id") or 0), "concept": r["concept"]} for o in fr["objects"])
    w, h = sized[0]["width"], sized[0]["height"]
    if any((r["width"], r["height"]) != (w, h) for r in sized):
        return {"width": None, "height": None, "frames": [], "error": "barrier results differ in frame size"}
    return {"concept": "+".join(r["concept"] for r in sized), "fps": sized[0].get("fps"), "width": w, "height": h,
            "frames": [{"i": i, "objects": objs} for i, objs in sorted(by_i.items())]}


def ref_kind(concept: str) -> dict:
    if concept == "paper":
        return ({"kind": "rect", "size_cm": [21.0, 29.7]} if os.environ.get("BL_PAPER_KIND") == "a4"
                else {"kind": "a4x4"})
    if os.environ.get("BL_TACTILE_BLOCKS"):  # explicit block count: the old fixed-size reading
        return {"kind": "tactile_block", "n_blocks": int(os.environ["BL_TACTILE_BLOCKS"])}
    return {"kind": "tactile_strip", "width_cm": float(os.environ.get("BL_TACTILE_WIDTH_CM", "30"))}


# ---------------------------------------------------------------- model calls

async def _triage(jpgs: list[bytes], meta: dict, replay: bool) -> dict:
    if replay:
        with _spark_replay():
            return await spark.triage(jpgs, meta)
    backend = spark._backend()
    if backend.name == "replay":
        return await spark.triage(jpgs, meta)
    _check_cap(0.0, "Muse Spark triage")
    out = await spark.triage(jpgs, meta)
    _ledger({"kind": "spark.triage", "backend": backend.name, "model": out.get("model"), "usd": None,
             "usage": (out.get("call") or {}).get("usage")})
    return out


async def _explain(measurement: dict, replay: bool) -> dict:
    if replay:
        with _spark_replay():
            return await spark.explain(measurement)
    backend = spark._backend()
    if backend.name != "replay":
        _check_cap(0.0, "Muse Spark explain")
    out = await spark.explain(measurement)
    if backend.name != "replay" and out.get("call"):
        _ledger({"kind": "spark.explain", "backend": backend.name, "model": out.get("model"), "usd": None,
                 "usage": out["call"].get("usage")})
    return out


async def _segment(clip: Path, concept: str, n_frames: int, replay: bool, fixture_sha: str | None) -> dict:
    try:
        if replay:
            res = await sam.segment_video(str(clip), concept, mode="replay")
            if not res.get("frames") and fixture_sha:
                alt = sam._replay(fixture_sha, sam.normalize_concept(concept))
                if alt.get("replay_fixture"):
                    res = alt
            return res
        est = max(sam.MIN_BILLED_FRAMES, n_frames) * sam.USD_PER_FRAME
        _check_cap(est, f"SAM 3.1 '{concept}'")
        res = await sam.segment_video(str(clip), concept, mode="live")
        if res.get("cache") != "hit" and res.get("source") == "meta-api":
            _ledger({"kind": "sam", "concept": concept, "usd": res.get("cost_usd") or est,
                     "frames_billed": res.get("frames_billed"), "response_id": res.get("response_id"),
                     "cost_basis": res.get("cost_basis")})
        return res
    except SpendCapError:
        raise
    except (sam.SamError, ValueError) as e:
        return {"concept": concept, "width": None, "height": None, "frames": [], "source": "error",
                "error": str(e)[:300]}


# ---------------------------------------------------------------- main entry

def _unknown(why: str) -> dict:
    return {"gap_cm": None, "band_cm": None, "verdict": "unknown", "why": why, "detail": {}}


# Why an "unknown" was unknown, in plain words, from geometry's own numbers (never from the model). The explanation
# must name this cause; a model sentence that blames something else ("the view was unclear") is replaced.
# (reason, next step, words the explanation must contain one of)
UNKNOWN_CAUSES = {
    "tilt": ("the phone's tilt was not recorded, so the gap could be anywhere from {lo} to {hi} cm",
             "Allow the motion sensor when you film so the tilt is recorded, or calibrate once in your profile.",
             ("tilt", "angle", "기울")),
    "height": ("we do not know how high you held the phone, so the gap could be anywhere from {lo} to {hi} cm",
               "Set your armrest height in your profile, or calibrate once.", ("high", "height", "높이")),
    "field of view": ("we do not know how wide your phone camera sees, so the gap could be anywhere from {lo} to "
                      "{hi} cm", "Calibrate once in your profile so we know your camera.", ("camera", "화각", "카메라")),
    "roll": ("the phone was leaning to one side, so the gap could be anywhere from {lo} to {hi} cm",
             "Hold the phone level when you film.", ("level", "side", "lean", "수평", "기울")),
    "mask edges": ("the edges of the barriers were hard to pin down, so the gap could be anywhere from {lo} to {hi} cm",
                   "Film a little closer and hold the phone steady.", ("edge", "가장자리")),
    # not an error source: a pass from default assumptions alone is held back (apply_path_and_mode)
    "default_pass": ("this was measured from default assumptions only (no tilt reading, reference or calibration), and "
                     "we only say a chair fits when one of those confirms it",
                     "Turn on 'Use phone tilt' and film again, or put four A4 sheets flat on the ground near the gap.",
                     ("default", "assum", "confirm", "tilt", "가정", "기울")),
}


def apply_path_and_mode(m: dict, tri: dict | None, mode: str) -> dict:
    """Models may only make a verdict more cautious, never more hopeful.
    - Muse Spark saw the sidewalk closed: blocked, whatever the geometry measured (its numbers move to detail).
    - A pass measured from default assumptions alone (no reference, tilt sensor or calibration) is not reported as a
      pass: the gap may be between the wrong objects and nothing confirmed the scale. It becomes unknown with that cause.
    """
    d = dict(m.get("detail") or {})
    path = (tri or {}).get("path") or {}
    if path.get("state") == "closed" and m.get("verdict") != "blocked":
        d.update({"override": "path_closed", "geometry": {k: m.get(k) for k in ("verdict", "gap_cm", "band_cm", "why")}})
        return {**m, "verdict": "blocked", "gap_cm": None, "band_cm": None, "detail": d,
                "why": "Muse Spark saw the sidewalk closed (" + (path.get("evidence") or "no detail") + "), so the "
                       "measured gap is not used"}
    if mode == "default" and m.get("verdict") == "passes":
        d.update({"capped": "default_pass", "geometry": {"verdict": "passes"}})
        return {**m, "verdict": "unknown", "detail": d,
                "why": f"{m.get('why')}; not reported as a pass because it was measured from default assumptions only"}
    return m


def unknown_cause(m: dict) -> dict | None:
    """{"reason", "next_step", "keywords"} for a band too wide to decide, or None (not unknown, or no gap at all)."""
    d = m.get("detail") or {}
    band, dom = m.get("band_cm"), d.get("capped") or d.get("dominant_error")
    if m.get("verdict") != "unknown" or not band or dom not in UNKNOWN_CAUSES:
        return None
    reason, step, words = UNKNOWN_CAUSES[dom]
    return {"reason": reason.format(lo=round(band[0]), hi=round(band[1])), "next_step": step, "keywords": words}


def cause_text(cause: dict) -> str:
    """Deterministic explanation for an unknown with a known cause."""
    return (f"We cannot say yet whether your wheelchair fits: {cause['reason']}. {cause['next_step']} "
            "Four A4 sheets taped flat on the ground near the gap also work.")


# replaces the fixed fallback text for "unknown", which asked for a reference as if it were required
UNKNOWN_EXPLANATION = ("We could not measure this gap, so we cannot say whether your wheelchair fits. Film again with "
                       "both sides of the gap and the ground in front of it in view. A reference on the ground (four "
                       "A4 sheets or a tactile paving strip) is optional; a one-time calibration in your profile makes "
                       "the measurement more accurate.")


async def analyze(video_path: str | Path, lat: float | None, lon: float | None, replay: bool = False, *,
                  concepts_hint: list[str] | None = None, fixture_sha: str | None = None,
                  require_seated_height: bool = True, sample: bool = False, profile: dict | None = None,
                  sensor: dict | None = None) -> dict:
    """Run the whole check on one clip. See the module docstring.

    profile: the user's calibration profile (app.calibration.clean_profile fields: chair width, armrest height,
    phone FOV, one-time calibration). sensor: averaged DeviceOrientation beta/gamma/angle/n from the page. With no
    reference in the clip, the gap is measured from the camera_pose built from these (calibration.camera_pose);
    result["assumptions"] says which mode was used ("reference", "calibrated", "default").

    concepts_hint: barrier concepts to use when triage has no answer in replay mode (sample clips).
    fixture_sha: replay SAM fixtures recorded for this source-clip hash when the blurred clip has none.
    Raises AnalyzeError for unreadable video, SpendCapError over the cap, blur.BlurError when faces cannot be
    blurred, spark.SparkError when live triage fails.
    """
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    replay = bool(replay or not key("META_API_KEY"))
    src = Path(video_path)
    if not src.is_file():
        raise AnalyzeError("video file not found")

    prof = calibration.clean_profile(profile)
    hints = await calibration.clip_hints(src)
    chair_cm, chair_src = calibration.chair_width_cm(prof, geometry.DEFAULT_WHEELCHAIR_CM)
    clip, blur_counts, job = await prepare(src)
    info = clip_info(clip)
    timings["prepare_s"] = round(time.monotonic() - t0, 2)
    base = {"job": job, "replay": replay, "sample": sample, "location": {"lat": lat, "lon": lon},
            "frame_size": [info["width"], info["height"]], "fps": info["fps"], "n_frames": info["frames"],
            "video_url": f"/api/analyze/clip/{job}", "blur": blur_counts}
    notes: list[str] = []

    # triage
    t = time.monotonic()
    jpgs, meta = triage_frames(clip, info["frames"])
    meta["require_seated_height"] = require_seated_height
    try:
        tri = await _triage(jpgs, meta, replay)
    except spark.SparkError:
        if not replay:
            raise
        tri = {"usable": None, "model": None, "source": "skipped",
               "reasons": ["Not checked: replay mode, and no cached Muse Spark triage for these frames."],
               "barriers": [{"concept": c, "frames": [], "evidence": "sample concept (no cached triage)"}
                            for c in concepts_hint or []],
               "reference": {"visible": False, "frames": [], "evidence": ""}}
        if concepts_hint:
            notes.append("Muse Spark triage had no cached answer for these frames; sample concepts were used instead.")
    tri["frames_sent"] = meta["frame_indices"]
    timings["triage_s"] = round(time.monotonic() - t, 2)
    models = {"triage": tri.get("model"), "sam": sam.MODEL, "explain": None, "blur": blur_counts.get("detector")}

    if tri.get("usable") is False or not tri.get("barriers"):
        why = ("The clip could not be used: " + ("; ".join(tri.get("reasons") or []) or "no reason given") + "."
               if tri.get("usable") is False else
               "Nothing to measure: " + " ".join(tri.get("reasons") or ["no barrier was named."]))
        timings["total_s"] = round(time.monotonic() - t0, 2)
        return _result(base, _unknown(why), None, tri, [], models, timings, notes, 0)

    # SAM: barriers (cap) + one reference
    t = time.monotonic()
    seen, barriers = set(), []
    for b in tri.get("barriers") or []:
        # hints name fixture concepts exactly; aliases are for concepts triage proposes
        c = b["concept"] if tri.get("source") == "skipped" else CONCEPT_ALIASES.get(b["concept"], b["concept"])
        if c not in seen and c not in REF_CONCEPTS:
            seen.add(c)
            barriers.append(c)
    barriers = barriers[:MAX_BARRIERS]
    ref_concept = "paper" if (tri.get("reference") or {}).get("visible") else "tactile paving"
    results = await asyncio.gather(*[_segment(clip, c, info["frames"], replay, fixture_sha)
                                     for c in [*barriers, ref_concept]])
    size = (info["width"], info["height"])
    results = [r if not r.get("width") or (r["width"], r["height"]) == size else
               {**r, "frames": [], "error": f"result frame size {r['width']}x{r['height']} is not this clip's "
                                            f"{size[0]}x{size[1]} (generic fixture?); ignored"}
               for r in results]
    bar_results, ref_result = list(results[:-1]), results[-1]
    timings["sam_s"] = round(time.monotonic() - t, 2)
    sam_frames = sum(int(r.get("frames_billed") or 0) for r in results
                     if r.get("source") == "meta-api" and r.get("cache") != "hit")
    for r in results:
        if r.get("error"):
            notes.append(f"SAM '{r['concept']}' failed: {r['error']}")
        if r.get("source") == "replay" and not r.get("replay_fixture") and not r.get("frames"):
            notes.append(f"SAM '{r['concept']}': no cached or fixture result (replay mode).")

    # geometry
    t = time.monotonic()
    merged = merge_barriers(bar_results)
    pose = calibration.camera_pose(prof, sensor, hints, size)
    mode = "reference" if ref_result.get("frames") else pose["source"]
    if not merged["frames"]:
        m = _unknown("No barrier was segmented in the clip, so there is nothing to measure.")
    elif mode != "reference":
        m = await asyncio.to_thread(calibration.measure_with_pose, geometry, merged, pose, chair_cm)
        m.setdefault("detail", {})["reference"] = {"concept": None, **pose}
    else:
        # pose too: when the reference never shares a frame with the barriers this falls back to the camera pose,
        # which must be the user's calibrated / sensor pose, not the default
        m = await asyncio.to_thread(pipeline.measure_from_sam, merged, ref_result, ref_kind(ref_concept),
                                    pose=pose, wheelchair_cm=chair_cm)
        if (m.get("detail") or {}).get("fallback") == "camera_pose":
            mode = pose["source"]
            m["detail"]["reference"] = {"concept": None, **pose}
        else:
            m.setdefault("detail", {})["reference"] = {"concept": ref_concept, **ref_kind(ref_concept)}
    m = apply_path_and_mode(m, tri, mode)
    timings["measure_s"] = round(time.monotonic() - t, 2)
    assumptions = {"mode": mode, "label": calibration.LABELS[mode], "chair_width_cm": chair_cm,
                   "chair_source": chair_src, "pose": None if mode == "reference" else pose, "clip_hints": hints,
                   "main": calibration.assumption_text(mode, pose, chair_cm, chair_src, ref_concept)}
    if mode != "reference" and pose["pitch_deg"] is None:
        # say how geometry actually got the tilt: ground lines only when it used them, else its wide assumed range
        used = ((m.get("detail") or {}).get("pose") or {}).get("pitch_source") == "ground lines"
        assumptions["main"] = assumptions["main"].replace(
            "Tilt not recorded, so it is estimated from lines on the ground.",
            "Tilt not recorded, so it is estimated from lines on the ground." if used else
            "Tilt not recorded, so a wide range of tilts is assumed; the motion sensor or a calibration narrows it.")

    # explanation
    t = time.monotonic()
    ex_in = {k: m.get(k) for k in ("gap_cm", "band_cm", "verdict", "why")}
    ex_in.update({"chair_width_cm": chair_cm, "barrier": ", ".join(barriers) or None})
    cause = unknown_cause(m)
    if cause:
        ex_in.update({"reason": cause["reason"], "next_step": cause["next_step"]})
    ex = await _explain(ex_in, replay)
    if cause and (ex.get("model") == "template"
                  or not any(w in (ex.get("text") or "").lower() for w in cause["keywords"])):
        # the model's sentence does not name the measured cause (or there was no model): say it ourselves
        ex = {**ex, "text": cause_text(cause), "model": "template",
              "warnings": (ex.get("warnings") or []) + ["explanation did not name the measured cause; replaced"]}
    elif ex.get("model") == "template" and m.get("verdict") == "unknown":
        ex = {**ex, "text": UNKNOWN_EXPLANATION}
    models["explain"] = ex.get("model")
    timings["explain_s"] = round(time.monotonic() - t, 2)

    masks = [mask_track(r, "barrier") for r in bar_results] + [mask_track(ref_result, "reference")]
    timings["total_s"] = round(time.monotonic() - t0, 2)
    return {**_result(base, m, ex, tri, masks, models, timings, notes, sam_frames), "assumptions": assumptions}


def _result(base, m, ex, tri, masks, models, timings, notes, sam_frames) -> dict:
    return {
        **base,
        "verdict": m["verdict"], "gap_cm": m["gap_cm"], "band_cm": m["band_cm"], "why": m["why"],
        "explanation": (ex or {}).get("text") or "",
        "triage": tri, "masks": masks, "measurement_detail": m.get("detail") or {},
        "cost": {"sam_frames": sam_frames, "usd_estimate": round(sam_frames * sam.USD_PER_FRAME, 4),
                 "spark_usd": "not priced", "ledger_total_usd": spent_usd(), "cap_usd": spend_cap_usd()},
        "models": models, "timings": timings, "notes": notes,
    }


# ---------------------------------------------------------------- bundled sample

def _sample_source(only_sha: str | None = None) -> tuple[Path, str, list[str]] | None:
    """A local dev clip that has SAM fixtures: (clip, fixture clip sha, barrier concepts with matches).

    Fixtures name the clip SAM saw (data/cache/sam/clips/<sha16>-...mp4, frame numbers refer to it). Prefers a
    clip whose fixtures include a reference concept with matches."""
    groups: dict[str, dict] = {}
    for p in sorted(sam.FIXTURE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        sha, name = d.get("clip_sha256"), d.get("clip_name")
        if not sha or not name or not d.get("frames") or (only_sha and sha != only_sha):
            continue
        g = groups.setdefault(sha, {"name": name, "bar": [], "ref": [], "objects": 0})
        g["objects"] += sum(len(f.get("objects", [])) for f in d["frames"])
        c = sam.normalize_concept(d.get("concept", ""))
        if c in ("sidewalk", "pavement", "road", "curb"):  # ground-line probe fixtures, not barriers
            continue
        (g["ref"] if c in REF_CONCEPTS or "tactile" in c or "paper" in c else g["bar"]).append(c)
    best = None
    for sha, g in groups.items():
        clip = sam.CACHE_DIR / "clips" / g["name"]
        if not clip.exists():  # newer fixtures name the source clip; SAM saw the prepared one
            clip = next(iter(sorted((sam.CACHE_DIR / "clips").glob(f"{sha[:16]}-*.mp4"))), clip)
        if not clip.exists() or not g["bar"]:
            continue
        score = (bool(g["ref"]), len(g["bar"]), g["objects"])
        if best is None or score > best[0]:
            best = (score, clip, sha, g["bar"])
    return None if best is None else (best[1], best[2], best[3])


async def run_sample(lat: float | None = None, lon: float | None = None, only_sha: str | None = None,
                     profile: dict | None = None, sensor: dict | None = None, name: str | None = None) -> dict:
    """Sample check in replay mode on a local dev clip with fixtures; else the recorded live run (a deploy has no
    dev clips); else the precomputed JSON."""
    if name:  # a named recorded run, wherever this runs
        return _recorded(name, lat, lon, profile, sensor)
    src = _sample_source(only_sha)
    if src is not None:
        clip, sha, concepts = src
        out = await analyze(clip, lat, lon, replay=True, concepts_hint=concepts, fixture_sha=sha,
                            require_seated_height=False, sample=True, profile=profile, sensor=sensor)
        out["sample_source"] = {"clip": clip.name, "fixture_clip_sha256": sha}
        return out
    if not only_sha and DEMO_RUNS["scooters"].exists():
        return _recorded("scooters", lat, lon, profile, sensor)
    if SAMPLE_FALLBACK.exists():
        out = json.loads(SAMPLE_FALLBACK.read_text())
        out.update({"location": {"lat": lat, "lon": lon}, "video_url": None, "precomputed": True})
        return out
    raise AnalyzeError("no sample is available: no dev clip with SAM fixtures and no precomputed sample JSON")


def _recorded(name: str, lat, lon, profile, sensor) -> dict:
    p = DEMO_RUNS.get(name)
    if p is None or not p.exists():
        raise AnalyzeError(f"no recorded sample named {name!r}; try one of: {', '.join(DEMO_RUNS)}")
    out = json.loads(p.read_text())
    if lat is not None and lon is not None:
        out["location"] = {"lat": lat, "lon": lon}
    if profile or sensor:
        out["notes"] = (out.get("notes") or []) + ["This is a recorded run, so your calibration and tilt are not applied to it."]
    return out


def build_sample_fallback() -> Path:
    """Write the precomputed sample JSON (no video) from a replay run. python -m app.analyze --build-sample"""
    res = asyncio.run(run_sample(only_sha=SAMPLE_FALLBACK_SHA))
    if res.get("precomputed"):
        raise SystemExit("no local sample clip; cannot rebuild")
    res = {**res, "note": "Precomputed by `python -m app.analyze --build-sample` (replay mode, no paid calls). "
                          "Served by /api/sample when the dev clip is not on this machine.",
           "video_url": None, "location": None, "job": None}
    SAMPLE_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_FALLBACK.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")))
    return SAMPLE_FALLBACK


if __name__ == "__main__":
    import sys
    if "--build-sample" in sys.argv:
        print(build_sample_fallback())
    else:
        print(__doc__)
