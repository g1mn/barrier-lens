"""Seoul public data for the map: e-scooter towing history and Yangjae Station accessibility.

Everything is read from small caches in data/seoul/ written by scripts/fetch_seoul.py.
Nothing here calls VWorld at runtime. station(live=True) may refresh the elevator status from
Seoul Open Data (OA-15994) at most once every 10 minutes and falls back to the cache.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from pathlib import Path

import httpx

from app.config import key

DATA = Path(__file__).resolve().parents[1] / "data" / "seoul"
TOWING_FILE = DATA / "towing-yangjae.json"
STATION_FILE = DATA / "yangjae-station.json"
EXITS_FILE = DATA / "yangjae-exits-osm.json"

# Yangjae Station centre: mean of the 4 OA-21212 elevator nodes named 양재(서초구청) (see yangjae-station.json).
YANGJAE = (37.4840283, 127.0346411)

# English glosses for the towing "TYPE" values seen in OA-23057 (Korean original stays the key).
TYPE_EN = {
    "보도와 차도가 구분된 도로의 차도": "Roadway of a street with a separate sidewalk",
    "상기 사항 외 보도": "Other sidewalk",
    "횡단보도 주변(3m)": "Within 3 m of a crosswalk",
    "횡단보도 주변(3m 이내)": "Within 3 m of a crosswalk",
    "보호구역(어린이·노인·장애인)": "Protected zone (children, older or disabled people)",
    "그 외 지역": "Elsewhere",
    "버스정류소 및 택시승강장 주변(5m 이내)": "Within 5 m of a bus stop or taxi stand",
    "자전거 도로": "Bike lane",
    "지하철역 전면(5m이내)": "Within 5 m of a subway station front",
    "점자블록 또는 교통약자 엘리베이터": "On tactile paving or at an accessible elevator",
    "점자블록, 엘리베이터 입구": "On tactile paving or at an elevator entrance",
    "교통섬 내부": "Inside a traffic island",
    "오신고": "False report",
    "버스정류장, 택시 승강장": "Bus stop or taxi stand",
    "버스정류장": "Bus stop",
    "횡단보도, 산책로 등": "Crosswalk, walking path, etc.",
    "자전거도로": "Bike lane",
    "보도 중앙": "Middle of the sidewalk",
    "지하철역 진출입로 및 주변": "Subway station entrance or its surroundings",
    "지하철역": "Subway station",
    "점자보도": "Tactile paving",
    "기타": "Other",
    "기타(신고자 자유 기재)": "Other (reporter's own words, not published)",
}


@lru_cache(maxsize=1)
def _towing() -> dict:
    if not TOWING_FILE.exists():
        return {"data": [], "radius_m": 0, "centre": {"lat": YANGJAE[0], "lon": YANGJAE[1]}}
    return json.loads(TOWING_FILE.read_text())


def towing_meta() -> dict:
    """What the towing cache covers (no points): centre, radius, date range, caveats, source."""
    t = _towing()
    return {k: v for k, v in t.items() if k != "data"}


def towing_near(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Towing records grouped by lot address with band_m <= radius_m, most reports first.

    Each item: address, band_m, count, first, last (report dates YYYY-MM-DD), kinds ({kind: n},
    Korean form categories), plus by_year, kinds_en, and addr / types_en (aliases kept for
    static/app.js). Reads data/seoul/towing-yangjae.json only; never calls VWorld or Seoul Open Data.
    lat/lon are ignored: the cache only covers Yangjae Station. Only 양재동 within 1 km of Yangjae
    Station is cached: an empty list outside it means "not cached", not "no towing".
    """
    out = []
    for p in _towing().get("data", []):
        if p["band_m"] > radius_m:
            continue
        kinds = p.get("kinds", {})
        kinds_en = {TYPE_EN.get(t, t): n for t, n in kinds.items()}
        out.append({
            "address": p["address"], "band_m": p["band_m"], "count": p["count"],
            "first": p["first"], "last": p["last"], "kinds": kinds,
            "by_year": p.get("by_year", {}), "kinds_en": kinds_en,
            "addr": p["address"], "types_en": kinds_en,
        })
    out.sort(key=lambda p: (-p["count"], p["address"]))
    return out


def exit_position(ref: str) -> tuple[float, float] | None:
    """(lat, lon) of a Yangjae Station exit by its number, from the cached OpenStreetMap nodes."""
    if not EXITS_FILE.exists():
        return None
    for n in json.loads(EXITS_FILE.read_text()).get("nodes", []):
        if n.get("railway") == "subway_entrance" and n.get("ref") == str(ref):
            return n["lat"], n["lon"]
    return None


# ---------------------------------------------------------------- station accessibility

_live_cache: dict = {"at": 0.0, "rows": None, "as_of": None}
LIVE_TTL_S = 600


def _fetch_live_status() -> list[dict]:
    k = key("SEOUL_OPEN_DATA_KEY")
    if not k:
        raise RuntimeError("SEOUL_OPEN_DATA_KEY is not set")
    rows: list[dict] = []
    with httpx.Client(timeout=8) as client:
        start, total = 1, None
        while total is None or start <= total:
            url = f"http://openapi.seoul.go.kr:8088/{k}/json/SeoulMetroFaciInfo/{start}/{start + 999}/"
            try:
                d = client.get(url).json()["SeoulMetroFaciInfo"]
            except Exception as e:  # noqa: BLE001  (never let the URL, which holds the key, reach a log)
                raise RuntimeError(f"OA-15994 fetch failed: {type(e).__name__}") from None
            total = int(d["list_total_count"])
            rows += d.get("row", [])
            start += 1000
    return [{"name": r["ELVTR_NM"], "kind": r["ELVTR_SE"], "where": r["INSTL_PSTN"],
             "floors": r["OPR_SEC"], "status": r["USE_YN"], "station": r["STN_NM"]}
            for r in rows if (r.get("STN_NM") or "").startswith("양재(")]


def _exit_ref(where: str) -> str | None:
    # "11번 출입구" -> "11"
    head = where.split("번")[0].strip()
    return head if head.isdigit() else None


def station(live: bool = False) -> dict:
    """Yangjae Station accessibility: exits (OSM positions) joined with elevator status (OA-15994)."""
    cached = json.loads(STATION_FILE.read_text()) if STATION_FILE.exists() else {}
    status_rows, as_of, source, note = cached.get("live_status", []), cached.get("fetched_at"), "cache", None
    if live:
        now = time.time()
        if _live_cache["rows"] is not None and now - _live_cache["at"] < LIVE_TTL_S:
            status_rows, as_of, source = _live_cache["rows"], _live_cache["as_of"], "live"
        else:
            try:
                rows = _fetch_live_status()
                _live_cache.update(at=now, rows=rows, as_of=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
                status_rows, as_of, source = rows, _live_cache["as_of"], "live"
            except Exception as e:  # noqa: BLE001
                note = f"live refresh failed ({e}); showing the cached status"
    exits_osm = json.loads(EXITS_FILE.read_text())["nodes"] if EXITS_FILE.exists() else []
    by_exit: dict[str, list[dict]] = {}
    for r in status_rows:
        ref = _exit_ref(r["where"])
        if ref:
            by_exit.setdefault(ref, []).append(r)
    exits = []
    for n in exits_osm:
        if n.get("railway") != "subway_entrance" or "ref" not in n:
            continue
        lifts = [r for r in by_exit.get(n["ref"], []) if r["kind"] == "EV"]
        exits.append({
            "ref": n["ref"], "lat": n["lat"], "lon": n["lon"],
            "elevator": ("none listed" if not lifts else
                         "working" if all(r["status"] == "사용가능" for r in lifts) else "out of service"),
            "elevator_rows": lifts,
            "osm_wheelchair": n.get("wheelchair"),
        })
    exits.sort(key=lambda e: (len(e["ref"]), e["ref"]))
    return {
        "station": "Yangjae (Line 3 / Shinbundang)",
        "centre": {"lat": YANGJAE[0], "lon": YANGJAE[1]},
        "status_source": source,
        "status_as_of": as_of,
        "note": note,
        "exits": exits,
        "inside": [r for r in status_rows if _exit_ref(r["where"]) is None],
        "caveats": [
            "Elevator status covers the Line 3 (Seoul Metro) side only. The Shinbundang side is run by "
            "another operator and is not in OA-15994.",
            "'none listed' means the feed has no elevator row for that exit, not that the exit has no lift.",
            "Exit positions are from OpenStreetMap.",
        ],
        "sources": ["Seoul Open Data OA-15994 (KOGL Type 1)", "(c) OpenStreetMap contributors, ODbL"],
    }
