import json

from app import seoul


def _cache(tmp_path, monkeypatch, data):
    f = tmp_path / "towing.json"
    f.write_text(json.dumps({"radius_m": 1000, "data": data}, ensure_ascii=False))
    monkeypatch.setattr(seoul, "TOWING_FILE", f)
    seoul._towing.cache_clear()


def _station_files(tmp_path, monkeypatch, live_rows, nodes):
    sf = tmp_path / "stations.json"
    sf.write_text(json.dumps({"fetched_at": "2026-10-29T00:00:00+09:00",
                              "live_status": live_rows}, ensure_ascii=False))
    ef = tmp_path / "exits.json"
    ef.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False))
    monkeypatch.setattr(seoul, "STATION_FILE", sf)
    monkeypatch.setattr(seoul, "EXITS_FILE", ef)


def test_towing_near_band_filter_most_reports_first(tmp_path, monkeypatch):
    lat, lon = seoul.CENTRE
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
    (got,) = seoul.towing_near(*seoul.CENTRE, 300)
    assert set(got) >= {"address", "band_m", "count", "first", "last", "kinds", "by_year"}
    assert "lat" not in got and "lon" not in got and "dist_m" not in got
    assert got["kinds_en"] == {"Bike lane": 2}
    assert got["addr"] == "a"  # alias used by static/app.js
    assert got["types_en"] == got["kinds_en"]
    seoul._towing.cache_clear()


def test_towing_near_without_cache_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(seoul, "TOWING_FILE", tmp_path / "missing.json")
    seoul._towing.cache_clear()
    assert seoul.towing_near(*seoul.CENTRE, 1000) == []
    seoul._towing.cache_clear()


def test_exit_position_from_osm_cache(tmp_path, monkeypatch):
    _station_files(tmp_path, monkeypatch, [], [
        {"osm_id": 1, "lat": 37.5758, "lon": 126.9735, "ref": "1",
         "railway": "subway_entrance", "station": "gyeongbokgung"},
        {"osm_id": 2, "lat": 37.5716, "lon": 126.9769, "ref": "1",
         "railway": "subway_entrance", "station": "gwanghwamun"},
    ])
    pos = seoul.exit_position("1", "gyeongbokgung")
    assert pos is not None and abs(pos[0] - 37.5758) < 0.01 and abs(pos[1] - 126.9735) < 0.01
    assert seoul.exit_position("1", "gwanghwamun") == (37.5716, 126.9769)
    assert seoul.exit_position("1") is not None  # no station key: first match
    assert seoul.exit_position("99") is None


def test_station_joins_status_to_exits_by_station_and_ref(tmp_path, monkeypatch):
    _station_files(tmp_path, monkeypatch, [
        {"name": "EV gbg", "kind": "EV", "where": "1번 출입구", "floors": "B1-1F",
         "status": "사용가능", "station": "gyeongbokgung"},
        {"name": "EV gwm", "kind": "EV", "where": "1번 출입구", "floors": "B1-1F",
         "status": "사용중지", "station": "gwanghwamun"},
    ], [
        {"osm_id": 1, "lat": 37.5758, "lon": 126.9735, "ref": "1",
         "railway": "subway_entrance", "station": "gyeongbokgung"},
        {"osm_id": 2, "lat": 37.5716, "lon": 126.9769, "ref": "1",
         "railway": "subway_entrance", "station": "gwanghwamun"},
    ])
    d = seoul.station(live=False)
    assert d["area"] == "Gwanghwamun & Gyeongbokgung"
    by_key = {s["key"]: s for s in d["stations"]}
    assert by_key["gyeongbokgung"]["exits"][0]["elevator"] == "working"
    assert by_key["gwanghwamun"]["exits"][0]["elevator"] == "out of service"
    flat = {(x["station"], x["ref"]): x for x in d["exits"]}
    assert flat[("gyeongbokgung", "1")]["station_name"] == "Gyeongbokgung"
    assert flat[("gwanghwamun", "1")]["station_name"] == "Gwanghwamun"
    assert flat[("gyeongbokgung", "1")]["elevator_rows"][0]["name"] == "EV gbg"
    assert flat[("gwanghwamun", "1")]["elevator_rows"][0]["name"] == "EV gwm"
