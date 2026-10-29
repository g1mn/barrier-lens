"""Languages: every string the page shows has a translation in every language file, with the same {slots} and markup.
No browser and no model calls. A failing language: .venv/bin/python -m pytest tests/test_i18n.py -k ko"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
I18N = ROOT / "static" / "i18n"
LANGS = ["ko", "ja", "zh-CN", "zh-TW", "es", "fr", "de", "vi", "id"]  # static/i18n.js LANGS, English aside
SOURCE = json.loads((I18N / "source.json").read_text())
KEYS = [r["en"] for r in SOURCE]
slots = lambda s: sorted(re.findall(r"\{\w+\}", s))  # noqa: E731
tags = lambda s: sorted(re.findall(r"</?\w+[^>]*>", s))  # noqa: E731


def test_source_list_is_current():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "i18n_strings.py")], capture_output=True, text=True,
                         cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert json.loads((I18N / "source.json").read_text()) == SOURCE, "source.json changed: run scripts/i18n_strings.py"


def test_page_and_server_agree_on_languages():
    js = (ROOT / "static" / "i18n.js").read_text()
    assert re.findall(r"\['([\w-]+)', '", js) == ["en", *LANGS]
    from app import spark
    assert list(spark.LANGS) == ["en", *LANGS]


def test_server_templates_still_match_the_server_code():
    from scripts.i18n_strings import SERVER_TEMPLATES
    code = "".join(p.read_text() for p in (ROOT / "app").glob("*.py"))
    code = re.sub(r'"\s*\n\s*(?:f?")', "", code)  # join implicitly concatenated string literals
    for text, where in SERVER_TEMPLATES:
        for part in re.split(r"\{\w+\}|[()]|(?<=\.) ", text):  # f-strings break sentences at their {expressions}
            part = part.strip(" .,;:–")
            if len(part) > 3:
                assert part in code, f"{part!r} ({where}) is no longer in the server code"


@pytest.mark.parametrize("lang", LANGS)
def test_language_file(lang):
    path = I18N / f"{lang}.json"
    assert path.exists(), f"{path.name} missing"
    d = json.loads(path.read_text())
    missing = [k for k in KEYS if not str(d.get(k) or "").strip()]
    assert not missing, f"{len(missing)} untranslated, e.g. {missing[:3]}"
    assert not set(d) - set(KEYS), f"stale keys: {sorted(set(d) - set(KEYS))[:3]}"
    for k in KEYS:
        assert slots(d[k]) == slots(k), f"{lang}: slots differ in {k!r}"
        assert tags(d[k]) == tags(k), f"{lang}: markup differs in {k!r}"
