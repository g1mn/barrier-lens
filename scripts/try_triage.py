"""Run Muse Spark triage on development clips and print the JSON, latency and token usage.

usage: python scripts/try_triage.py [clip.mp4 ...] [--n 6] [--explain] [--any-height]
  With no clips, uses every .mp4 in data/raw/pexels/. Frames (short side 720 px) are extracted once with
  ffmpeg into data/raw/pexels/frames/<clip>/ with an index.json of frame numbers and times.
  --any-height  do not require seated height (stock footage is filmed standing).
  --explain     also run explain() on a few hand-written measurement dicts.
Backend follows app.spark (META_API_KEY > BASETEN_API_KEY > replay; SPARK_BACKEND forces one). Never prints keys.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import spark  # noqa: E402

PEXELS = ROOT / "data" / "raw" / "pexels"


def probe(video: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(video)],
                         check=True, capture_output=True, text=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"width": s["width"], "height": s["height"], "fps": float(num) / float(den),
            "duration_s": float(j["format"]["duration"])}


def extract(video: Path, n: int) -> tuple[list[Path], dict]:
    """n frames evenly spaced over the clip, short side scaled to 720 px."""
    out_dir = PEXELS / "frames" / video.stem
    index = out_dir / "index.json"
    if index.exists():
        meta = json.loads(index.read_text())
        if len(meta["files"]) == n:
            return [out_dir / f for f in meta["files"]], meta
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(video)
    times = [round(info["duration_s"] * (k + 0.5) / n, 2) for k in range(n)]
    files, idx = [], []
    for t in times:
        fi = round(t * info["fps"])
        name = f"f{fi:05d}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                        "-vf", "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)'", "-q:v", "3",
                        str(out_dir / name)], check=True)
        files.append(name)
        idx.append(fi)
    meta = {**info, "clip": video.name, "times_s": times, "frame_indices": idx, "files": files}
    index.write_text(json.dumps(meta, indent=1))
    return [out_dir / f for f in files], meta


EXPLAIN_CASES = [
    {"gap_cm": 92.4, "band_cm": [86.0, 99.0], "verdict": "passes", "chair_width_cm": 70,
     "why": "gap between e-scooter mask and kerb edge, scale from A4 sheet (21.0 cm) in 5 frames"},
    {"gap_cm": 74.0, "band_cm": [68.5, 79.5], "verdict": "tight", "chair_width_cm": 70,
     "why": "error band overlaps chair width plus 10 cm margin"},
    {"gap_cm": None, "band_cm": None, "verdict": "unknown",
     "why": "no reference sheet found in any frame; cannot convert pixels to cm"},
]


async def run(args) -> None:
    clips = [Path(c) for c in args.clips] or sorted(PEXELS.glob("*.mp4"))
    print("backend:", spark._backend().name)
    for clip in clips:
        paths, meta = extract(clip, args.n)
        frames = [p.read_bytes() for p in paths]
        if args.any_height:
            meta = {**meta, "require_seated_height": False}
        print(f"\n=== {clip.name}  {meta['width']}x{meta['height']} {meta['duration_s']:.1f}s  "
              f"frames {meta['frame_indices']}  ({sum(map(len, frames)) // 1024} KiB of JPEG)")
        t0 = time.monotonic()
        try:
            res = await spark.triage(frames, meta)
        except spark.SparkError as e:
            print("SparkError:", e)
            continue
        wall = time.monotonic() - t0
        call = res.get("call") or {}
        print(json.dumps({k: v for k, v in res.items() if k != "call"}, ensure_ascii=False, indent=1))
        print(f"wall {wall:.2f}s | model latency {call.get('latency_s')}s | attempts {call.get('attempts')} | "
              f"usage {json.dumps(call.get('usage'))}")
    if args.explain:
        for m in EXPLAIN_CASES:
            t0 = time.monotonic()
            res = await spark.explain(m)
            call = res.get("call") or {}
            print(f"\n--- explain {m['verdict']} gap={m['gap_cm']}\n{res['text']}\n"
                  f"model {res['model']} | wall {time.monotonic() - t0:.2f}s | attempts {call.get('attempts')} | "
                  f"usage {json.dumps(call.get('usage'))} | warnings {res.get('warnings')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="*")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--any-height", action="store_true")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
