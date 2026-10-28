import json

from app import seoul


def _cache(tmp_path, monkeypatch, data):
    f = tmp_path / "towing.json"
    f.write_text(json.dumps({"radius_m": 1000, "data": data}, ensure_ascii=False))
    monkeypatch.setattr(seoul, "TOWING_FILE", f)
    seoul._towing.cache_clear()


def test_towing_near_nearest_first_and_radius(tmp_path, monkeypatch):
    lat, lon = seoul.YANGJAE
    _cache(tmp_path, monkeypatch, [
        {"address": "far", "lat": lat + 0.0030, "lon": lon, "count": 9, "first": "2024-01-01",
         "last": "2025-01-01", "kinds": {"상기 사항 외 보도": 9}},
        {"address": "near", "lat": lat + 0.0005, "lon": lon, "count": 2, "first": "2023-05-01",
         "last": "2023-06-01", "kinds": {"자전거 도로": 2}},
        {"address": "out", "lat": lat + 0.0100, "lon": lon, "count": 50, "first": "2022-01-01",
         "last": "2022-01-02", "kinds": {}},
    ])
    got = seoul.towing_near(lat, lon, 500)
    assert [p["address"] for p in got] == ["near", "far"]
    assert got[0]["dist_m"] < got[1]["dist_m"] <= 500
    assert set(got[0]) >= {"address", "lat", "lon", "count", "first", "last", "kinds"}
    assert got[0]["kinds_en"] == {"Bike lane": 2}
    assert got[0]["addr"] == "near"  # alias used by static/app.js
    seoul._towing.cache_clear()


def test_towing_near_without_cache_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(seoul, "TOWING_FILE", tmp_path / "missing.json")
    seoul._towing.cache_clear()
    assert seoul.towing_near(*seoul.YANGJAE, 1000) == []
    seoul._towing.cache_clear()


def test_exit_position_from_osm_cache():
    pos = seoul.exit_position("1")
    assert pos is not None and abs(pos[0] - 37.48) < 0.01 and abs(pos[1] - 127.03) < 0.01
    assert seoul.exit_position("99") is None
