"""app.analyze end to end in replay mode: a synthetic clip with two bollards and four A4 sheets, cached triage and
SAM fixtures in temp dirs, no network. Checks the verdict comes from geometry, masks arrive as polygons, nothing is
paid, the spend cap refuses calls, and the HTTP routes wire through."""
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from app import analyze, blur, sam, spark  # noqa: E402
from app.sam import encode_rle  # noqa: E402
from synth_scene import render_scene  # noqa: E402

N_FRAMES = 10


@pytest.fixture(scope="module")
def scene():
    return render_scene(90, 3.0, "a4x4")


@pytest.fixture(scope="module")
def clip_src(tmp_path_factory, scene):
    """A 1 s, 10 fps H.264 clip of the synthetic scene (grey ground, white sheets, dark bollards)."""
    w, h = scene["frame_size"]
    img = np.full((h, w, 3), 120, np.uint8)
    img[scene["ref_mask"]] = 245
    for m in scene["barrier_masks"]:
        img[m] = 40
    out = tmp_path_factory.mktemp("clip") / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                    "-r", "10", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                   input=img.tobytes() * N_FRAMES, check=True)
    return out


class _NoFaces:
    name = "stub (no YuNet model on this machine)"

    def detect(self, img):
        return []


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp work/cache/fixture dirs, no keys, and any HTTP request fails the test."""
    for name in ("META_API_KEY", "BASETEN_API_KEY", "SPARK_BACKEND", "SAM_MODE", "BL_SPEND_CAP_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(analyze, "WORK_DIR", tmp_path / "analyze")
    monkeypatch.setattr(analyze, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(sam, "FIXTURE_DIR", tmp_path / "fixtures")
    monkeypatch.setattr(sam, "CACHE_DIR", tmp_path / "sam")
    monkeypatch.setattr(spark, "CACHE_DIR", tmp_path / "spark")
    (tmp_path / "fixtures").mkdir()
    if not (blur.MODEL_DIR / blur.YUNET_FILE).exists():
        monkeypatch.setattr(blur, "FaceDetector", _NoFaces)

    async def no_network(self, request, **kw):
        raise AssertionError(f"network call in a replay test: {request.url}")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    return tmp_path


def _write_sam_fixture(d: Path, concept: str, masks, size, n=N_FRAMES):
    objs = [{"id": k, "score": 0.9, "mask_rle": encode_rle(m)} for k, m in enumerate(masks)]
    (d / f"{concept.replace(' ', '-')}.json").write_text(json.dumps(
        {"concept": concept, "fps": 10.0, "width": size[0], "height": size[1], "frame_count": n, "model": "sam-3.1",
         "frames": [{"i": i, "objects": objs} for i in range(n)]}))


async def _seed_triage(src: Path, usable: bool = True):
    """Cache a triage answer under the digest spark computes for the frames analyze will send."""
    clip, _, _ = await analyze.prepare(src)
    jpgs, _ = analyze.triage_frames(clip, analyze.clip_info(clip)["frames"])
    h = hashlib.sha256()
    for j in jpgs:
        h.update(hashlib.sha256(j).digest())
    n = len(jpgs)
    spark._cache_put("triage", h.hexdigest(), {
        "checks": {"ground_visible": True, "barrier_visible": True, "sharp": usable, "seated_height": True},
        "reasons": ["test fixture"] if usable else ["frames are badly blurred"],
        "barriers": [{"concept": "bollard", "frames": list(range(n)), "evidence": "two bollards either side"}],
        "reference": {"visible": True, "frames": list(range(n)), "evidence": "sheets on the ground"},
        "model": "test-fixture", "warnings": []})


def test_replay_end_to_end(env, scene, clip_src):
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    r = asyncio.run(analyze.analyze(clip_src, 37.484, 127.034, replay=True))

    assert r["replay"] is True and r["triage"]["usable"] is True
    assert r["verdict"] == "passes", r["why"]
    lo, hi = r["band_cm"]
    assert lo <= 90 <= hi
    assert r["frame_size"] == list(scene["frame_size"]) and r["n_frames"] == N_FRAMES
    assert r["measurement_detail"]["reference"]["concept"] == "paper"
    roles = {m["concept"]: m for m in r["masks"]}
    assert set(roles) == {"bollard", "paper"} and roles["paper"]["role"] == "reference"
    obj = roles["bollard"]["frames"][0]["objects"][0]
    assert len(obj["polygon"]) >= 4 and all(len(p) == 2 for p in obj["polygon"])
    assert r["cost"]["sam_frames"] == 0 and r["cost"]["usd_estimate"] == 0
    assert not (env / "ledger.jsonl").exists()
    assert r["video_url"].endswith(r["job"]) and analyze.clip_path(r["job"]).exists()
    assert not list((env / "analyze").rglob("*unblurred*"))
    assert r["explanation"]  # template or cached text, never empty


def test_unusable_clip_returns_early(env, scene, clip_src):
    asyncio.run(_seed_triage(clip_src, usable=False))
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True))
    assert r["verdict"] == "unknown" and r["gap_cm"] is None and r["masks"] == []
    assert "could not be used" in r["why"] and "blurred" in r["why"]


def test_replay_miss_is_unknown_not_an_error(env, clip_src):
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True))
    assert r["verdict"] == "unknown" and r["triage"]["usable"] is None


def test_no_reference_is_unknown(env, scene, clip_src):
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True, concepts_hint=["bollard"]))
    assert r["verdict"] == "unknown" and "reference" in r["why"]
    assert any(m["frames"] for m in r["masks"] if m["concept"] == "bollard")


def test_spend_cap_refuses_before_any_call(env, clip_src, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "test-placeholder")
    monkeypatch.setenv("BL_SPEND_CAP_USD", "1")
    (env / "ledger.jsonl").write_text(json.dumps({"kind": "sam", "usd": 1.0}) + "\n")
    with pytest.raises(analyze.SpendCapError):
        asyncio.run(analyze.analyze(clip_src, None, None, replay=False))
    assert analyze.spent_usd() == 1.0


def test_polygons_of_a_square():
    m = np.zeros((40, 60), bool)
    m[10:30, 20:50] = True
    polys = analyze.polygons({"mask_rle": encode_rle(m)}, (60, 40))
    assert len(polys) == 1 and len(polys[0]) == 4
    xs, ys = [p[0] for p in polys[0]], [p[1] for p in polys[0]]
    assert (min(xs), max(xs), min(ys), max(ys)) == (20.5, 49.5, 10.5, 29.5)


def test_routes(env, scene, clip_src):
    from fastapi.testclient import TestClient
    from app.main import app
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    c = TestClient(app)
    with clip_src.open("rb") as f:
        r = c.post("/api/analyze", files={"video": ("clip.mp4", f, "video/mp4")},
                   data={"lat": "37.484", "lon": "127.034", "replay": "true"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["verdict"] == "passes" and j["location"] == {"lat": 37.484, "lon": 127.034}
    assert not list((env / "analyze" / "uploads").iterdir())  # the unblurred upload is gone
    v = c.get(j["video_url"])
    assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
    assert c.get("/api/analyze/clip/../../etc").status_code == 404
    bad = c.post("/api/analyze", files={"video": ("x.mp4", b"not a video", "video/mp4")},
                 data={"lat": "37.5", "lon": "127.0", "replay": "true"})
    assert bad.status_code == 422


def test_sample_route_answers(env, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setattr(sam, "FIXTURE_DIR", ROOT / "data" / "fixtures" / "sam")
    monkeypatch.setattr(sam, "CACHE_DIR", ROOT / "data" / "cache" / "sam")
    r = TestClient(app).get("/api/sample", params={"lat": 37.484, "lon": 127.034})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["sample"] is True and j["replay"] is True and j["cost"]["sam_frames"] == 0
    assert j["verdict"] in {"passes", "tight", "blocked", "unknown"}
    assert any(m["frames"] for m in j["masks"])
