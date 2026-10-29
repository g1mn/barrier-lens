"""Fetch the Seoul open data the Gwanghwamun & Gyeongbokgung demo needs and cache it under data/seoul/.

Sources (Korea Open Government License Type 1 / 공공누리 1유형, see data/seoul/ATTRIBUTION.txt):
  aiTbAutoKickboard  OA-23057  e-scooter towing history (lot-number address only, no coordinates)
  SeoulMetroFaciInfo OA-15994  live elevator / escalator status, Seoul Metro stations
  tbTraficElvtr      OA-21212  elevator positions around subway stations (WGS84, 2020 data)
  tbTraficEntrcLft   OA-21211  wheelchair lift positions at station entrances (WGS84, 2020 data)
Geocoding goes through the VWorld address API during "build" only, one request at a time,
1.5 s apart, and only in memory: VWorld results must be used in real time and must not be stored,
so just the 300 / 500 / 1000 m distance band of each address is written to disk.

Steps (run in this order; towing resumes from its cache):
  .venv/bin/python scripts/fetch_seoul.py towing           page the whole towing table once, keep Jongno-gu + Jung-gu
  .venv/bin/python scripts/fetch_seoul.py station          Gwanghwamun & Gyeongbokgung elevators, lifts and live status
  .venv/bin/python scripts/fetch_seoul.py exits            exit / elevator positions from OpenStreetMap (one Overpass call per station)
  .venv/bin/python scripts/fetch_seoul.py build            write data/seoul/towing-gwanghwamun.json
  .venv/bin/python scripts/fetch_seoul.py stats            counts only, no network
Keys are read through app.config.key() and are never printed: every error message is redacted.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import key  # noqa: E402
from app.seoul import CENTRE as GATE, STATIONS  # noqa: E402  (positions from OpenStreetMap, ODbL)

SEOUL_DIR = ROOT / "data" / "seoul"          # small, committed
CACHE_DIR = ROOT / "data" / "cache" / "seoul"  # large, gitignored
TOW_ROWS = CACHE_DIR / "towing-rows.jsonl"
TOW_PROGRESS = CACHE_DIR / "towing-progress.jsonl"
TOW_OUT = SEOUL_DIR / "towing-gwanghwamun.json"
STATION_OUT = SEOUL_DIR / "gwanghwamun-stations.json"
EXITS_OUT = SEOUL_DIR / "gwanghwamun-exits-osm.json"

SEOUL_BASE = "http://openapi.seoul.go.kr:8088"
VWORLD_URL = "https://api.vworld.kr/req/address"
PAGE = 1000            # Seoul Open API maximum rows per call
KEEP_GU = {"종로구", "중구"}
# Only these get geocoded: the legal dongs (법정동) within about 1 km of Gwanghwamun Gate.
# Neighbouring dongs inside the circle are deliberately left out to keep VWorld calls low.
_JONGNO = ("세종로", "사직동", "내자동", "적선동", "통의동", "창성동", "효자동", "궁정동", "신교동",
           "청운동", "옥인동", "누상동", "누하동", "필운동", "체부동", "통인동", "내수동", "도렴동",
           "당주동", "신문로1가", "신문로2가", "수송동", "중학동", "송현동", "사간동", "소격동",
           "삼청동", "팔판동", "안국동", "화동", "견지동", "공평동", "청진동", "서린동", "종로1가",
           "관훈동", "경운동")
_JUNG = ("태평로1가", "무교동", "다동", "정동")
TARGET_DONGS = tuple(f"서울특별시 종로구 {n}" for n in _JONGNO) + tuple(f"서울특별시 중구 {n}" for n in _JUNG)
BUILD_RADIUS_M = 1000
BANDS_M = (300, 500, 1000)  # written per address; anything beyond BUILD_RADIUS_M is dropped
VWORLD_GAP_S = 1.5


def _secrets() -> list[str]:
    return [v for v in (key("SEOUL_OPEN_DATA_KEY"), key("VWORLD_API_KEY")) if v]


def redact(text: object) -> str:
    s = str(text)
    for v in _secrets():
        s = s.replace(v, "<key>")
    return s


def log(*parts: object) -> None:
    print(redact(" ".join(str(p) for p in parts)), flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- Seoul Open API

def seoul_page(client: httpx.Client, service: str, start: int, end: int) -> dict:
    k = key("SEOUL_OPEN_DATA_KEY")
    if not k:
        raise SystemExit("SEOUL_OPEN_DATA_KEY is not set (~/.config/barrier-lens/.env)")
    url = f"{SEOUL_BASE}/{k}/json/{service}/{start}/{end}/"
    last = None
    for attempt in range(4):
        try:
            r = client.get(url, timeout=60)
            r.raise_for_status()
            d = r.json()
            if service not in d:
                res = d.get("RESULT", {})
                raise RuntimeError(f"{service} {start}-{end}: {res.get('CODE')} {res.get('MESSAGE')}")
            return d[service]
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  retry {attempt + 1} {service} {start}-{end}: {redact(repr(e))[:200]}")
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"giving up on {service} {start}-{end}: {redact(repr(last))[:200]}")


def seoul_all(client: httpx.Client, service: str) -> list[dict]:
    first = seoul_page(client, service, 1, PAGE)
    rows, total = list(first.get("row", [])), int(first["list_total_count"])
    for start in range(PAGE + 1, total + 1, PAGE):
        rows += seoul_page(client, service, start, start + PAGE - 1).get("row", [])
        time.sleep(0.3)
    return rows


def cmd_towing(_args: argparse.Namespace) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    done: set[int] = set()
    if TOW_PROGRESS.exists():
        for line in TOW_PROGRESS.read_text().splitlines():
            done.add(json.loads(line)["start"])
    with httpx.Client() as client:
        head = seoul_page(client, "aiTbAutoKickboard", 1, 1)
        total = int(head["list_total_count"])
        starts = [s for s in range(1, total + 1, PAGE) if s not in done]
        log(f"towing: list_total_count={total}, pages left={len(starts)} of {math.ceil(total / PAGE)}")
        with TOW_ROWS.open("a") as rows_f, TOW_PROGRESS.open("a") as prog_f:
            for n, start in enumerate(starts, 1):
                page = seoul_page(client, "aiTbAutoKickboard", start, start + PAGE - 1)
                rows = page.get("row", [])
                kept = [r for r in rows if r.get("GU_NM") in KEEP_GU]
                for r in kept:
                    rows_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                rows_f.flush()
                prog_f.write(json.dumps({"start": start, "rows": len(rows), "kept": len(kept),
                                         "total": int(page["list_total_count"]), "at": now_iso()}) + "\n")
                prog_f.flush()
                if n % 20 == 0 or n == len(starts):
                    log(f"  page {n}/{len(starts)} start={start} rows={len(rows)} kept={len(kept)}")
                time.sleep(0.3)
    cmd_stats(_args)


def load_tow_rows() -> list[dict]:
    if not TOW_ROWS.exists():
        return []
    by_no: dict[str, dict] = {}
    for line in TOW_ROWS.read_text().splitlines():
        r = json.loads(line)
        by_no[r["NO"]] = r  # paging can repeat a row; NO is the record id
    return list(by_no.values())


def in_target(addr: str) -> bool:
    return any(addr.startswith(p + " ") for p in TARGET_DONGS)


# ---------------------------------------------------------------- VWorld geocoding (in memory only; never written to disk)

def vworld_geocode(client: httpx.Client, addr: str, kind: str) -> dict:
    k = key("VWORLD_API_KEY")
    if not k:
        raise SystemExit("VWORLD_API_KEY is not set (~/.config/barrier-lens/.env)")
    params = {"service": "address", "request": "getcoord", "version": "2.0", "crs": "epsg:4326",
              "address": addr, "refine": "true", "simple": "false", "format": "json",
              "type": kind, "key": k}
    r = client.get(VWORLD_URL, params=params, timeout=20)
    r.raise_for_status()
    resp = r.json().get("response", {})
    status = resp.get("status")
    if status == "OK":
        p = resp["result"]["point"]
        return {"status": "OK", "lat": round(float(p["y"]), 7), "lon": round(float(p["x"]), 7),
                "refined": (resp.get("refined") or {}).get("text")}
    if status == "NOT_FOUND":
        return {"status": "NOT_FOUND"}
    err = resp.get("error") or {}
    raise RuntimeError(f"vworld status={status} code={err.get('code')} {err.get('text')}")


# ---------------------------------------------------------------- station accessibility

def cmd_station(_args: argparse.Namespace) -> None:
    SEOUL_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        status_rows = seoul_all(client, "SeoulMetroFaciInfo")
        elvtr_rows = seoul_all(client, "tbTraficElvtr")
        lift_rows = seoul_all(client, "tbTraficEntrcLft")
    fetched = now_iso()

    def point(wkt: str) -> tuple[float, float]:
        x, y = wkt.replace("POINT(", "").rstrip(")").split()
        return float(y), float(x)

    def nodes(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            # OA-21212 names Gyeongbokgung "경복궁(정부서울청사)"; Gwanghwamun has no OA-21212 nodes
            if (r.get("SBWY_STN_NM") or "").strip() == "경복궁(정부서울청사)":
                lat, lon = point(r["NODE_WKT"])
                out.append({"node_id": int(r["NODE_ID"]), "lat": round(lat, 7), "lon": round(lon, 7),
                            "station_code": r.get("SBWY_STN_CD"), "dong": r.get("EMD_NM")})
        return out

    def live_key(stn_nm: str) -> str | None:
        # OA-15994 STN_NM now comes without a line suffix ("경복궁"); older rows had one ("양재(3)")
        for s in STATIONS:
            if stn_nm == s["oa_name"] or stn_nm.startswith(s["oa_name"] + "("):
                return s["key"]
        return None

    elevators = nodes(elvtr_rows)
    lifts = nodes(lift_rows)
    live = []
    for r in status_rows:
        sk = live_key(r.get("STN_NM") or "")
        if sk is not None:
            live.append({"name": r["ELVTR_NM"], "kind": r["ELVTR_SE"], "where": r["INSTL_PSTN"],
                         "floors": r["OPR_SEC"], "status": r["USE_YN"], "station": sk})
    out = {
        "area": "Gwanghwamun & Gyeongbokgung",
        "fetched_at": fetched,
        "centre": {"lat": GATE[0], "lon": GATE[1], "how": "Gwanghwamun Gate (OSM way 170199440)"},
        "stations": [{"key": s["key"], "name": s["name"], "line": s["line"]} for s in STATIONS],
        "elevators_2020": elevators,
        "entrance_lifts_2020": lifts,
        "live_status": live,
        "counts": {"SeoulMetroFaciInfo_total": len(status_rows), "tbTraficElvtr_total": len(elvtr_rows),
                   "tbTraficEntrcLft_total": len(lift_rows)},
        "notes": [
            "live_status comes from OA-15994. Both stations are Seoul Metro lines"
            " (Gyeongbokgung Line 3, Gwanghwamun Line 5), so both are in this feed.",
            "elevators_2020 / entrance_lifts_2020 are 2020 positions (OA-21212 / OA-21211) for Gyeongbokgung"
            " only and do not say which exit they serve.",
        ],
        "sources": ["OA-15994 SeoulMetroFaciInfo", "OA-21212 tbTraficElvtr", "OA-21211 tbTraficEntrcLft"],
    }
    STATION_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    log(f"station: {len(elevators)} elevator nodes, {len(lifts)} lifts, {len(live)} live status rows"
        f" -> {STATION_OUT.relative_to(ROOT)}")


def _overpass(q: str) -> list[dict]:
    errors = []
    for endpoint in ("https://overpass-api.de/api/interpreter",
                     "https://overpass.private.coffee/api/interpreter",
                     "https://overpass.kumi.systems/api/interpreter"):
        try:
            r = httpx.post(endpoint, data={"data": q}, timeout=60,
                           headers={"User-Agent": "barrier-lens-hackathon/0.1 (one-off fetch)"})
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{endpoint}: {e!r}"[:160])
            time.sleep(3)
    raise SystemExit("Overpass failed: " + " | ".join(errors))


def cmd_exits(_args: argparse.Namespace) -> None:
    """Exit and elevator positions from OpenStreetMap (ODbL), so live status can sit on the map."""
    seen: dict[int, dict] = {}
    for s in STATIONS:
        q = (f"[out:json][timeout:25];("
             f'node["railway"="subway_entrance"](around:450,{s["lat"]},{s["lon"]});'
             f'node["highway"="elevator"](around:450,{s["lat"]},{s["lon"]}););out body;')
        for e in _overpass(q):
            seen.setdefault(e["id"], e)
    keep = ("ref", "name", "name:en", "wheelchair", "railway", "highway", "level", "entrance")
    nodes = []
    for e in seen.values():
        near = min(STATIONS, key=lambda s: haversine_m(s["lat"], s["lon"], e["lat"], e["lon"]))
        nodes.append({"osm_id": e["id"], "lat": e["lat"], "lon": e["lon"], "station": near["key"],
                      **{k: e["tags"][k] for k in keep if k in e.get("tags", {})}})
    out = {
        "what": "Subway entrances and elevators within 450 m of Gyeongbokgung and Gwanghwamun stations,"
                " from OpenStreetMap",
        "fetched_at": now_iso(),
        "license": "ODbL 1.0, (c) OpenStreetMap contributors",
        "nodes": nodes,
    }
    EXITS_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    log(f"exits: {len(nodes)} OSM nodes -> {EXITS_OUT.relative_to(ROOT)}")


# ---------------------------------------------------------------- build the demo cache

# TYPE is a category picked from a district form, but some rows carry the reporter's own free text.
# Free text is folded into one bucket so complaint wording never reaches the published cache.
KNOWN_KINDS = {
    "보도와 차도가 구분된 도로의 차도", "상기 사항 외 보도", "버스정류장, 택시 승강장", "횡단보도, 산책로 등",
    "횡단보도 주변(3m)", "횡단보도 주변(3m 이내)", "자전거 도로", "자전거도로", "보호구역(어린이·노인·장애인)",
    "보도 중앙", "버스정류소 및 택시승강장 주변(5m 이내)", "버스정류장", "그 외 지역", "지하철역 진출입로 및 주변",
    "지하철역 전면(5m이내)", "지하철역", "점자블록 또는 교통약자 엘리베이터", "점자블록, 엘리베이터 입구",
    "점자보도", "교통섬 내부", "기타", "오신고",
}
FREE_TEXT_KIND = "기타(신고자 자유 기재)"


def kind_of(t: str) -> str:
    t = (t or "").strip()
    return t if t in KNOWN_KINDS else FREE_TEXT_KIND

def cmd_build(_args: argparse.Namespace) -> None:
    station = json.loads(STATION_OUT.read_text())
    c = station["centre"]
    rows = [r for r in load_tow_rows() if in_target(r["ADDR"])]
    freq = collections.Counter(r["ADDR"].strip() for r in rows)
    # VWorld answers stay in memory: each distinct address is geocoded once, only its band is kept.
    bands: dict[str, int] = {}
    not_found, beyond, calls, fails_in_row = 0, 0, 0, 0
    with httpx.Client() as client:
        for addr in [a for a, _ in freq.most_common()]:
            g = None
            for kind in ("parcel", "road"):
                try:
                    calls += 1
                    g = vworld_geocode(client, addr, kind)
                    fails_in_row = 0
                except Exception as e:  # noqa: BLE001
                    fails_in_row += 1
                    log(f"  vworld error ({fails_in_row} in a row) for {addr}: {redact(repr(e))[:160]}")
                    if fails_in_row >= 6:
                        log(f"  stopping: 6 failures in a row after {calls} calls; rerun later")
                        return
                    time.sleep(VWORLD_GAP_S * 4)
                    g = None
                time.sleep(VWORLD_GAP_S)
                if g and g["status"] == "OK":
                    break
                if g is None:
                    break  # transport error: leave it out of this build
            if g is None:
                continue
            if g["status"] == "NOT_FOUND":
                not_found += 1
                continue
            band = next((b for b in BANDS_M if haversine_m(c["lat"], c["lon"], g["lat"], g["lon"]) <= b), None)
            if band is None:
                beyond += 1
            else:
                bands[addr] = band
            if calls % 50 == 0:
                log(f"  {calls} vworld calls so far")
    log(f"build: geocoded {len(freq)} addresses with {calls} vworld calls")
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["ADDR"].strip() in bands:
            groups[r["ADDR"].strip()].append(r)
    points = []
    for addr, rs in groups.items():
        dates = sorted(r["DCLR_DT"] for r in rs)
        points.append({
            "address": addr, "band_m": bands[addr],
            "count": len(rs), "first": dates[0], "last": dates[-1],
            "by_year": dict(sorted(collections.Counter(x[:4] for x in dates).items())),
            "kinds": dict(collections.Counter(kind_of(r["TYPE"]) for r in rs).most_common()),
        })
    points.sort(key=lambda p: (-p["count"], p["address"]))
    all_dates = sorted(d for p in points for d in (p["first"], p["last"]))
    out = {
        "what": "E-scooter towing records (OA-23057) in the listed Jongno-gu and Jung-gu dongs"
                " within 1 km of Gwanghwamun Gate, grouped by lot address",
        "built_at": now_iso(),
        "centre": c,
        "radius_m": BUILD_RADIUS_M,
        "records": sum(p["count"] for p in points),
        "points": len(points),
        "date_range": [all_dates[0], all_dates[-1]] if all_dates else None,
        "coverage": {
            "dongs_geocoded": list(TARGET_DONGS),
            "rows_in_dongs": len(rows),
            "rows_in_dongs_date_range": ([min(r["DCLR_DT"] for r in rows), max(r["DCLR_DT"] for r in rows)]
                                         if rows else None),
            "distinct_addresses": len(freq),
            "addresses_banded": len(bands),
            "rows_banded": sum(1 for r in rows if r["ADDR"].strip() in bands),
            "addresses_not_found": not_found,
            "addresses_beyond_1km": beyond,
        },
        "caveats": [
            "Addresses are placed in 300 / 500 / 1000 m bands from Gwanghwamun Gate by geocoding each lot"
            " address with VWorld at build time; no coordinates are stored (VWorld results may not be stored).",
            "Records are past towing reports (DCLR_DT = report date), not current obstacles.",
            "Districts changed how they log towing over the years; compare years inside one district only.",
            f"Only these dongs are geocoded: {', '.join(_JONGNO + _JUNG)}. Parts of the 1 km circle in other"
            " dongs have towing too but are not in this file.",
            "Some report kinds are the reporter's own free text; those are counted as 기타(신고자 자유 기재).",
        ],
        "source": "Seoul Open Data Plaza OA-23057 (aiTbAutoKickboard), KOGL Type 1;"
                  " distance bands from VWorld geocoding at build time (not stored)",
        "data": points,
    }
    TOW_OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    log(f"build: {out['records']} records at {out['points']} addresses within {BUILD_RADIUS_M} m,"
        f" dates {out['date_range']}, not found: {not_found}, beyond 1 km: {beyond}"
        f" -> {TOW_OUT.relative_to(ROOT)}")


def cmd_stats(_args: argparse.Namespace) -> None:
    rows = load_tow_rows()
    raw_lines = len(TOW_ROWS.read_text().splitlines()) if TOW_ROWS.exists() else 0
    prog = [json.loads(x) for x in TOW_PROGRESS.read_text().splitlines()] if TOW_PROGRESS.exists() else []
    log(f"towing pages done={len(prog)}, rows returned={sum(p['rows'] for p in prog)},"
        f" kept lines={raw_lines}, unique kept NO={len(rows)}")
    if rows:
        dates = sorted(r["DCLR_DT"] for r in rows)
        log(f"kept date range {dates[0]} .. {dates[-1]}")
    by_gu = collections.Counter(r["GU_NM"] for r in rows)
    log("by gu:", dict(by_gu))
    for p in TARGET_DONGS:
        rs = [r for r in rows if r["ADDR"].startswith(p + " ")]
        log(f"  {p}: rows={len(rs)} unique addresses={len({r['ADDR'].strip() for r in rs})}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("towing")
    sub.add_parser("station")
    sub.add_parser("exits")
    sub.add_parser("build")
    sub.add_parser("stats")
    args = ap.parse_args()
    {"towing": cmd_towing, "station": cmd_station, "exits": cmd_exits,
     "build": cmd_build, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
