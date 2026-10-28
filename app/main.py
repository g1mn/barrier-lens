from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="Barrier Lens")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


# ---- map routes (role: map) -------------------------------------------------
from fastapi import Query  # noqa: E402

from app import seoul  # noqa: E402


@app.get("/api/towing")
def api_towing(
    lat: float = Query(seoul.YANGJAE[0], ge=33.0, le=39.0),
    lon: float = Query(seoul.YANGJAE[1], ge=124.0, le=132.0),
    r: int = Query(500, ge=10, le=3000, description="radius in metres"),
) -> dict:
    """E-scooter towing history (OA-23057) grouped by lot address, nearest first. Cached area only."""
    points = seoul.towing_near(lat, lon, r)
    return {
        "query": {"lat": lat, "lon": lon, "r": r},
        "records": sum(p["count"] for p in points),
        "points": points,
        "coverage": seoul.towing_meta(),
    }


@app.get("/api/station")
def api_station(live: bool = False) -> dict:
    """Yangjae Station exits with elevator status (OA-15994, live=1 refreshes at most every 10 min)."""
    return seoul.station(live=live)
