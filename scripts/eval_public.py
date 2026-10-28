"""Camera-pose measurement on the public ground-truth stills (data/eval), through app.geometry.measure_gap.

No model calls, no network. For each image in data/eval/annotations.json (HAND-ANNOTATED pixel points, read by eye)
the annotated ends become small barrier masks (or a wall-base mask + a kerb walkway_edge), and measure_gap runs with
kind "camera_pose" under these profiles:

    seated      calibration.camera_pose({}, None, {}, size): the app's default (armrest height 0.65-0.90 m, default
                phone field of view); tilt from the annotated ground lines, else the wide prior
    standing    as seated, but height 1.4-1.7 m: these public photos were taken standing, so this is the fair
                no-calibration baseline for them
    calibrated  EXIF field of view +-0.5 deg, height = our camera_height_guess_m mid-point +-5 cm (a GUESS from the
                viewpoint, see manifest.json); tilt as above
    cal+sensor  calibrated plus a simulated sensor tilt: the pitch the annotated ground lines give at the EXIF field
                of view, +-1.5 deg (calibration.CALIBRATED_PITCH_SD_DEG), roll 0 +-3 deg. Only where lines give a
                pitch; the sensor value is therefore not independent of the lines (labelled in the output)

Annotation reading error (px_sd per image) is passed as contact_px, so it widens the band.
Writes data/eval/results.json and prints a table. Three images: a small sample, not a benchmark.

    .venv/bin/python scripts/eval_public.py [--draws N]
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import calibration, geometry  # noqa: E402

EVAL = ROOT / "data" / "eval"
BOX_PX = 8          # side of the small mask drawn next to an annotated end point
WALL_BAND_PX = 10   # height of the mask drawn above an annotated wall-base line
STANDING_M = (1.4, 1.7)
CAL_HFOV_SD = 0.5
CAL_HEIGHT_PM = 0.05


def _box(mask, x, y, side):
    """Filled box whose bottom edge ends at row y, on the given side of column x (contact point lands on (x, y))."""
    x, y = int(round(x)), int(round(y))
    x0, x1 = (x - BOX_PX, x) if side == "left" else (x, x + BOX_PX)
    mask[max(0, y - BOX_PX):y, max(0, x0):x1] = True


def barriers(a, size):
    W, H = size
    if a["measure"] == "point_to_point":
        (p, q) = sorted(a["points"])
        out = []
        for pt, side in ((p, "left"), (q, "right")):
            m = np.zeros((H, W), bool)
            _box(m, pt[0], pt[1], side)
            out.append(m)
        return out, None
    (p, q) = a["barrier_line"]
    poly = np.array([p, q, [q[0], q[1] - WALL_BAND_PX], [p[0], p[1] - WALL_BAND_PX]], np.int32)
    m = np.zeros((H, W), np.uint8)
    cv2.fillPoly(m, [poly], 1)
    return [m.astype(bool)], a["edge"]


def profiles(item, a, size):
    W, H = size
    lines = [np.asarray(s, float).tolist() for s in a.get("ground_lines") or []]
    seated = {**calibration.camera_pose({}, None, {}, size), "ground_lines": lines}
    standing = {**seated, "height_m": sum(STANDING_M) / 2, "height_range_m": list(STANDING_M),
                "sources": {**seated["sources"], "height": "standing photographer (eval profile)"}}
    hfov = item["exif"]["hfov_deg"]
    h = sum(item["camera_height_guess_m"]) / 2
    cal = {**seated, "source": "calibrated", "height_m": h, "height_range_m": [h - CAL_HEIGHT_PM, h + CAL_HEIGHT_PM],
           "hfov_deg": hfov, "hfov_range_deg": [hfov - CAL_HFOV_SD, hfov + CAL_HFOV_SD],
           "sources": {**seated["sources"], "height": "camera_height_guess_m mid-point (our guess)",
                       "hfov": item["exif"]["hfov_source"]}}
    out = {"seated": seated, "standing": standing, "calibrated": cal}
    srcs, _ = geometry.ground_line_sources(lines, size)
    est, why = geometry.line_pitch(srcs, size, geometry.focal_px(hfov, W), 0.0, 200, np.random.default_rng(1))
    if est:
        out["cal+sensor"] = {**cal, "pitch_deg": round(est["pitch_deg"], 2),
                             "pitch_sd_deg": calibration.CALIBRATED_PITCH_SD_DEG, "roll_deg": 0.0,
                             "roll_sd_deg": calibration.SENSOR_ROLL_SD_DEG,
                             "sources": {**cal["sources"], "pitch": "simulated sensor centred on the line pitch"}}
    else:
        out["cal+sensor"] = None, why
    return out


def main():
    draws = int(sys.argv[sys.argv.index("--draws") + 1]) if "--draws" in sys.argv else 1000
    manifest = {m["id"]: m for m in json.loads((EVAL / "manifest.json").read_text())["items"]}
    ann = json.loads((EVAL / "annotations.json").read_text())
    rows = []
    for a in ann["items"]:
        item = manifest[a["id"]]
        size = tuple(item["file_size"])
        truth_cm = float(item["truth"]["value_m"]) * 100
        bars, edge = barriers(a, size)
        res = {}
        for name, pose in profiles(item, a, size).items():
            if isinstance(pose, tuple):
                res[name] = {"skipped": pose[1] or "no pitch from lines"}
                continue
            r = geometry.measure_gap(pose, bars, size, walkway_edge=edge, contact_px=a["px_sd"], n_draws=draws,
                                     wheelchair_cm=70.0)
            band = r.get("band_cm")
            d = r.get("detail") or {}
            res[name] = {"gap_cm": r.get("gap_cm"), "band_cm": band, "verdict": r["verdict"],
                         "inside": None if not band else bool(band[0] <= truth_cm <= band[1]),
                         "pitch_source": (d.get("pose") or {}).get("pitch_source"),
                         "pitch_deg": (d.get("pose") or {}).get("pitch_deg"),
                         "pitch_sd_deg": (d.get("pose") or {}).get("pitch_sd_deg"),
                         "dominant_error": d.get("dominant_error"), "why": r["why"]}
        rows.append({"id": a["id"], "truth_cm": truth_cm, "relation": item["truth"]["relation"],
                     "annotation": a["annotation"], "profiles": res})
    out = {"about": "scripts/eval_public.py output: camera_pose bands vs public ground truth. Hand-annotated points; "
                    "camera heights for 'calibrated' are guesses; cal+sensor tilt is centred on the line pitch "
                    "(not an independent sensor). 3 images: small sample.",
           "draws": draws, "results": rows}
    (EVAL / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")

    names = ["seated", "standing", "calibrated", "cal+sensor"]
    print(f"{'image':28s} {'truth':>6s}  " + "  ".join(f"{n:>20s}" for n in names))
    for r in rows:
        cells = []
        for n in names:
            p = r["profiles"][n]
            if "skipped" in p:
                cells.append(f"{'n/a':>20s}")
            elif not p["band_cm"]:
                cells.append(f"{'unknown':>20s}")
            else:
                lo, hi = p["band_cm"]
                cells.append(f"{lo:6.0f}-{hi:<6.0f} {'IN ' if p['inside'] else 'OUT'} {p['pitch_source'][:5]:>5s}")
        print(f"{r['id']:28s} {r['truth_cm']:6.0f}  " + "  ".join(cells))
    print(f"wrote {EVAL / 'results.json'}")


if __name__ == "__main__":
    main()
