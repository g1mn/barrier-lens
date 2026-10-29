"""app/spark.py guards: explanation numbers must come from the measurement, and triage rejects distances."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import spark  # noqa: E402

MEASUREMENT = {"verdict": "passes", "gap_cm": 95.0, "band_cm": [90.0, 99.0], "why": "test"}


def test_foreign_numbers_flags_only_numbers_outside_the_measurement():
    assert spark._foreign_numbers("The free gap measures about 95 cm (between 90 and 99 cm).",
                                 MEASUREMENT) == []
    assert spark._foreign_numbers("The free gap measures about 120 cm.", MEASUREMENT) == ["120"]


def test_distance_guard_finds_sizes_but_not_counts():
    assert spark._distances("about 80 cm") == ["80 cm"]
    assert spark._distances("1.2 m") == ["1.2 m"]
    assert spark._distances("3 scooters") == []


def test_explain_falls_back_when_the_model_invents_a_number(monkeypatch):
    """Two replies with a foreign number end on the fixed sentence; no network (fake _chat like
    test_spark_retry_never_sends_an_empty_assistant_turn)."""
    async def fake_chat(b, messages, schema, effort, max_tokens=0):
        return {"content": "The gap is 120 cm wide.", "finish": "stop", "model": "m",
                "latency_s": 0.0, "usage": {}}
    monkeypatch.setattr(spark, "_chat", fake_chat)
    monkeypatch.setattr(spark, "_backend", lambda: spark.Backend("meta", "u", "m", "k", "max_completion_tokens"))
    monkeypatch.setattr(spark, "_cache_put", lambda *a: None)
    out = asyncio.run(spark.explain(dict(MEASUREMENT)))
    assert out["model"] == "template" and out["text"] == spark._template(MEASUREMENT)
    assert any("120" in w for w in out["warnings"])


def test_reference_kind_picks_the_strip_over_paper():
    # a tactile strip must go to SAM as "tactile paving", not "paper" (Seoul sidewalks, 2026-10-29)
    assert spark.ref_kind({"visible": True, "kind": "tactile paving"}) == "tactile paving"
    assert spark.ref_kind({"visible": True, "evidence": "A yellow tactile paving strip runs across the foreground"}) == "tactile paving"
    assert spark.ref_kind({"visible": True, "evidence": "Four A4 sheets on the ground"}) == "paper"
    assert spark.ref_kind({"visible": False, "evidence": "No paper sheets or tactile paving strip"}) == "none"
    ref_schema = spark.TRIAGE_SCHEMA["schema"]["properties"]["reference"]
    assert "kind" in ref_schema["required"] and ref_schema["properties"]["kind"]["enum"] == list(spark.REF_KINDS)
