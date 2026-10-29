"""Static checks for the opening "Where is this?" intro (no JS engine, no server, no network)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()
CSS = (ROOT / "static" / "app.css").read_text()


def test_pinned_vendor_scripts_before_app():
    d3 = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"
    topo = "https://cdnjs.cloudflare.com/ajax/libs/topojson/3.0.2/topojson.min.js"
    assert d3 in HTML and topo in HTML
    assert HTML.index(d3) < HTML.index("/static/app.js")
    assert HTML.index(topo) < HTML.index("/static/app.js")


def test_overlay_markup_and_a11y():
    for frag in ('id="intro"', 'id="intro-globe"', 'id="intro-skip"', 'id="intro-cap"',
                 'id="intro-cap-text"', 'Skip intro', 'aria-hidden="true"',
                 "Locating: Gwanghwamun Gate, Seoul, South Korea", 'role="status"',
                 'id="tour-intro"', "Replay intro"):
        assert frag in HTML, f"{frag} missing"


def test_intro_behaviour_hooks():
    for s in ("bl-intro-seen", "sessionStorage", "#nointro", "#tour", "Escape",
              "prefers-reduced-motion", "geoOrthographic", "geoInterpolate",
              "geoGraticule", "South Korea", "126.9780", "37.5665", "flyTo",
              "Globe: Natural Earth", "devicePixelRatio", "requestAnimationFrame",
              "Gwanghwamun Gate"):
        assert s in JS, f"{s} missing from intro code"


def test_intro_styles_both_themes():
    for s in ("#intro", "#intro-cap", ".intro-skip", "--globe-ocean", "--globe-land",
              '[data-theme="light"]', "prefers-reduced-motion"):
        assert s in CSS, f"{s} missing from CSS"
