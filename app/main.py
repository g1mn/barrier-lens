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
