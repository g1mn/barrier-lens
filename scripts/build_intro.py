"""Write static/demo/intro-corridor.json: the fields of the recorded SANPO run that the opening intro draws
(the measured frame's outlines, the gap, the verdict). No model calls; it copies from sanpo-corridor-live.json.
The intro's clip and still are made once by hand, see static/demo/ATTRIBUTION.txt."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = json.loads((ROOT / "static/demo/sanpo-corridor-live.json").read_text())
md = src["measurement_detail"]
frame = md["frame"]
outlines = [
    [[round(x, 1), round(y, 1)] for x, y in o["polygon"]]
    for m in src["masks"] if m["role"] == "barrier"
    for f in m["frames"] if f["i"] == frame
    for o in f["objects"] if o["id"] in md["barrier_ids"]
]
pose = md["pose"]
out = {
    "from": "static/demo/sanpo-corridor-live.json (recorded live run, " + src["recorded"]["at"] + ")",
    "frame": frame,
    "frame_size": src["frame_size"],
    "outlines": outlines,
    "gap_px": md["gap_px"],
    "gap_cm": src["gap_cm"],
    "band_cm": src["band_cm"],
    "chair_cm": md["wheelchair_cm"],
    "needed_cm": md["wheelchair_cm"] + 2 * md["clearance_cm"],
    "verdict": src["verdict"],
    "explanation": src["explanation"],
    "path": src["triage"]["path"]["state"],
    "barriers": [b["concept"] for b in src["triage"]["barriers"]],
    "blur": {k: src["blur"][k] for k in ("frames", "frames_with_faces", "faces")},
    "camera": {"height_m": pose["height_m"], "pitch_deg": pose["pitch_deg"]},
    "models": src["models"],
}
assert outlines and out["gap_cm"] >= out["needed_cm"], "intro expects the SANPO run's outlines and a passing gap"
(ROOT / "static/demo/intro-corridor.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")
print(f"{len(outlines)} outlines, gap {out['gap_cm']} cm, needs {out['needed_cm']} cm")
