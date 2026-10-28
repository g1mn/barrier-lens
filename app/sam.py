"""SAM 3.1 video segmentation on the Meta Model API, with a content-hash cache and an offline replay mode.

Shared interface:
    async def segment_video(video_path: str, concept: str) -> dict
    -> {"concept", "fps", "width", "height",
        "frames": [{"i": int, "objects": [{"id": int, "score": float|None, "box": [x0, y0, x1, y1],
                                            "mask_rle": {"size": [h, w], "order": "row-major", "counts": [...]}}]}],
        "source": "meta-api" | "replay", ...extra keys documented in _result()}
    decode_mask(obj) -> numpy bool array (height, width) for one object (mask_rle or polygon).

Wire facts used here (dev.meta.ai/docs/sam/segmenting, /docs/sam/reading-segmentation, /docs/pricing-rate-limits,
read 2026-10-28):
- POST https://api.meta.ai/v1/responses, model "sam-3.1", "Authorization: Bearer <key>", one user message with one
  input_text noun phrase (one concept per request) and one input_video (video_url, data URL or Files API file_id).
- With stream=true each frame arrives as one response.output_text.delta line:
  <Nf>id<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..[;c=..]|><|mask;x=0;y=0[;c=..];data=H,W,<enc>payload|>,id<|box..
  Frames with nothing visible emit no line. The stream ends with response.completed; its
  response.usage.video_frames_processed is the billed frame count ($0.20 per 1,000 frames, at least 50 per request).
- Box x2/y2 are inclusive. The mask raster is box-local (H,W equal the box height,width in every documented example;
  the docs only say "smaller than the frame"). First real call 2026-10-28 (720x1280, 30 frames, 60 masks): 0 needed
  resizing and every decoded mask stayed inside its box.
- Concept wording matters: on the same clip "e-scooter" returned zero matches, "scooter" returned 2 tracked objects.
  The payload codec is undocumented, so masks are decoded with Meta's meta-sam-parser (decode_mask_to_raster).

Never logs or prints the API key.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path

import httpx
import numpy as np
from meta_sam_parser import SegmentationMask, decode_mask_to_raster

from app.config import key

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache" / "sam"
FIXTURE_DIR = ROOT / "data" / "fixtures" / "sam"

API_BASE = "https://api.meta.ai/v1"
MODEL = "sam-3.1"
USD_PER_FRAME = 0.20 / 1000
MIN_BILLED_FRAMES = 50
MAX_FRAMES = 15_000
UPLOAD_TTL_S = 7 * 24 * 3600  # Files API allows 3600..2592000
VIDEO_CODECS = {"h264", "hevc", "mpeg4"}
RETRYABLE_STATUS = {429, 500, 502, 503}
RETRYABLE_CODES = {"rate_limit_exceeded", "server_error", "server_shutting_down", "service_overloaded",
                   "backend_unavailable"}
TIMEOUT = httpx.Timeout(connect=15, read=300, write=300, pool=15)


class SamError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 retry_after: float | None = None, retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.retry_after, self.retryable = status, code, retry_after, retryable


# ---------------------------------------------------------------- settings, concept, cache paths

def _settings() -> dict:
    """Everything that changes what SAM returns for one clip + concept. Part of the cache key."""
    return {
        "model": MODEL,
        "mask_encoding": os.environ.get("SAM_MASK_ENCODING", "one_bit"),
        "include_confidence": "true",
        "prep": os.environ.get("SAM_PREP", "1") != "0",
        "max_fps": float(os.environ.get("SAM_MAX_FPS", "10")),
        "max_side": int(os.environ.get("SAM_MAX_SIDE", "720")),
    }


def normalize_concept(concept: str) -> str:
    """Case, extra whitespace and trailing punctuation do not change SAM's match (docs), so fold them."""
    c = " ".join(str(concept).split()).strip().rstrip(".!?").strip().lower()
    if not c:
        raise ValueError("concept is empty")
    if any(ch in c for ch in ",;+"):
        raise ValueError(f"one concept per request: {concept!r} is a list, which SAM 3.1 answers with zero "
                         "matches and no error (dev.meta.ai/docs/sam/segmenting). Call once per concept.")
    if c.startswith("<"):
        raise ValueError("box/point prompts are not supported by segment_video; pass a short noun phrase")
    return c


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "concept"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_paths(sha: str, concept: str, settings: dict) -> tuple[Path, Path]:
    tag = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:8]
    base = CACHE_DIR / sha[:16] / f"{_slug(concept)}--{tag}"
    return base.with_suffix(".json"), base.with_suffix(".raw.txt")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- masks

def encode_rle(mask: np.ndarray) -> dict:
    """Binary mask (h, w) -> uncompressed row-major RLE. counts alternate 0-runs and 1-runs, starting with 0s."""
    m = np.asarray(mask, dtype=bool)
    flat = m.ravel()
    if flat.size == 0:
        return {"size": list(m.shape), "order": "row-major", "counts": []}
    change = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    counts = np.diff(np.concatenate(([0], change, [flat.size]))).tolist()
    if flat[0]:
        counts.insert(0, 0)
    return {"size": [int(m.shape[0]), int(m.shape[1])], "order": "row-major", "counts": counts}


def _rle_to_array(rle: dict) -> np.ndarray:
    h, w = (int(v) for v in rle["size"])
    counts = np.asarray(rle["counts"], dtype=np.int64)
    values = np.arange(counts.size) % 2 == 1
    flat = np.repeat(values, counts)
    if flat.size != h * w:
        raise ValueError(f"RLE covers {flat.size} pixels, expected {h}x{w}={h * w}")
    order = "F" if rle.get("order") == "column-major" else "C"  # column-major = COCO convention
    return flat.reshape((h, w), order=order)


def _fill_polygon(points, width: int, height: int) -> np.ndarray:
    """Even-odd fill: a pixel is inside when its centre is inside the polygon."""
    pts = np.asarray(points, dtype=float)
    out = np.zeros((height, width), dtype=bool)
    if pts.ndim != 2 or len(pts) < 3:
        return out
    x, y = pts[:, 0], pts[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    for row in range(height):
        yc = row + 0.5
        cross = (y <= yc) != (y2 <= yc)
        if not cross.any():
            continue
        xs = np.sort(x[cross] + (yc - y[cross]) * (x2[cross] - x[cross]) / (y2[cross] - y[cross]))
        for a, b in zip(xs[0::2], xs[1::2]):
            lo, hi = max(int(np.ceil(a - 0.5)), 0), min(int(np.floor(b - 0.5)), width - 1)
            if hi >= lo:
                out[row, lo:hi + 1] = True
    return out


def decode_mask(obj: dict, frame_size: tuple[int, int] | None = None) -> np.ndarray:
    """One object (or a bare RLE dict) -> bool array of shape (height, width), full frame.

    Accepts {"mask_rle": {...}}, a bare {"size", "counts"} dict, or {"polygon": [[x, y], ...]}.
    frame_size is (width, height) and is needed only for polygons.
    """
    if "counts" in obj and "size" in obj:
        return _rle_to_array(obj)
    if obj.get("mask_rle") is not None:
        return _rle_to_array(obj["mask_rle"])
    if obj.get("polygon") is not None:
        if frame_size is None:
            raise ValueError("polygon masks need frame_size=(width, height)")
        return _fill_polygon(obj["polygon"], int(frame_size[0]), int(frame_size[1]))
    raise ValueError("object has neither mask_rle nor polygon")


def masks_for_frame(result: dict, i: int) -> dict[int, np.ndarray]:
    """{object id: full-frame bool mask} for frame i of a segment_video result (empty if no line for i)."""
    size = (result["width"], result["height"])
    for fr in result["frames"]:
        if fr["i"] == i:
            return {o["id"]: decode_mask(o, size) for o in fr["objects"]}
    return {}


def _wire_to_rle(enc: str, payload: str, mh: int, mw: int, box: list[int], width: int, height: int) -> tuple[dict, bool]:
    """Decode one box-local wire mask and place it in the full frame. Returns (rle, resized)."""
    raster = decode_mask_to_raster(SegmentationMask(encoding=enc, payload=payload, width=mw, height=mh))
    local = np.frombuffer(raster, dtype=np.uint8).reshape(mh, mw).astype(bool)
    x0, y0, x1, y1 = box
    bh, bw = y1 - y0, x1 - x0
    resized = (mh, mw) != (bh, bw)
    if resized:  # not seen in the docs; nearest-neighbour so the mask still covers its box
        local = local[(np.arange(bh) * mh) // bh][:, (np.arange(bw) * mw) // bw]
    full = np.zeros((height, width), dtype=bool)
    full[y0:y1, x0:x1] = local[: height - y0, : width - x0]
    return encode_rle(full), resized


def _decode_frame_task(items: list, width: int, height: int) -> list[tuple[dict, bool]]:
    return [_wire_to_rle(*it, width, height) for it in items]


def _decode_worker() -> None:
    """Subprocess entry point: {"jobs", "width", "height"} JSON on stdin -> decoded RLEs as JSON on stdout."""
    req = json.load(sys.stdin)
    json.dump([_decode_frame_task(job, req["width"], req["height"]) for job in req["jobs"]], sys.stdout)


# Plain `python -c` workers rather than multiprocessing: spawn re-imports the caller's __main__, which breaks
# any script without an `if __name__ == "__main__"` guard.
_WORKER = "import sys; sys.path.insert(0, sys.argv[1]); from app.sam import _decode_worker; _decode_worker()"


async def _decode_parallel(jobs: list, width: int, height: int) -> list:
    n = max(1, min(8, (os.cpu_count() or 2) - 1, len(jobs)))
    size = -(-len(jobs) // n)

    async def run(chunk: list) -> list:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", _WORKER, str(ROOT),
                                                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(json.dumps({"jobs": chunk, "width": width, "height": height}).encode())
        if proc.returncode != 0:
            raise SamError(f"mask decode worker failed: {err.decode(errors='replace')[-300:]}", code="decode_failed")
        return json.loads(out)

    parts = await asyncio.gather(*(run(jobs[k:k + size]) for k in range(0, len(jobs), size)))
    return [frame for part in parts for frame in part]


# ---------------------------------------------------------------- wire parsing

_LINE_RE = re.compile(r"<(\d+)f(?:;[^>]*)?>(.*)")
_RECORD_RE = re.compile(r",?(\d+)((?:<\|[A-Za-z][\w.-]*(?:;[^|]*)?\|>)+)")
_TOKEN_RE = re.compile(r"<\|([A-Za-z][\w.-]*)(?:;([^|]*))?\|>")


def _fields(body: str | None) -> dict[str, str]:
    out = {}
    for part in (body or "").split(";"):
        k, sep, v = part.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def parse_output(text: str) -> tuple[list[dict], tuple[int, int] | None, list[str]]:
    """SAM special-token text -> (frames with wire masks, (width, height) or None, problems).

    The payload alphabet never contains | or ; (meta-sam-parser grammar notes), so tokens end at |> and
    ; separates fields. Keys are read by name, so the optional c= field can sit anywhere.
    """
    frames: dict[int, list[dict]] = {}
    size = None
    problems: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.fullmatch(line)
        if not m:
            problems.append(f"line {n}: not a frame line: {line[:80]!r}")
            continue
        fi, body, pos = int(m.group(1)), m.group(2), 0
        objs = frames.setdefault(fi, [])
        while pos < len(body):
            rm = _RECORD_RE.match(body, pos)
            if not rm:
                problems.append(f"line {n}: unparsed tail at {pos}: {body[pos:pos + 60]!r}")
                break
            pos = rm.end()
            toks = {t.group(1): _fields(t.group(2)) for t in _TOKEN_RE.finditer(rm.group(2))}
            bx, mk = toks.get("box"), toks.get("mask")
            try:
                x1, y1, x2, y2, w, h = (int(bx[k]) for k in ("x1", "y1", "x2", "y2", "w", "h"))
                mh, mw, payload = mk["data"].split(",", 2)
                mh, mw = int(mh), int(mw)
            except (TypeError, KeyError, ValueError):
                problems.append(f"line {n}: malformed record for object {rm.group(1)}")
                continue
            if not (0 <= x1 <= x2 < w and 0 <= y1 <= y2 < h) or payload[:1] not in ("!", "~"):
                problems.append(f"line {n}: invalid box or mask marker for object {rm.group(1)}")
                continue
            if size is not None and size != (w, h):
                problems.append(f"line {n}: frame size {w}x{h} differs from {size[0]}x{size[1]}")
            size = size or (w, h)
            c = bx.get("c", mk.get("c"))
            objs.append({
                "id": int(rm.group(1)),
                "score": float(c) if c is not None else None,
                "box": [x1, y1, x2 + 1, y2 + 1],  # half-open
                "_wire": ("one_bit" if payload[0] == "!" else "lossless", payload, mh, mw),
            })
    out = [{"i": i, "objects": objs} for i, objs in sorted(frames.items()) if objs]
    return out, size, problems


async def _decode_frames(frames: list[dict], width: int, height: int) -> int:
    """Replace each object's _wire with mask_rle in place. Large jobs go to worker subprocesses (the decoder is pure
    Python, about 1.7 us per box pixel measured on this Mac). Returns how many masks needed resizing to their box."""
    jobs = [[(*o["_wire"], o["box"]) for o in fr["objects"]] for fr in frames]
    pixels = sum(it[2] * it[3] for job in jobs for it in job)
    if pixels > 1_500_000 and (os.cpu_count() or 1) > 2:
        done = await _decode_parallel(jobs, width, height)
    else:
        done = [_decode_frame_task(job, width, height) for job in jobs]
    resized = 0
    for fr, rles in zip(frames, done):
        for o, (rle, was_resized) in zip(fr["objects"], rles):
            o.pop("_wire")
            o["mask_rle"] = rle
            resized += was_resized
    return resized


# ---------------------------------------------------------------- clip probing and preparation

def _ratio(text: str | None) -> float | None:
    try:
        a, _, b = (text or "").partition("/")
        v = float(a) / float(b or 1)
        return v if v > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


async def _run(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def probe_video(path: Path) -> dict | None:
    """ffprobe -> {codec, container, fps, width, height (as displayed), rotation, frame_count, duration_s}.
    None when ffprobe is not installed. Raises SamError when the file is not a readable video."""
    if not shutil.which("ffprobe"):
        return None
    rc, out, err = await _run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration"
                              ":stream_side_data=rotation:stream_tags=rotate:format=format_name,duration",
                              "-of", "json", str(path))
    try:
        j = json.loads(out)
        s = j["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise SamError(f"not a readable video: {path.name} ({err.strip()[:200]})", code="bad_video")
    rot = 0
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(float((s.get("tags") or {}).get("rotate", 0) or 0))
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = s.get("duration") or (j.get("format") or {}).get("duration")
    nb = s.get("nb_frames")
    return {
        "codec": s.get("codec_name"),
        "container": (j.get("format") or {}).get("format_name"),
        "fps": _ratio(s.get("avg_frame_rate")) or _ratio(s.get("r_frame_rate")),
        "width": w, "height": h, "rotation": rot,
        "frame_count": int(nb) if nb and str(nb).isdigit() else None,
        "duration_s": float(dur) if dur else None,
    }


async def prepare_clip(video_path: str | Path, sha: str | None = None, settings: dict | None = None) -> dict:
    """Make the clip SAM will see: constant frame rate capped at SAM_MAX_FPS (default 10), short side capped at
    SAM_MAX_SIDE (default 720), rotation baked in, H.264 MP4 with faststart, no audio. Fewer frames = lower cost
    and faster decode; frame indices in the result refer to THIS clip, so play/extract frames from result["clip"].
    SAM_PREP=0 sends the original file instead."""
    src = Path(video_path)
    settings = settings or _settings()
    sha = sha or _sha256(src)
    info = await probe_video(src)
    if not settings["prep"] or info is None or not shutil.which("ffmpeg"):
        if info and info["codec"] not in VIDEO_CODECS:
            raise SamError(f"SAM accepts H.264/H.265/MPEG-4 in MP4/MOV; {src.name} is {info['codec']}", code="bad_video")
        return {"path": src, "sha256": sha, "prepped": False, **(info or {})}
    fps = info["fps"] or 30.0
    if settings["max_fps"] > 0:
        fps = min(fps, settings["max_fps"])
    fps = round(fps, 3)
    w, h = info["width"], info["height"]
    side = settings["max_side"]
    if side > 0 and min(w, h) > side:
        k = side / min(w, h)
        w, h = int(round(w * k / 2)) * 2, int(round(h * k / 2)) * 2
    out = CACHE_DIR / "clips" / f"{sha[:16]}-{fps:g}fps-{w}x{h}.mp4"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".tmp.mp4")
        rc, _, err = await _run("ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0",
                                "-an", "-sn", "-dn", "-vf", f"fps={fps:g},scale={w}:{h}", "-c:v", "libx264",
                                "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                                "-movflags", "+faststart", str(tmp))
        if rc != 0:
            tmp.unlink(missing_ok=True)
            raise SamError(f"ffmpeg could not prepare {src.name}: {err.strip()[:300]}", code="prep_failed")
        os.replace(tmp, out)
    pinfo = await probe_video(out) or {}
    return {"path": out, "sha256": sha, "prepped": True, "source_fps": info["fps"],
            "source_size": [info["width"], info["height"]], **pinfo, "fps": fps}


# ---------------------------------------------------------------- Meta Model API client

def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


def _http_error(resp: httpx.Response, body: bytes) -> SamError:
    code = etype = None
    msg = body[:300].decode(errors="replace").strip() or "(empty body)"
    try:
        err = json.loads(body).get("error") or {}
        code, etype, msg = err.get("code"), err.get("type"), err.get("message") or msg
    except (ValueError, AttributeError):
        pass
    try:
        retry_after = float(resp.headers["retry-after"])
    except (KeyError, ValueError):
        retry_after = None
    return SamError(f"SAM API {resp.status_code} {etype or ''}/{code or ''}: {msg}", status=resp.status_code,
                    code=code, retry_after=retry_after, retryable=resp.status_code in RETRYABLE_STATUS)


_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


def _lock(name: str) -> asyncio.Lock:
    k = (name, id(asyncio.get_running_loop()))
    return _LOCKS.setdefault(k, asyncio.Lock())


async def _upload(client: httpx.AsyncClient, api_key: str, clip: Path, force: bool = False) -> str:
    """Upload once per clip (POST /v1/files, purpose=user_data, 7-day expiry); reuse the file id after that."""
    rec_path = CACHE_DIR / "uploads" / f"{_sha256(clip)[:16]}.json"
    async with _lock(str(rec_path)):
        if not force and rec_path.exists():
            rec = json.loads(rec_path.read_text())
            if rec.get("expires_at", 0) - time.time() > 3600:
                return rec["id"]
        ctype = "video/quicktime" if clip.suffix.lower() == ".mov" else "video/mp4"
        t0 = time.monotonic()
        r = await client.post(f"{API_BASE}/files", headers=_auth(api_key),
                              data={"purpose": "user_data", "expires_after[anchor]": "created_at",
                                    "expires_after[seconds]": str(UPLOAD_TTL_S)},
                              files={"file": (clip.name, clip.read_bytes(), ctype)})
        if r.status_code != 200:
            raise _http_error(r, r.content)
        j = r.json()
        rec = {"id": j["id"], "bytes": j.get("bytes"), "status": j.get("status"),
               "expires_at": j.get("expires_at") or time.time() + UPLOAD_TTL_S,
               "upload_s": round(time.monotonic() - t0, 3)}
        _write_atomic(rec_path, json.dumps(rec))
        log.info("sam upload %s -> %s in %.2fs", clip.name, rec["id"], rec["upload_s"])
        return rec["id"]


async def _stream_once(client: httpx.AsyncClient, api_key: str, body: dict) -> dict:
    """One streamed /v1/responses call. Returns deltas, terminal response object and timings."""
    t0 = time.monotonic()
    deltas: list[str] = []
    done_text = response = outcome = None
    first = None
    events: dict[str, int] = {}
    headers = {**_auth(api_key), "Accept": "text/event-stream"}
    async with client.stream("POST", f"{API_BASE}/responses", json=body, headers=headers) as r:
        if r.status_code != 200:
            raise _http_error(r, await r.aread())
        request_id = r.headers.get("x-request-id")
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            ev = json.loads(data)
            t = ev.get("type")
            events[t] = events.get(t, 0) + 1
            if t == "response.output_text.delta":
                first = first or time.monotonic() - t0
                deltas.append(ev.get("delta") or "")
            elif t == "response.output_text.done":
                done_text = ev.get("text")
            elif t in ("response.completed", "response.incomplete"):
                response, outcome = ev.get("response") or {}, t.split(".")[1]
            elif t == "response.failed":
                err = (ev.get("response") or {}).get("error") or {}
                raise SamError(f"SAM response failed: {err.get('code')}: {err.get('message')}", code=err.get("code"),
                               retryable=err.get("code") in RETRYABLE_CODES)
            elif t == "error":
                err = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                raise SamError(f"SAM stream error: {err.get('code')}: {err.get('message')}", code=err.get("code"),
                               retryable=err.get("code") in RETRYABLE_CODES)
            elif t and t.startswith("response.refusal"):
                raise SamError("SAM refused the request", code="refusal")
    if outcome is None:
        raise SamError("SAM stream ended without response.completed", code="eof", retryable=True)
    return {"deltas": deltas, "done_text": done_text, "response": response, "outcome": outcome,
            "latency_s": time.monotonic() - t0, "first_frame_s": first, "request_id": request_id, "events": events}


def _response_text(response: dict | None) -> str:
    """output_text parts of a terminal response object (fallback when the stream carried no text deltas)."""
    parts = []
    for item in (response or {}).get("output") or []:
        for c in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts)


async def _with_retries(fn, attempts: int = 4):
    for n in range(attempts):
        try:
            return await fn()
        except httpx.ConnectError as e:  # nothing was sent, safe to retry
            err = SamError(f"connect failed: {e}", code="connect", retryable=True)
        except SamError as e:
            err = e
        if not err.retryable or n == attempts - 1:
            raise err
        delay = err.retry_after if err.retry_after is not None else min(30.0, 0.5 * 2 ** n) + random.random() / 2
        log.warning("sam retry %d after %.1fs: %s", n + 1, delay, err)
        await asyncio.sleep(min(delay, 90.0))


async def _live(clip: dict, concept: str, settings: dict, api_key: str, client: httpx.AsyncClient) -> tuple[dict, str]:
    path = Path(clip["path"])
    if (clip.get("frame_count") or 0) > MAX_FRAMES:
        raise SamError(f"clip has {clip['frame_count']} frames; SAM takes at most {MAX_FRAMES} per request",
                       code="too_long")
    inline = os.environ.get("SAM_VIDEO_INPUT", "file") == "inline"

    async def video_part(force_upload: bool = False) -> dict:
        if inline:
            b64 = base64.b64encode(path.read_bytes()).decode()
            return {"type": "input_video", "video_url": f"data:video/mp4;base64,{b64}"}
        return {"type": "input_video", "file_id": await _with_retries(lambda: _upload(client, api_key, path, force_upload))}

    def body(part: dict) -> dict:
        return {"model": MODEL, "stream": True,
                "metadata": {"mask_encoding": settings["mask_encoding"],
                             "include_confidence": settings["include_confidence"]},
                "input": [{"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": concept}, part]}]}

    part = await video_part()
    try:
        res = await _with_retries(lambda: _stream_once(client, api_key, body(part)))
    except SamError as e:
        if e.code != "file_not_found" or inline:
            raise
        part = await video_part(force_upload=True)  # upload expired or was deleted
        res = await _with_retries(lambda: _stream_once(client, api_key, body(part)))
    text = "".join(res["deltas"])
    if len(res["deltas"]) > 1 and "\n" not in text:
        text = "\n".join(res["deltas"])  # docs: one delta per frame line; guard against deltas without newlines
    if not res["deltas"] and res["done_text"]:
        text = res["done_text"]
    if not text.strip():
        text = _response_text(res["response"])
    return res, text


# ---------------------------------------------------------------- results

async def _result_from_text(text: str, concept: str, clip: dict, source: str) -> dict:
    frames, size, problems = parse_output(text)
    width, height = size or (clip.get("width"), clip.get("height"))
    if size and clip.get("width") and size != (clip.get("width"), clip.get("height")):
        problems.append(f"SAM frame size {size[0]}x{size[1]} differs from probed {clip.get('width')}x{clip.get('height')}")
    resized = await _decode_frames(frames, width, height) if frames else 0
    if resized:
        problems.append(f"{resized} masks were not box-sized and were resized to their box")
    return {
        "concept": concept, "fps": clip.get("fps"), "width": width, "height": height,
        "frames": frames, "source": source, "model": MODEL,
        "frame_count": clip.get("frame_count"),
        "clip": {"path": _rel(Path(clip["path"])), "sha256": clip["sha256"], "prepped": clip.get("prepped", False),
                 "duration_s": clip.get("duration_s")},
        "diagnostics": problems,
    }


def _cost(usage: dict | None, frame_count: int | None) -> tuple[int | None, float | None, str]:
    billed = (usage or {}).get("video_frames_processed")
    if isinstance(billed, int):
        return billed, round(billed * USD_PER_FRAME, 6), "usage.video_frames_processed"
    if frame_count:
        est = max(MIN_BILLED_FRAMES, frame_count)
        return est, round(est * USD_PER_FRAME, 6), "estimate: max(50, frame_count); usage had no video_frames_processed"
    return None, None, "unknown"


def _replay(sha: str, concept: str) -> dict:
    """Fixture with the same clip hash and concept, else a generic fixture for the concept, else zero matches."""
    exact = generic = None
    for p in sorted(FIXTURE_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
            if normalize_concept(d.get("concept", "")) != concept:
                continue
        except ValueError:
            continue
        if d.get("clip_sha256") == sha:
            exact = (p, d)
            break
        if not d.get("clip_sha256") and generic is None:
            generic = (p, d)
    pick = exact or generic
    if pick is None:
        return {"concept": concept, "fps": None, "width": None, "height": None, "frames": [], "source": "replay",
                "model": MODEL, "replay_fixture": None, "cost_usd": 0.0,
                "diagnostics": [f"replay: no fixture for concept {concept!r}; returning zero matches"]}
    p, d = pick
    out = {**d, "concept": concept, "source": "replay", "replay_fixture": p.name, "cost_usd": 0.0}
    out.setdefault("diagnostics", [])
    if pick is generic:
        out["diagnostics"] = [*out["diagnostics"], "replay: generic fixture, not this clip (frame size is the fixture's)"]
    return out


async def segment_video(video_path: str, concept: str, *, mode: str | None = None,
                        client: httpx.AsyncClient | None = None, use_cache: bool = True) -> dict:
    """Segment one concept in one clip with SAM 3.1.

    mode: "auto" (default; env SAM_MODE) = live when META_API_KEY exists, else replay; "live"; "replay".
    The cache (data/cache/sam/<clip sha>/<concept>--<settings>.json) is checked first in every mode, so a clip +
    concept is paid for once. use_cache=False skips the read (the fresh result still overwrites the cache).
    """
    concept = normalize_concept(concept)
    src = Path(video_path)
    if not src.is_file():
        raise FileNotFoundError(video_path)
    settings = _settings()
    sha = _sha256(src)
    json_path, raw_path = _cache_paths(sha, concept, settings)
    if use_cache and json_path.exists():
        out = json.loads(json_path.read_text())
        out["cache"] = "hit"
        return out

    mode = (mode or os.environ.get("SAM_MODE") or "auto").lower()
    api_key = key("META_API_KEY")
    if mode == "replay" or (mode == "auto" and not api_key):
        return _replay(sha, concept)
    if not api_key:
        raise SamError("META_API_KEY is not set (add it to ~/.config/barrier-lens/.env) or use SAM_MODE=replay",
                       code="missing_key")

    async with _lock(f"prep:{sha}:{json.dumps(settings, sort_keys=True)}"):
        clip = await prepare_clip(src, sha, settings)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        res, text = await _live(clip, concept, settings, api_key, client)
    finally:
        if own_client:
            await client.aclose()
    _write_atomic(raw_path, text)  # the paid artifact; re-parse from here without paying again

    out = await _result_from_text(text, concept, clip, "meta-api")
    resp = res["response"] or {}
    billed, cost, basis = _cost(resp.get("usage"), clip.get("frame_count"))
    out.update({
        "outcome": res["outcome"], "response_id": resp.get("id"), "request_id": res["request_id"],
        "usage": resp.get("usage"), "frames_billed": billed, "cost_usd": cost, "cost_basis": basis,
        "latency_s": round(res["latency_s"], 3),
        "first_frame_s": round(res["first_frame_s"], 3) if res["first_frame_s"] is not None else None,
        "settings": settings, "cache": "miss", "stream_events": res.get("events"),
        "response_output_items": len(resp.get("output") or []),
    })
    if not out["frames"]:
        out["diagnostics"].append("zero matches: SAM returned no frame lines for this concept (not an error)")
    _write_atomic(json_path, json.dumps(out, separators=(",", ":")))
    log.info("sam %s %r: %d frames with objects, billed %s, %.2fs", src.name, concept, len(out["frames"]),
             billed, res["latency_s"])
    return out
