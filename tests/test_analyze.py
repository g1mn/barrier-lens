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
    # no reference in the clip: measured from the default camera pose (or "unknown" until geometry supports it)
    a = r["assumptions"]
    assert a["mode"] == "default" and a["label"] == "Estimated with default assumptions"
    assert a["pose"]["kind"] == "camera_pose" and a["pose"]["height_range_m"] == [0.65, 0.90]
    assert r["verdict"] == "unknown" or r["band_cm"] is not None
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


# ---------------------------------------------------------------- profile / calibration (role: calibration-ui)

from app import calibration  # noqa: E402

PROFILE = {"chair_type": "custom", "chair_width_cm": 95, "armrest_height_cm": 80, "phone_model": "test phone",
           "junk": "dropped"}


def test_clean_profile_drops_junk_and_bad_values():
    p = calibration.clean_profile({**PROFILE, "hfov_deg": 500, "calibration": {"height_m": 9}})
    assert p == {"chair_type": "custom", "chair_width_cm": 95.0, "armrest_height_cm": 80.0, "phone_model": "test phone"}
    assert calibration.clean_profile({"chair_type": "power"})["chair_width_cm"] == 70.0
    assert calibration.clean_profile(None) == {}
    assert calibration.parse_json("not json") is None and calibration.parse_json("[1]") is None


def test_sensor_pose_portrait_landscape_and_too_few_samples():
    assert calibration.sensor_pose({"beta": 70, "gamma": 2, "angle": 0, "n": 30}) == \
        {"pitch_deg": 20.0, "roll_deg": 2.0, "samples": 30}
    assert calibration.sensor_pose({"beta": 3, "gamma": -72, "angle": 90, "n": 30})["pitch_deg"] == 18.0
    assert calibration.sensor_pose({"beta": 70, "gamma": 2, "n": 2}) is None
    assert calibration.sensor_pose(None) is None


def test_camera_pose_sources():
    size = (1280, 720)
    d = calibration.camera_pose({}, None, {}, size)
    assert d["source"] == "default" and d["height_m"] == 0.75 and d["pitch_deg"] is None
    lo, hi = d["hfov_range_deg"]
    assert lo < d["hfov_deg"] < hi and 60 < d["hfov_deg"] < 75   # 26 mm equivalent on the long side
    portrait = calibration.camera_pose({}, None, {}, (720, 1280))
    assert portrait["hfov_deg"] < d["hfov_deg"]
    p = calibration.camera_pose(calibration.clean_profile(PROFILE), {"beta": 70, "gamma": 0, "n": 9}, {}, size)
    assert p["height_m"] == 0.8 and p["pitch_deg"] == 20.0 and p["pitch_sd_deg"] == calibration.SENSOR_PITCH_SD_DEG
    meta = calibration.camera_pose({}, None, {"focal_35mm": 24.0}, size)
    assert meta["sources"]["hfov"] == "clip metadata" and meta["hfov_deg"] > d["hfov_deg"]
    cal = calibration.clean_profile({"calibration": {"height_m": 0.71, "pitch_bias_deg": 1.5, "hfov_deg": 65}})
    c = calibration.camera_pose(cal, {"beta": 70, "gamma": 0, "n": 9}, {}, size)
    assert c["source"] == "calibrated" and c["height_m"] == 0.71 and c["pitch_deg"] == 21.5
    assert c["pitch_sd_deg"] == calibration.CALIBRATED_PITCH_SD_DEG and c["hfov_deg"] == 65


def test_solve_pose_recovers_the_synthetic_camera(scene):
    from app import geometry
    for corners in (scene["ref_corners_true"], geometry.find_reference_corners(scene["ref_mask"])):
        s = calibration.solve_pose(corners, "a4x4", scene["frame_size"], 65.0)
        assert abs(s["height_m"] - 0.75) < 0.03, s
        assert abs(s["pitch_deg"] - scene["pitch_deg"]) < 1.0, s


def test_reference_mode_uses_profile_chair_width(env, scene, clip_src):
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True, profile=PROFILE))
    a = r["assumptions"]
    assert a["mode"] == "reference" and a["chair_width_cm"] == 95 and a["chair_source"] == "profile"
    assert r["verdict"] in {"tight", "blocked"}, r  # 90 cm gap is "passes" for the default 70 cm chair
    assert "95 cm" in a["main"]


def test_calibrate_replay_solves_height_and_bias(env, scene, clip_src):
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    sensor = {"beta": 90 - (scene["pitch_deg"] - 2.0), "gamma": 0, "angle": 0, "n": 20}
    r = asyncio.run(calibration.calibrate(clip_src, "a4x4", {"hfov_deg": 65}, sensor, replay=True))
    assert r["ok"], r
    c = r["calibration"]
    assert abs(c["height_m"] - 0.75) < 0.03 and abs(c["pitch_bias_deg"] - 2.0) < 1.0
    assert r["hfov_source"] == "profile" and not (env / "ledger.jsonl").exists()
    # the stored result goes back in as the profile: next check is "calibrated"
    pose = calibration.camera_pose(calibration.clean_profile({"calibration": c}), sensor, {}, scene["frame_size"])
    assert pose["source"] == "calibrated" and abs(pose["pitch_deg"] - scene["pitch_deg"]) < 1.0


def test_calibrate_without_reference_says_why(env, clip_src):
    r = asyncio.run(calibration.calibrate(clip_src, "tactile_block", None, None, replay=True))
    assert r["ok"] is False and "not found" in r["why"]


def test_profile_routes(env, scene, clip_src):
    from fastapi.testclient import TestClient
    from app.main import app
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    c = TestClient(app)
    with clip_src.open("rb") as f:
        r = c.post("/api/analyze", files={"video": ("clip.mp4", f, "video/mp4")},
                   data={"lat": "37.484", "lon": "127.034", "replay": "true", "profile": json.dumps(PROFILE),
                         "sensor": json.dumps({"beta": 70, "gamma": 1, "angle": 0, "n": 12})})
    assert r.status_code == 200, r.text
    assert r.json()["assumptions"]["chair_width_cm"] == 95
    with clip_src.open("rb") as f:
        k = c.post("/api/calibrate", files={"video": ("ref.mp4", f, "video/mp4")},
                   data={"ref_kind": "a4x4", "replay": "true", "profile": json.dumps({"hfov_deg": 65})})
    assert k.status_code == 200 and k.json()["ok"], k.text
    assert not list((env / "analyze" / "uploads").iterdir())
    with clip_src.open("rb") as f:
        bad = c.post("/api/calibrate", files={"video": ("ref.mp4", f, "video/mp4")}, data={"ref_kind": "coin"})
    assert bad.status_code == 422


def test_unknown_cause_comes_from_geometry():
    m = {"verdict": "unknown", "gap_cm": 33.5, "band_cm": [15.8, 125.9], "detail": {"dominant_error": "tilt"}}
    c = analyze.unknown_cause(m)
    assert "tilt" in c["reason"] and "16" in c["reason"] and "126" in c["reason"]
    assert "motion sensor" in c["next_step"] and "tilt" in c["keywords"]
    text = analyze.cause_text(c)
    assert c["reason"] in text and c["next_step"] in text
    assert analyze.unknown_cause({**m, "verdict": "tight"}) is None
    assert analyze.unknown_cause({"verdict": "unknown", "gap_cm": None, "band_cm": None, "detail": {}}) is None
    assert analyze.unknown_cause({**m, "detail": {"dominant_error": "reference corners"}}) is None


def test_unknown_explanation_must_name_the_measured_cause(env, scene, clip_src, monkeypatch):
    """A model sentence that blames something geometry did not measure ("unclear view") is replaced."""
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])

    async def wrong_cause(measurement, replay):
        return {"text": "We can't tell because the view was too unclear to measure.", "model": "muse-spark-1.3",
                "warnings": []}
    monkeypatch.setattr(analyze, "_explain", wrong_cause)
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True, concepts_hint=["bollard"]))
    cause = analyze.unknown_cause({"verdict": r["verdict"], "band_cm": r["band_cm"], "detail": r["measurement_detail"]})
    if cause is None:
        pytest.skip(f"this synthetic scene did not end as a wide-band unknown ({r['verdict']})")
    assert "unclear" not in r["explanation"]
    assert any(w in r["explanation"].lower() for w in cause["keywords"])
    assert r["models"]["explain"] == "template"


def test_sample_without_dev_clips_is_the_recorded_live_run(env):
    """A deploy has no dev clips: the sample is the recorded live run, with its blurred clip on the server."""
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    j = c.get("/api/sample", params={"lat": 37.484, "lon": 127.034}).json()
    assert j["recorded"]["note"].startswith("Recorded live run") and j["location"] == {"lat": 37.484, "lon": 127.034}
    assert j["models"]["triage"] == "muse-spark-1.3" and j["models"]["sam"] == "sam-3.1"
    assert c.get(j["video_url"]).status_code == 200


def test_live_checks_need_the_demo_token_when_set(env, clip_src, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    monkeypatch.setenv("BL_DEMO_TOKEN", "judge-secret")
    assert c.get("/api/health").json()["live_needs_token"] is True
    files = {"video": ("c.mp4", clip_src.read_bytes(), "video/mp4")}
    r = c.post("/api/analyze", files=files, data={"lat": "37.48", "lon": "127.03"})
    assert r.status_code == 401 and "judge token" in r.json()["detail"]
    r = c.post("/api/analyze", files=files, data={"lat": "37.48", "lon": "127.03"}, headers={"X-Demo-Token": "nope"})
    assert r.status_code == 401
    r = c.post("/api/calibrate", files=files)
    assert r.status_code == 401
    # replay uploads also run ffmpeg and the blur on the server, so they need the token too; the sample stays open
    r = c.post("/api/analyze", files=files, data={"lat": "37.48", "lon": "127.03", "replay": "true"})
    assert r.status_code == 401
    assert c.get("/api/sample").status_code == 200


def test_blur_tiles_cover_the_frame_and_bound_the_detector_input():
    for w, h in [(1280, 720), (2560, 1440), (720, 1280), (300, 200)]:
        t = blur.tiles(w, h, 1_000_000, 96)
        assert max(tw * th for _, _, tw, th in t) <= 1_000_000 * 1.2
        cover = np.zeros((h, w), bool)
        for x, y, tw, th in t:
            cover[y:y + th, x:x + tw] = True
        assert cover.all()


def test_blur_dedupe_keeps_one_box_per_face():
    face, same_face_from_a_tile, other = (100, 100, 140, 150), (102, 101, 139, 149), (400, 100, 430, 140)
    assert sorted(blur.dedupe([face, same_face_from_a_tile, other])) == sorted([face, other])


def test_spark_retry_never_sends_an_empty_assistant_turn(monkeypatch):
    """Meta answers an empty assistant turn with 404 model_not_found; a cut-off empty reply must be retried without
    it, and with a larger token limit."""
    assert spark._retry_turns("", "again") == [{"role": "user", "content": "again"}]
    assert spark._retry_turns("{}", "again")[0] == {"role": "assistant", "content": "{}"}
    seen = []

    async def fake_chat(b, messages, schema, effort, max_tokens=0):
        seen.append((messages, max_tokens))
        if len(seen) == 1:
            return {"content": "", "finish": "length", "model": "m", "latency_s": 0.1, "usage": {}}
        return {"content": "Your wheelchair should fit through here.", "finish": "stop", "model": "m",
                "latency_s": 0.1, "usage": {}}
    monkeypatch.setattr(spark, "_chat", fake_chat)
    monkeypatch.setattr(spark, "_backend", lambda: spark.Backend("meta", "u", "m", "k", "max_completion_tokens"))
    monkeypatch.setattr(spark, "_cache_put", lambda *a: None)
    out = asyncio.run(spark.explain({"verdict": "passes", "gap_cm": 95.0, "band_cm": [90.0, 99.0]}))
    assert out["text"].startswith("Your wheelchair") and len(seen) == 2
    retry_msgs, retry_tokens = seen[1]
    assert all(m.get("content") for m in retry_msgs if m["role"] == "assistant")
    assert retry_tokens == spark.RETRY_MAX_TOKENS


def test_models_may_only_make_a_verdict_more_cautious():
    passes = {"verdict": "passes", "gap_cm": 162.0, "band_cm": [140.0, 180.0], "why": "narrowest point 162 cm",
              "detail": {"dominant_error": "tilt"}}
    closed = analyze.apply_path_and_mode(passes, {"path": {"state": "closed", "evidence": "a SIDEWALK CLOSED sign"}}, "sensor")
    assert closed["verdict"] == "blocked" and closed["gap_cm"] is None and closed["band_cm"] is None
    assert "SIDEWALK CLOSED" in closed["why"] and closed["detail"]["geometry"]["gap_cm"] == 162.0
    capped = analyze.apply_path_and_mode(passes, {"path": {"state": "open", "evidence": "clear"}}, "default")
    assert capped["verdict"] == "unknown" and capped["detail"]["capped"] == "default_pass"
    cause = analyze.unknown_cause(capped)
    assert "default assumptions" in cause["reason"] and "tilt" in cause["next_step"].lower()
    # a confirmed scale (tilt sensor, calibration or a reference) keeps the pass; "narrowed" changes nothing
    assert analyze.apply_path_and_mode(passes, {"path": {"state": "narrowed", "evidence": "x"}}, "sensor") is passes
    assert analyze.apply_path_and_mode(passes, None, "reference") is passes


def test_triage_needs_a_path_state_with_evidence_for_closed():
    base = {"checks": {k: True for k in spark.CHECKS}, "reasons": ["ok"],
            "barriers": [{"concept": "sign", "frames": [0], "evidence": "a closure sign"}],
            "reference": {"visible": False, "frames": [], "evidence": ""}}
    _, errors, _ = spark._validate_triage(base, 6)
    assert any("path" in e for e in errors)
    _, errors, _ = spark._validate_triage({**base, "path": {"state": "closed", "frames": [0], "evidence": ""}}, 6)
    assert any("closed" in e for e in errors)
    clean, errors, _ = spark._validate_triage({**base, "path": {"state": "closed", "frames": [0, 2],
                                                                "evidence": "SIDEWALK CLOSED sign across it"}}, 6)
    assert not errors and clean["path"]["state"] == "closed"


def test_measured_gap_carries_its_two_image_points(env, scene, clip_src):
    """The ruler drawn on the clip comes from geometry's own two ground points, not from the screen."""
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    r = asyncio.run(analyze.analyze(clip_src, 37.484, 127.034, replay=True))
    ends = r["measurement_detail"]["gap_px"]
    w, h = scene["frame_size"]
    assert len(ends) == 2 and all(0 <= x <= w and 0 <= y <= h for x, y in ends)
    xs = sorted(x for x, _ in ends)
    # the two ends sit on different bollards: each end is inside (or next to) one barrier mask's columns
    cols = [np.where(m.any(axis=0))[0] for m in scene["barrier_masks"]]
    owners = {k for x in xs for k, c in enumerate(cols) if c.min() - 3 <= x <= c.max() + 3}
    assert len(owners) == 2, (xs, [(c.min(), c.max()) for c in cols])


def test_named_recorded_samples(env):
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    j = c.get("/api/sample", params={"name": "closed"}).json()
    assert j["verdict"] == "blocked" and j["triage"]["path"]["state"] == "closed"
    assert j["measurement_detail"]["override"] == "path_closed" and c.get(j["video_url"]).status_code == 200
    s = c.get("/api/sample", params={"name": "scooters"}).json()  # filmed facing a parked row: nothing straight ahead
    assert s["verdict"] == "unknown" and s["gap_cm"] is None and s["recorded"]["name"] == "scooters"
    k = c.get("/api/sample", params={"name": "known"}).json()     # SANPO: camera height and tilt known
    assert k["verdict"] == "passes" and k["assumptions"]["mode"] == "calibrated"
    assert len(k["measurement_detail"]["gap_px"]) == 2 and k["band_cm"][0] >= 75
    assert c.get("/api/sample", params={"name": "nope"}).status_code == 404


def test_stream_reports_each_stage_then_the_result(env, scene, clip_src, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    _write_sam_fixture(env / "fixtures", "bollard", scene["barrier_masks"], scene["frame_size"])
    _write_sam_fixture(env / "fixtures", "paper", [scene["ref_mask"]], scene["frame_size"])
    asyncio.run(_seed_triage(clip_src))
    monkeypatch.setenv("BL_DEMO_TOKEN", "judge-secret")
    c = TestClient(app)
    files = {"video": ("c.mp4", clip_src.read_bytes(), "video/mp4")}
    assert c.post("/api/analyze/stream", files=files, data={"lat": "37.48", "lon": "127.03"}).status_code == 401
    r = c.post("/api/analyze/stream", files=files, data={"lat": "37.48", "lon": "127.03"},
               headers={"X-Demo-Token": "judge-secret"})
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    stages = [(e["stage"], e["status"]) for e in events if "stage" in e]
    assert stages == [(s, st) for s in ("blur", "triage", "sam", "measure", "explain") for st in ("start", "done")]
    assert events[-1]["result"]["verdict"] == "passes"
    assert next(e for e in events if e.get("stage") == "sam" and e["status"] == "done")["outlined"]["bollard"] > 0


def test_closed_path_with_no_barriers_is_blocked(env, clip_src):
    """A path Muse Spark saw closed comes back blocked, not unknown, when triage names no barriers."""
    async def seed():
        clip, _, _ = await analyze.prepare(clip_src)
        jpgs, _ = analyze.triage_frames(clip, analyze.clip_info(clip)["frames"])
        h = hashlib.sha256()
        for j in jpgs:
            h.update(hashlib.sha256(j).digest())
        spark._cache_put("triage", h.hexdigest(), {
            "checks": {"ground_visible": True, "barrier_visible": False, "sharp": True, "seated_height": True},
            "reasons": ["the sidewalk is closed"], "barriers": [],
            "reference": {"visible": False, "frames": [], "evidence": ""},
            "path": {"state": "closed", "frames": [0], "evidence": "SIDEWALK CLOSED sign across it"},
            "model": "test-fixture", "warnings": []})
    asyncio.run(seed())
    r = asyncio.run(analyze.analyze(clip_src, None, None, replay=True))
    assert r["verdict"] == "blocked" and "closed" in r["why"].lower()


def test_spark_ledger_prices_token_usage(env, monkeypatch):
    """A ledgered triage call prices its tokens, and the spend cap then counts it."""
    monkeypatch.setattr(spark, "_backend",
                        lambda: spark.Backend("meta", "u", "muse-spark-1.3", "k", "max_completion_tokens"))

    async def fake_triage(jpgs, meta):
        return {"model": "muse-spark-1.3", "barriers": [],
                "call": {"usage": {"prompt_tokens": 1000, "completion_tokens": 2000, "reasoning_tokens": 500}}}
    monkeypatch.setattr(spark, "triage", fake_triage)
    asyncio.run(analyze._triage([], {}, False))
    entry = json.loads((env / "ledger.jsonl").read_text().strip())
    assert entry["usd"] == pytest.approx(1000 / 1e6 * 1.25 + 2000 / 1e6 * 4.25)
    assert analyze.spent_usd() == pytest.approx(entry["usd"])
    monkeypatch.setenv("BL_SPEND_CAP_USD", "0.009")
    with pytest.raises(analyze.SpendCapError):
        analyze._check_cap(0.0, "anything")


def test_chat_malformed_200_is_a_spark_error(monkeypatch):
    """A 200 with no usable body raises SparkError, not the KeyError the API maps to 500."""
    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(spark.httpx, "AsyncClient", _Client)
    b = spark.Backend("meta", "u", "muse-spark-1.3", "k", "max_completion_tokens")
    with pytest.raises(spark.SparkError, match="malformed"):
        asyncio.run(spark._chat(b, [], None, "low"))
