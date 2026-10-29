"""Static checks for the 1-minute tour (no JS engine, no server, no network)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text()
JS = (ROOT / "static" / "app.js").read_text()
CSS = (ROOT / "static" / "app.css").read_text()
ANALYZE = (ROOT / "app" / "analyze.py").read_text()


def test_header_button_and_card_present():
    assert 'id="tour-btn"' in HTML and '1-minute tour' in HTML
    assert 'id="tour-card"' in HTML
    assert 'aria-live="polite"' in HTML
    for bid in ("tour-pause", "tour-next", "tour-close", "tour-replay", "tour-try"):
        assert f'id="{bid}"' in HTML, f"{bid} missing"


def test_step_data_array_valid():
    m = re.search(r"const TOUR_STEPS = \[(.*?)\];", JS, re.S)
    assert m, "TOUR_STEPS array missing"
    body = m.group(1)
    durs = [int(x) for x in re.findall(r"dur:\s*(\d+)", body)]
    assert len(durs) == 7, f"expected 7 steps, found {len(durs)}"
    assert 55 <= sum(durs) <= 75, f"total {sum(durs)}s is not about 60-70s"
    keys = re.findall(r"sample:\s*'([a-z]+)'", body)
    assert keys, "no sample keys in TOUR_STEPS"
    recorded = set(re.findall(r'"([a-z]+)": ROOT / "static" / "demo"', ANALYZE))
    assert set(keys) <= recorded, f"{set(keys) - recorded} are not recorded runs"


def test_tour_behaviour_hooks():
    for s in ("#tour", "Escape", "ArrowRight", "prefers-reduced-motion", "isTrusted",
              "replay of the recorded run", "tour-hit", "aria-pressed"):
        assert s in JS, f"{s} missing from tour code"
    assert ".tour-card" in CSS and ".tour-hit" in CSS
    assert "keep-all" in CSS
