"""Probe SAM 3.1 on the Meta Model API with one short clip and one concept.

usage: python scripts/probe_sam.py [clip.mp4] [--concept bollard] [--seconds 3] [--models-only] [--fresh]

Without META_API_KEY it prints what is missing and exits 1. With the key it checks GET /v1/models/sam-3.1,
trims the clip losslessly to --seconds, runs segment_video(mode="live") and prints status, latency, frame counts
and the cost from usage. A repeat run is answered by the cache (no spend) unless --fresh. Never prints the key.
Cost of one run: max(50, frames) x $0.20 / 1,000 (3 s at 10 fps -> billed 50 frames -> $0.01).
"""
import argparse
import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import sam  # noqa: E402
from app.config import key  # noqa: E402


def default_clip() -> Path | None:
    clips = sorted(p for ext in ("*.mp4", "*.mov", "*.MOV") for p in (ROOT / "data" / "raw").rglob(ext))
    return min(clips, key=lambda p: p.stat().st_size) if clips else None


def trim(src: Path, seconds: float, duration: float | None = None) -> Path:
    if duration is not None and duration <= seconds + 0.05:
        return src  # already short enough; keep its hash so replay fixtures match the clip you passed
    out = sam.CACHE_DIR / "probe" / f"{src.stem}-{seconds:g}s.mp4"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-t", f"{seconds:g}",
                        "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", nargs="?")
    ap.add_argument("--concept", default="bollard")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--models-only", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore the cache and pay for a new call")
    ap.add_argument("--save-fixture", metavar="NAME", help="write data/fixtures/sam/NAME.json (first 30 frames)")
    a = ap.parse_args()

    clip = Path(a.clip) if a.clip else default_clip()
    missing = []
    if not key("META_API_KEY"):
        missing.append("META_API_KEY (add to ~/.config/barrier-lens/.env once the card is back on dev.meta.ai)")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            missing.append(f"{tool} (brew install ffmpeg)")
    if clip is None or not clip.is_file():
        missing.append(f"a clip: {a.clip or 'nothing under data/raw/'}")
    if missing:
        print("cannot probe SAM 3.1 yet. missing:")
        for m in missing:
            print("  -", m)
        print("replay mode still works offline: SAM_MODE=replay (fixtures in data/fixtures/sam/)")
        return 1

    api_key = key("META_API_KEY")
    async with httpx.AsyncClient(timeout=sam.TIMEOUT) as client:
        t0 = time.monotonic()
        r = await client.get(f"{sam.API_BASE}/models/{sam.MODEL}", headers={"Authorization": f"Bearer {api_key}"})
        print(f"GET /v1/models/{sam.MODEL}: {r.status_code} in {time.monotonic() - t0:.2f}s")
        if r.status_code != 200:
            print(r.text[:400])
            return 1
        print("  model:", {k: v for k, v in r.json().items() if k in ("id", "object", "owned_by", "created")})
        if a.models_only:
            return 0

        short = trim(clip, a.seconds, ((await sam.probe_video(clip)) or {}).get("duration_s"))
        info = await sam.probe_video(short)
        print(f"clip: {clip.name} -> {short.name} ({short.stat().st_size} bytes) probe={info}")
        print(f"concept: {a.concept!r}  settings: {sam._settings()}")
        t0 = time.monotonic()
        try:
            out = await sam.segment_video(str(short), a.concept, mode="live", client=client, use_cache=not a.fresh)
        except sam.SamError as e:
            print(f"FAILED after {time.monotonic() - t0:.2f}s: status={e.status} code={e.code} {e}")
            return 1
        wall = time.monotonic() - t0

    print(f"status: {out.get('outcome')}  cache: {out.get('cache')}  wall: {wall:.2f}s")
    print(f"latency: stream {out.get('latency_s')}s, first frame line {out.get('first_frame_s')}s")
    n_obj = sorted({o['id'] for f in out['frames'] for o in f['objects']})
    print(f"frames: clip {out.get('frame_count')} @ {out.get('fps')} fps, {len(out['frames'])} with objects, "
          f"object ids {n_obj}, frame size {out.get('width')}x{out.get('height')}")
    spent = "$0 now (cache hit; numbers below are from the original call)" if out.get("cache") == "hit" else "this call"
    print(f"spend: {spent}")
    print(f"billed frames: {out.get('frames_billed')}  cost: ${out.get('cost_usd')}  ({out.get('cost_basis')})")
    print(f"usage: {out.get('usage')}")
    print(f"stream events: {out.get('stream_events')}  response output items: {out.get('response_output_items')}")
    print(f"response_id: {out.get('response_id')}  request_id: {out.get('request_id')}")
    if out["frames"]:
        o = out["frames"][0]["objects"][0]
        print(f"first object: frame {out['frames'][0]['i']} id {o['id']} score {o['score']} box {o['box']} "
              f"mask area {int(sam.decode_mask(o).sum())} px")
    per_frame = [len(f["objects"]) for f in out["frames"]]
    print(f"objects per frame: {per_frame}")
    if out.get("diagnostics"):
        print("diagnostics:", out["diagnostics"][:5])
    if a.save_fixture:
        path = save_fixture(out, a.save_fixture)
        print(f"fixture: {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


def save_fixture(out: dict, name: str, max_frames: int = 30) -> Path:
    """Replay fixture from a real result: same clip hash and concept, masks for at most max_frames frames."""
    keep = ("concept", "fps", "width", "height", "frame_count", "model", "outcome", "response_id", "usage",
            "frames_billed", "cost_usd", "cost_basis", "latency_s", "first_frame_s", "settings", "diagnostics")
    fx = {"note": f"Real SAM 3.1 output (Meta Model API, {out.get('response_id')}), first {max_frames} frames kept.",
          "clip_sha256": out["clip"]["sha256"], "clip_name": Path(out["clip"]["path"]).name,
          **{k: out.get(k) for k in keep}, "frames": out["frames"][:max_frames]}
    path = sam.FIXTURE_DIR / f"{sam._slug(name)}.json"
    sam._write_atomic(path, sam.json.dumps(fx, separators=(",", ":")))
    return path


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
