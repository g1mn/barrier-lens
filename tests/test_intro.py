"""Static checks for the opening intro (no JS engine, no server, no network)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()
CSS = (ROOT / "static" / "app.css").read_text()


def test_pinned_vendor_scripts_before_app():
    for url in ("https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/5.24.0/maplibre-gl.min.js",
                "https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js"):
        assert url in HTML and HTML.index(url) < HTML.index("/static/app.js")
    assert "maplibre-gl/5.24.0/maplibre-gl.min.css" in HTML
    assert HTML.count('integrity="sha512-') >= 4  # Leaflet js/css and MapLibre js/css


def test_overlay_markup_and_a11y():
    for frag in ('id="intro"', 'id="intro-globe"', 'id="intro-fx"', 'id="iv-cam"', 'id="iv-video"', 'id="iv-still"',
                 'id="iv-rail"', 'id="intro-skip"', 'Skip intro', 'role="status"', "Gwanghwamun Gate",
                 "SANPO (Google Research, CC BY 4.0)", "OpenStreetMap contributors", 'id="tour-intro"', "Replay intro"):
        assert frag in HTML, f"{frag} missing"


def test_intro_behaviour_hooks():
    for s in ("bl-intro-seen", "sessionStorage", "#nointro", "#tour", "Escape", "prefers-reduced-motion",
              "intro_at", "projection: { type: 'globe' }", "tile.openstreetmap.org", "map.getZoom() - 1",
              "zoomPath", "monotone", "redraw()", "intro-on", "/static/demo/intro-corridor.json",
              "requestAnimationFrame", "devicePixelRatio"):
        assert s in JS, f"{s} missing from intro code"


def test_intro_assets_match_the_recorded_run():
    demo = ROOT / "static" / "demo"
    for name in ("intro-corridor.mp4", "intro-corridor-77.jpg", "intro-corridor.json"):
        assert (demo / name).stat().st_size > 1000, name
    d = json.loads((demo / "intro-corridor.json").read_text())
    run = json.loads((demo / "sanpo-corridor-live.json").read_text())
    assert d["gap_cm"] == run["gap_cm"] and d["band_cm"] == run["band_cm"] and d["verdict"] == run["verdict"] == "passes"
    assert d["frame"] == run["measurement_detail"]["frame"] and d["gap_px"] == run["measurement_detail"]["gap_px"]
    assert d["explanation"] == run["explanation"] and len(d["outlines"]) == len(run["measurement_detail"]["barrier_ids"])
    assert "intro-corridor" in (demo / "ATTRIBUTION.txt").read_text()


def test_intro_styles():
    for s in ("#intro", "#intro-globe", ".iv-cam", ".iv-rail", ".intro-skip", "--globe-ocean", "--globe-land",
              '[data-theme="light"]', "prefers-reduced-motion", ".intro-on .sheet", "translate"):
        assert s in CSS, f"{s} missing from CSS"
