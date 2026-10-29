import json

from app import seoul


def _cache(tmp_path, monkeypatch, data):
    f = tmp_path / "towing.json"
    f.write_text(json.dumps({"radius_m": 1000, "data": data}, ensure_ascii=False))
    monkeypatch.setattr(seoul, "TOWING_FILE", f)
    seoul._towing.cache_clear()


def test_towing_near_band_filter_most_reports_first(tmp_path, monkeypatch):
    lat, lon = seoul.YANGJAE
    _cache(tmp_path, monkeypatch, [
        {"address": "b-addr", "band_m": 500, "count": 9, "first": "2024-01-01",
         "last": "2025-01-01", "kinds": {"상기 사항 외 보도": 9}},
        {"address": "a-addr", "band_m": 300, "count": 9, "first": "2023-05-01",
         "last": "2023-06-01", "kinds": {"자전거 도로": 9}},
        {"address": "c-addr", "band_m": 1000, "count": 50, "first": "2022-01-01",
         "last": "2022-01-02", "kinds": {}},
    ])
    got = seoul.towing_near(lat, lon, 500)
    assert [p["address"] for p in got] == ["a-addr", "b-addr"]  # band_m <= 500, ties by address
    assert [p["count"] for p in got] == [9, 9]
    assert all(p["band_m"] <= 500 for p in got)
    assert seoul.towing_near(lat, lon, 1000)[0]["address"] == "c-addr"  # most reports first
    seoul._towing.cache_clear()


def test_towing_near_no_coords_kinds_en_and_aliases(tmp_path, monkeypatch):
    _cache(tmp_path, monkeypatch, [
        {"address": "a", "band_m": 300, "count": 2, "first": "2023-05-01",
         "last": "2023-06-01", "by_year": {"2023": 2}, "kinds": {"자전거 도로": 2}},
    ])
    (got,) = seoul.towing_near(*seoul.YANGJAE, 300)
    assert set(got) >= {"address", "band_m", "count", "first", "last", "kinds", "by_year"}
    assert "lat" not in got and "lon" not in got and "dist_m" not in got
    assert got["kinds_en"] == {"Bike lane": 2}
    assert got["addr"] == "a"  # alias used by static/app.js
    assert got["types_en"] == got["kinds_en"]
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
