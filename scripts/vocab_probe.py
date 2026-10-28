"""Find SAM 3.1 concept phrases that work on our footage, within a hard budget of paid calls.

usage:
  python scripts/vocab_probe.py CLIP_DIR [--budget 10] [--out results.json] [--fixtures]
  python scripts/vocab_probe.py CLIP_DIR --sheets SHEET_DIR      (contact sheets from cached results, no spend)

CLIP_DIR holds the 3 s, 10 fps, 720p cuts named in PLAN. Each PLAN group is a list of alternative phrases for one
target; the probe stops a group at the first phrase that returns objects ("stop early when a phrase works").
Every request is one concept (SAM 3.1 answers lists with zero matches). Cached clip + concept pairs cost nothing,
so a re-run only pays for pairs never asked before; the run stops before a call that would exceed --budget.
Never prints the key.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import sam  # noqa: E402
from app.config import key  # noqa: E402

# (clip file in CLIP_DIR, [alternative phrases, tried in order until one returns objects])
PLAN = [
    ("scooters-row-0-3s.mp4", ["electric scooter"]),
    ("sidewalk-closed-0-3s.mp4", ["barricade", "road barrier", "sawhorse"]),
    ("sidewalk-closed-0-3s.mp4", ["sign", "sidewalk closed sign"]),
    ("scooters-city-6-9s.mp4", ["tactile paving", "yellow tactile paving", "tactile paving strip", "yellow tile"]),
    ("scooters-city-6-9s.mp4", ["scooter"]),
    # ground surfaces whose mask edges could give two converging lines for horizon / pitch (one group each,
    # so every phrase is asked even when an earlier one works)
    ("scooters-row-0-3s.mp4", ["sidewalk"]),
    ("scooters-row-0-3s.mp4", ["curb"]),
    ("scooters-row-0-3s.mp4", ["road"]),
    ("scooters-city-6-9s.mp4", ["sidewalk"]),
    ("scooters-city-6-9s.mp4", ["pavement"]),
]
FIXTURE_MAX_BYTES = 250_000
FIXTURE_MAX_FRAMES = 30


def summarize(out: dict) -> dict:
    """Numbers for the phrase table: frames with objects, objects per frame, ids, scores, box sizes."""
    frames = out.get("frames") or []
    per_frame = [len(f["objects"]) for f in frames]
    scores = [o["score"] for f in frames for o in f["objects"] if o.get("score") is not None]
    ids = sorted({o["id"] for f in frames for o in f["objects"]})
    w, h = out.get("width") or 1, out.get("height") or 1
    by_id: dict[int, dict] = {}
    for f in frames:
        for o in f["objects"]:
            x0, y0, x1, y1 = o["box"]
            area = int(sam.decode_mask(o, (w, h)).sum())
            d = by_id.setdefault(o["id"], {"frames": 0, "box_w_frac": [], "box_h_frac": [], "mask_area_frac": []})
            d["frames"] += 1
            d["box_w_frac"].append((x1 - x0 + 1) / w)
            d["box_h_frac"].append((y1 - y0 + 1) / h)
            d["mask_area_frac"].append(area / (w * h))
    objects = {i: {"frames": d["frames"],
                   "box_w_frac_median": round(float(np.median(d["box_w_frac"])), 3),
                   "box_h_frac_median": round(float(np.median(d["box_h_frac"])), 3),
                   "mask_area_frac_median": round(float(np.median(d["mask_area_frac"])), 4)}
               for i, d in by_id.items()}
    return {
        "concept": out.get("concept"), "clip": (out.get("clip") or {}).get("path"),
        "clip_sha256": (out.get("clip") or {}).get("sha256"), "cache": out.get("cache"),
        "outcome": out.get("outcome"), "clip_frames": out.get("frame_count"),
        "frames_with_objects": len(frames), "objects_per_frame_min": min(per_frame, default=0),
        "objects_per_frame_max": max(per_frame, default=0), "object_ids": ids,
        "score_min": round(min(scores), 3) if scores else None,
        "score_median": round(float(np.median(scores)), 3) if scores else None,
        "score_max": round(max(scores), 3) if scores else None,
        "objects": objects, "frames_billed": out.get("frames_billed"), "cost_usd": out.get("cost_usd"),
        "latency_s": out.get("latency_s"), "response_id": out.get("response_id"),
        "diagnostics": out.get("diagnostics"),
    }


def save_fixture(out: dict, clip: Path, name: str) -> Path:
    """Replay fixture keyed by the cut's sha256; drops trailing frames until the file is under 250 KB."""
    keep = ("concept", "fps", "width", "height", "frame_count", "model", "outcome", "response_id", "usage",
            "frames_billed", "cost_usd", "cost_basis", "latency_s", "first_frame_s", "settings", "diagnostics")
    frames = (out.get("frames") or [])[:FIXTURE_MAX_FRAMES]
    while True:
        fx = {"note": f"Real SAM 3.1 output (Meta Model API, {out.get('response_id')}) for {clip.name}; "
                      f"{len(frames)} of {len(out.get('frames') or [])} frames with objects kept.",
              "clip_sha256": out["clip"]["sha256"], "clip_name": clip.name,
              **{k: out.get(k) for k in keep}, "frames": frames}
        text = json.dumps(fx, separators=(",", ":"))
        if len(text) <= FIXTURE_MAX_BYTES or len(frames) <= 1:
            break
        frames = frames[: max(1, int(len(frames) * FIXTURE_MAX_BYTES / len(text)) - 1)]
    path = sam.FIXTURE_DIR / f"{sam._slug(Path(clip).stem + '-' + out['concept'])}.json"
    sam._write_atomic(path, text)
    return path


def cached(clip: Path, concept: str) -> bool:
    json_path, _ = sam._cache_paths(sam._sha256(clip), sam.normalize_concept(concept), sam._settings())
    return json_path.exists()


async def probe(clip_dir: Path, budget: int, fixtures: bool) -> list[dict]:
    rows, paid = [], 0
    async with httpx.AsyncClient(timeout=sam.TIMEOUT) as client:
        for clip_name, phrases in PLAN:
            clip = clip_dir / clip_name
            if not clip.exists():
                print(f"{clip_name:28} missing in {clip_dir}, skipped")
                continue
            for phrase in phrases:
                if not cached(clip, phrase):
                    if paid >= budget:
                        rows.append({"concept": phrase, "clip": clip_name, "skipped": f"budget {budget} reached"})
                        continue
                    paid += 1
                try:
                    out = await sam.segment_video(str(clip), phrase, mode="live", client=client)
                except sam.SamError as e:
                    rows.append({"concept": phrase, "clip": clip_name, "error": str(e), "status": e.status,
                                 "code": e.code})
                    continue
                row = {"clip_cut": clip_name, "http_status": 200, **summarize(out)}
                if fixtures:
                    row["fixture"] = str(save_fixture(out, clip, phrase).relative_to(ROOT))
                rows.append(row)
                print(f"{clip_name:28} {phrase!r:26} cache={row['cache']:4} frames_with_objects="
                      f"{row['frames_with_objects']}/{row['clip_frames']} objects/frame "
                      f"{row['objects_per_frame_min']}-{row['objects_per_frame_max']} ids={row['object_ids']} "
                      f"score {row['score_min']}..{row['score_max']} ${row['cost_usd']}")
                if row["frames_with_objects"]:
                    break
    print(f"paid calls this run: {paid}")
    return rows


COLORS = [(0, 200, 255), (255, 80, 80), (80, 255, 80), (255, 0, 255), (255, 255, 0), (0, 128, 255), (180, 120, 255)]


def sheet(clip: Path, concept: str, dest: Path, n: int = 4) -> Path | None:
    """Masks of one cached result drawn on n evenly spaced frames of the clip SAM saw, tiled side by side."""
    import cv2

    json_path, _ = sam._cache_paths(sam._sha256(clip), sam.normalize_concept(concept), sam._settings())
    if not json_path.exists():
        return None
    out = json.loads(json_path.read_text())
    if not out.get("frames"):
        return None
    seen = ROOT / out["clip"]["path"] if not Path(out["clip"]["path"]).is_absolute() else Path(out["clip"]["path"])
    prepped = sorted((sam.CACHE_DIR / "clips").glob(f"{out['clip']['sha256'][:16]}-*.mp4"))
    seen = prepped[0] if prepped else seen
    cap = cv2.VideoCapture(str(seen))
    imgs = []
    total = out.get("frame_count") or 30
    for i in np.linspace(0, total - 1, n).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, img = cap.read()
        if not ok:
            continue
        masks = sam.masks_for_frame(out, int(i))
        objs = {o["id"]: o for f in out["frames"] if f["i"] == i for o in f["objects"]}
        over = img.copy()
        for oid, m in masks.items():
            over[m] = COLORS[oid % len(COLORS)]
        img = cv2.addWeighted(over, 0.5, img, 0.5, 0)
        for oid, o in objs.items():
            x0, y0, x1, y1 = o["box"]
            cv2.rectangle(img, (x0, y0), (x1, y1), COLORS[oid % len(COLORS)], 2)
            cv2.putText(img, f"id{oid} {o.get('score') or 0:.2f}", (x0 + 4, max(20, y0 + 22)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, COLORS[oid % len(COLORS)], 2)
        cv2.putText(img, f"f{i} {concept}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        imgs.append(img)
    cap.release()
    if not imgs:
        return None
    h = min(im.shape[0] for im in imgs)
    tile = np.hstack([cv2.resize(im, (int(im.shape[1] * h / im.shape[0]), h)) for im in imgs])
    k = min(1.0, 1800 / tile.shape[1])
    tile = cv2.resize(tile, (int(tile.shape[1] * k), int(tile.shape[0] * k)))
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"sheet-{clip.stem}-{sam._slug(concept)}.jpg"
    cv2.imwrite(str(path), tile, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clip_dir")
    ap.add_argument("--budget", type=int, default=10, help="max paid SAM calls in this run")
    ap.add_argument("--out", help="write the result rows as JSON here")
    ap.add_argument("--fixtures", action="store_true", help="write data/fixtures/sam/<cut>-<concept>.json")
    ap.add_argument("--sheets", metavar="DIR", help="render contact sheets from cached results instead (no spend)")
    a = ap.parse_args()
    clip_dir = Path(a.clip_dir)
    if a.sheets:
        for clip_name, phrases in PLAN:
            if not (clip_dir / clip_name).exists():
                continue
            for p in phrases:
                path = sheet(clip_dir / clip_name, p, Path(a.sheets))
                if path:
                    print(path)
        return 0
    if not key("META_API_KEY"):
        print("META_API_KEY is not set (~/.config/barrier-lens/.env); nothing to probe")
        return 1
    rows = asyncio.run(probe(clip_dir, a.budget, a.fixtures))
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
