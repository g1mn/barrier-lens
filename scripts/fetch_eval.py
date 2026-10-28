"""Fetch the public ground-truth evaluation images listed in data/eval/manifest.json.

Every image is from Wikimedia Commons under a licence that allows redistribution
(CC0, CC BY, CC BY-SA). For each entry the script re-checks the licence and
description through the Commons API, downloads a Commons-rendered thumbnail no
wider than 1600 px, and fills in the machine-checkable fields (URL, licence,
author, EXIF make/model/focal length, derived horizontal field of view).

Human-judged fields (stated width, where it is stated, which two edges it spans,
camera-height guess) live in the manifest and are not overwritten.

Usage:
    .venv/bin/python scripts/fetch_eval.py            # refresh metadata + download missing images
    .venv/bin/python scripts/fetch_eval.py --check    # metadata only, report licence drift
"""
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data" / "eval"
MANIFEST = EVAL / "manifest.json"
API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "BarrierLensEval/0.1 (Meta hackathon research; github.com/g1mn)"}
ALLOWED = ("CC0", "Public domain", "CC BY 2.0", "CC BY 3.0", "CC BY 4.0",
           "CC BY-SA 2.0", "CC BY-SA 3.0", "CC BY-SA 4.0")
MAX_W = 1600

# Sensor widths (mm) from maker spec sheets, used only when EXIF has no
# 35 mm-equivalent focal length. Nominal 1/2.3" type = 6.17 x 4.55 mm.
SENSOR_W_MM = {"Canon IXUS 115 HS": 6.17}


def _get(params, binary=False, url=None):
    url = url or API + "?" + urllib.parse.urlencode(dict(params, format="json", maxlag=5))
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                data = r.read()
            time.sleep(1.5)
            return data if binary else json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(10 * (i + 1))
    raise SystemExit("Commons rate limit: try again later")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def _ratio(v):
    if isinstance(v, str) and "/" in v:
        a, b = v.split("/")
        return float(a) / float(b)
    return float(v) if v not in (None, "") else None


def hfov_deg(model, focal_mm, f35, width, height):
    """Horizontal FOV of the full image. Prefer the 35 mm-equivalent focal
    (diagonal convention, 43.27 mm), else the sensor width from SENSOR_W_MM."""
    if f35:
        diag = math.hypot(width, height)
        w_equiv = 43.27 * width / diag
        return round(math.degrees(2 * math.atan(w_equiv / (2 * f35))), 1), "35mm-equivalent focal (EXIF), diagonal convention"
    if focal_mm and model in SENSOR_W_MM:
        sw = SENSOR_W_MM[model]
        if height > width:
            sw = sw * height / width  # unlikely; keep long side on sensor width
        return round(math.degrees(2 * math.atan(sw / (2 * focal_mm))), 1), f"EXIF focal {focal_mm} mm + spec sensor width {sw} mm"
    return None, "unknown"


def refresh(entry, download=True):
    d = _get({"action": "query", "prop": "imageinfo", "titles": entry["commons_title"],
              "iiprop": "url|extmetadata|size|metadata", "iiurlwidth": MAX_W})
    page = next(iter(d["query"]["pages"].values()))
    ii = page["imageinfo"][0]
    em = ii["extmetadata"]
    md = {m["name"]: m["value"] for m in ii.get("metadata") or []}
    lic = em.get("LicenseShortName", {}).get("value", "")
    entry["page_url"] = ii["descriptionurl"]
    entry["original_url"] = ii["url"].split("?")[0]
    entry["licence"] = lic
    entry["licence_url"] = em.get("LicenseUrl", {}).get("value", "")
    entry["author"] = _text(em.get("Artist", {}).get("value"))
    entry["description_excerpt"] = _text(em.get("ImageDescription", {}).get("value"))[:400]
    entry["original_size"] = [ii["width"], ii["height"]]
    model = str(md.get("Model", "")).strip()
    focal = _ratio(md.get("FocalLength"))
    f35 = _ratio(md.get("FocalLengthIn35mmFilm")) or None
    fov, how = hfov_deg(model, focal, f35, ii["width"], ii["height"])
    entry["exif"] = {"make": str(md.get("Make", "")).strip(), "model": model,
                     "focal_mm": focal, "focal_35mm_equiv": f35,
                     "hfov_deg": fov, "hfov_source": how}
    ok = lic in ALLOWED
    entry["licence_ok"] = ok
    if download and ok:
        out = EVAL / entry["file"]
        if not out.exists():
            src = ii["url"] if ii["width"] <= MAX_W else (ii.get("thumburl") or ii["url"])
            _save_resized(_get(None, binary=True, url=src), out)
        h, w = _cv2().imread(str(out)).shape[:2]
        entry["file_size"] = [w, h]
    return ok


def _cv2():
    import cv2
    return cv2


def _save_resized(data, out):
    """Decode, shrink to <= MAX_W on the long side, re-encode (drops EXIF; the
    EXIF fields we need are kept in the manifest)."""
    import numpy as np
    cv2 = _cv2()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    s = MAX_W / max(h, w)
    if s < 1:
        img = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 88])


def main():
    check = "--check" in sys.argv
    m = json.loads(MANIFEST.read_text())
    bad = []
    for e in m["items"]:
        if not refresh(e, download=not check):
            bad.append((e["id"], e["licence"]))
        print(f'{e["id"]}: {e["licence"]} hfov={e["exif"]["hfov_deg"]} -> {e["file"]}')
    if not check:
        MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")
    if bad:
        print("licence not allowed:", bad)
        sys.exit(1)


if __name__ == "__main__":
    main()
