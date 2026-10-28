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


# ---- check routes (role: analyze-api) ---------------------------------------
import tempfile  # noqa: E402

from fastapi import File, Form, HTTPException, UploadFile  # noqa: E402

from app import analyze as _analyze  # noqa: E402
from app import blur as _blur  # noqa: E402
from app import spark as _spark  # noqa: E402

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


@app.post("/api/analyze")
async def api_analyze(
    video: UploadFile = File(..., description="phone clip; only the first 10 s are used"),
    lat: float = Form(..., ge=-90, le=90),
    lon: float = Form(..., ge=-180, le=180),
    replay: bool = Form(False, description="cached/fixture results only, never a paid call"),
) -> dict:
    """Upload a clip -> normalise, blur faces locally, triage, SAM, geometry, explanation (see app/analyze.py)."""
    up_dir = _analyze.WORK_DIR / "uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(video.filename or "").suffix.lower()[:8] or ".mp4"
    fd, name = tempfile.mkstemp(suffix=suffix, dir=up_dir)
    tmp = Path(name)
    try:
        size = 0
        with open(fd, "wb") as f:
            while chunk := await video.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "clip too large (200 MB max); 10 seconds is enough")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "empty upload")
        return await _analyze.analyze(tmp, lat, lon, replay=replay)
    except _analyze.AnalyzeError as e:
        raise HTTPException(422, str(e)) from None
    except _analyze.SpendCapError as e:
        raise HTTPException(429, str(e)) from None
    except _blur.BlurError as e:
        raise HTTPException(503, f"faces could not be blurred, so nothing was sent: {e}") from None
    except (_spark.SparkError, _analyze.sam.SamError) as e:
        raise HTTPException(502, f"model call failed: {str(e)[:300]}") from None
    finally:
        tmp.unlink(missing_ok=True)  # the original upload is unblurred; keep only the blurred clip


@app.get("/api/analyze/clip/{job}")
def api_analyze_clip(job: str) -> FileResponse:
    """The normalised, face-blurred clip an analysis ran on (what the masks refer to)."""
    p = _analyze.clip_path(job)
    if p is None:
        raise HTTPException(404, "no such clip")
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/sample")
async def api_sample(
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
) -> dict:
    """The same check on a bundled dev clip in replay mode (no paid calls), or a precomputed result."""
    try:
        return await _analyze.run_sample(lat, lon)
    except _analyze.AnalyzeError as e:
        raise HTTPException(404, str(e)) from None
    except _blur.BlurError as e:
        raise HTTPException(503, str(e)) from None
