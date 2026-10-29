"""List every English string the page shows, for translation: static/i18n/source.json.

Three kinds:
  page   - index.html (data-i18n, data-i18n-html, data-i18n-attr) and app.js _('...') calls: shown through _();
  server - text the server writes (assumptions, fallback explanations, error details, Seoul data notes): shown
           through tr(), which matches whole texts, {name} templates and single sentences;
  run    - what the recorded runs say (Muse Spark's replies, sample notes): shown through tr() as well.
Each language file static/i18n/<lang>.json maps every key here to its translation (tests/test_i18n.py checks).
No model calls. Run: .venv/bin/python scripts/i18n_strings.py
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import analyze, calibration, seoul  # noqa: E402

SENTENCE = re.compile(r'(?<=[.!?])\s+(?=[A-Z“"(])')  # the same split as tr() in static/i18n.js
norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731


def page_strings() -> list[tuple[str, str]]:
    out = []
    doc = (ROOT / "static/index.html").read_text()
    for m in re.finditer(r"<(\w+)[^>]*\sdata-i18n(?=[\s>])[^>]*>(.*?)</\1>", doc, re.S):
        out.append((norm(html.unescape(m.group(2))), "index.html"))
    for m in re.finditer(r"<(\w+)[^>]*\sdata-i18n-html[^>]*>(.*?)</\1>", doc, re.S):
        out.append((norm(m.group(2)), "index.html (markup kept)"))
    for m in re.finditer(r"<\w+([^>]*\sdata-i18n-attr=\"([^\"]+)\"[^>]*)>", doc):
        for a in m.group(2).split():
            v = re.search(rf'\s{a}="([^"]*)"', m.group(1))
            if v:
                out.append((html.unescape(v.group(1)), f"index.html ({a})"))
    js = (ROOT / "static/app.js").read_text()
    for m in re.finditer(r"\b_\('((?:[^'\\]|\\.)*)'", js):
        out.append((json.loads('"' + m.group(1).replace("\\'", "'").replace('"', '\\"') + '"'), "app.js"))
    return out


# Text the server builds from parts. {name} slots match any text; tests/test_i18n.py checks that each template's
# fixed parts still appear in the server code, so a changed sentence shows up here.
SERVER_TEMPLATES = [
    # calibration.assumption_text
    ("Chair width {n} cm (your profile).", "app/calibration.py"),
    ("Chair width {n} cm (default).", "app/calibration.py"),
    ("Scale from the {ref} on the ground.", "app/calibration.py"),
    ("Phone at about {n} cm, solved from your calibration clip.", "app/calibration.py"),
    ("Phone held near your armrest height, {lo}–{hi} cm above the ground.", "app/calibration.py"),
    ("Phone assumed at armrest height, anywhere {lo}–{hi} cm above the ground.", "app/calibration.py"),
    ("Tilt from the phone's motion sensor.", "app/calibration.py"),
    ("Tilt not recorded, so it is estimated from lines on the ground.", "app/calibration.py"),
    ("Tilt not recorded, so a wide range of tilts is assumed; the motion sensor or a calibration narrows it.", "app/analyze.py"),
    ("The {concept} was not found in the calibration clip. Film it flat on the ground, fully in view.", "app/calibration.py"),
    ("The reference was found but its corners could not be solved into a camera height. Film closer, with all four "
     "corners in view.", "app/calibration.py"),
    # spark._template (the fallback when the model's sentence fails the number check)
    ("Your wheelchair should fit through here.", "app/spark.py"),
    ("It is tight here: your wheelchair may just fit, with very little room.", "app/spark.py"),
    ("Your wheelchair will not fit through here.", "app/spark.py"),
    ("We could not measure this gap, so we cannot say whether your wheelchair fits.", "app/spark.py"),
    ("Try filming again with four A4 sheets taped flat on the ground near the gap, or a bright tactile paving strip, "
     "in view.", "app/spark.py"),
    ("The free gap measures about {gap} cm (between {lo} and {hi} cm).", "app/spark.py"),
    ("The free gap measures about {gap} cm.", "app/spark.py"),
    # analyze.cause_text
    ("We cannot say yet whether your wheelchair fits: {reason}.", "app/analyze.py"),
    ("Four A4 sheets taped flat on the ground near the gap also work.", "app/analyze.py"),
    ("This is a recorded run, so your calibration and tilt are not applied to it.", "app/analyze.py"),
    # main.py error details
    ("live checks on this public demo need the judge token (see the submission's testing instructions); the recorded "
     "sample works without it", "app/main.py"),
    ("clip too large (200 MB max); 10 seconds is enough", "app/main.py"),
    ("clip too large (200 MB max); a few seconds is enough", "app/main.py"),
    ("empty upload", "app/main.py"),
    ("faces could not be blurred, so nothing was sent: {error}", "app/main.py"),
    ("model call failed: {error}", "app/main.py"),
    ("internal error: {error}", "app/main.py"),
]
# names the server or Muse Spark puts in a sentence: modes, references and the barrier words triage prefers
WORDS = ["reference", "calibrated", "default", "paper", "tactile paving",
         "scooter", "bicycle", "bollard", "construction barrier", "construction fence", "traffic cone", "planter",
         "parked car", "motorcycle", "sign", "trash bin", "pole", "bench"]


def server_strings() -> list[tuple[str, str]]:
    out = list(SERVER_TEMPLATES) + [(w, "barrier and reference names") for w in WORDS]
    out += [(v, "app/calibration.py LABELS") for v in calibration.LABELS.values()]
    for reason, step, _ in analyze.UNKNOWN_CAUSES.values():
        out += [(reason, "app/analyze.py UNKNOWN_CAUSES"), (step, "app/analyze.py UNKNOWN_CAUSES")]
    out += [(s, "app/analyze.py") for s in SENTENCE.split(analyze.UNKNOWN_EXPLANATION)]
    meta, st = seoul.towing_meta(), seoul.station(live=False)
    out += [(c, "towing data notes") for c in meta.get("caveats", [])]
    out += [(c, "station data notes") for c in st.get("caveats", [])]
    out += [(x, "station names and lines") for s in st["stations"] for x in (s["name"], s["line"])]
    return out


def run_strings() -> list[tuple[str, str]]:
    out = []
    for p in sorted((ROOT / "static/demo").glob("*-live.json")):
        d = json.loads(p.read_text())
        where = f"recorded run {p.stem}"
        ex = d.get("explanation")
        out.append(((ex or {}).get("text") if isinstance(ex, dict) else ex, f"{where}: Muse Spark's explanation"))
        out.append(((d.get("recorded") or {}).get("about"), f"{where}: what the sample shows"))
        out += [(n, f"{where}: note") for n in d.get("notes") or []]
        a = d.get("assumptions") or {}
        out += [(a.get("label"), f"{where}: assumptions"), (a.get("main"), f"{where}: assumptions")]
        tri = d.get("triage") or {}
        out.append((((tri.get("path") or {}).get("evidence")), f"{where}: Muse Spark's path reading"))
        for b in tri.get("barriers") or []:
            out += [(b.get("concept"), f"{where}: barrier name"), (b.get("evidence"), f"{where}: Muse Spark's barrier note")]
    d = json.loads((ROOT / "static/demo/intro-corridor.json").read_text())
    out.append((d["explanation"], "intro: Muse Spark's explanation"))
    out += [(b, "intro: barrier name") for b in d["barriers"]]
    return out


def collect() -> list[dict]:
    seen, rows = set(), []
    for kind, items in (("page", page_strings()), ("server", server_strings()), ("run", run_strings())):
        for text, where in items:
            if not text or text in seen or not re.search(r"[A-Za-z]", text):
                continue
            seen.add(text)
            rows.append({"en": text, "kind": kind, "where": where})
    return rows


if __name__ == "__main__":
    rows = collect()
    out = ROOT / "static/i18n/source.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(rows)} strings -> {out.relative_to(ROOT)}",
          {k: sum(r["kind"] == k for r in rows) for k in ("page", "server", "run")})
