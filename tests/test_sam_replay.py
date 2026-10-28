"""Offline tests for app/sam.py: replay fixtures, mask decoding, wire parsing, and the streamed client against a mock.

No network: the live-path test uses httpx.MockTransport and a fake key that never leaves the process.
"""
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import sam  # noqa: E402

# Complete wire line from https://dev.meta.ai/docs/sam/reading-segmentation ("Video output grammar"), lossless masks.
DOC_LINE = ("<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|><|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/#Xd8%dX9x(|>,"
            "1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|><|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`=o3?X{emCgO$y@|>")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point the cache at a temp dir so tests never read or write data/cache/sam."""
    monkeypatch.setattr(sam, "CACHE_DIR", tmp_path / "cache")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really a video, replay never opens it")
    return clip


def test_replay_returns_fixture_masks(isolated, monkeypatch):
    monkeypatch.setenv("SAM_MODE", "replay")
    out = asyncio.run(sam.segment_video(str(isolated), "  Bollard. "))
    assert out["source"] == "replay" and out["replay_fixture"] == "bollard-tiny.json"
    assert (out["width"], out["height"], out["fps"]) == (64, 48, 10.0)
    assert [f["i"] for f in out["frames"]] == [0, 1, 2]
    masks = sam.masks_for_frame(out, 0)
    assert set(masks) == {1, 2}
    left, right = masks[1], masks[2]
    assert left.shape == (48, 64) and left.dtype == bool
    assert int(left.sum()) == 9 * 33 and int(right.sum()) == 9 * 33
    cols_left, cols_right = np.flatnonzero(left.any(0)), np.flatnonzero(right.any(0))
    assert cols_right.min() - cols_left.max() - 1 == 21  # the gap the fixture note promises
    # box agrees with the mask
    x0, y0, x1, y1 = out["frames"][0]["objects"][0]["box"]
    ys, xs = np.nonzero(left)
    assert (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1) == (x0, y0, x1, y1)


def test_replay_unknown_concept_is_zero_matches(isolated, monkeypatch):
    monkeypatch.setenv("SAM_MODE", "replay")
    out = asyncio.run(sam.segment_video(str(isolated), "construction fence"))
    assert out["source"] == "replay" and out["frames"] == [] and out["replay_fixture"] is None


def test_auto_mode_without_key_replays(isolated, monkeypatch):
    monkeypatch.delenv("SAM_MODE", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    out = asyncio.run(sam.segment_video(str(isolated), "bollard"))
    assert out["source"] == "replay"


def test_live_mode_without_key_is_a_clear_error(isolated, monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    with pytest.raises(sam.SamError, match="META_API_KEY"):
        asyncio.run(sam.segment_video(str(isolated), "bollard", mode="live"))


@pytest.mark.parametrize("bad", ["bus, bicycle", "bus; bicycle", "bus + bicycle", "", "   "])
def test_concept_lists_are_rejected(bad):
    with pytest.raises(ValueError):
        sam.normalize_concept(bad)


def test_rle_roundtrip_random_mask():
    rng = np.random.default_rng(0)
    for shape in [(1, 1), (5, 7), (48, 64)]:
        m = rng.random(shape) > 0.6
        rle = sam.encode_rle(m)
        assert sum(rle["counts"]) == m.size
        assert np.array_equal(sam.decode_mask({"mask_rle": rle}), m)
    full = np.ones((3, 4), dtype=bool)
    assert sam.encode_rle(full)["counts"][0] == 0  # counts always start with a 0-run


def test_polygon_decode_uses_pixel_centres():
    m = sam.decode_mask({"polygon": [[2, 2], [6, 2], [6, 6], [2, 6]]}, frame_size=(10, 8))
    assert m.shape == (8, 10)
    assert int(m.sum()) == 16 and m[2:6, 2:6].all()
    with pytest.raises(ValueError):
        sam.decode_mask({"polygon": [[0, 0], [1, 0], [1, 1]]})


def test_parse_and_decode_documented_wire_line():
    frames, size, problems = sam.parse_output(DOC_LINE + "\n")
    assert problems == [] and size == (320, 334)
    assert [o["id"] for o in frames[0]["objects"]] == [0, 1]
    assert frames[0]["objects"][0]["box"] == [211, 228, 271, 255]
    resized = asyncio.run(sam._decode_frames(frames, 320, 334))
    assert resized == 0  # the mask raster is exactly box-sized
    m0 = sam.decode_mask(frames[0]["objects"][0])
    assert m0.shape == (334, 320) and int(m0.sum()) == 1539
    ys, xs = np.nonzero(m0)
    assert xs.min() >= 211 and xs.max() <= 270 and ys.min() >= 228 and ys.max() <= 254
    assert int(sam.decode_mask(frames[0]["objects"][1]).sum()) == 1236


def test_parse_reports_garbage_and_confidence():
    line = DOC_LINE.replace("h=334|><|mask;x=0;y=0;", "h=334;c=0.75|><|mask;x=0;y=0;c=0.75;", 1)
    frames, _, problems = sam.parse_output(line + "\nhello\n<3f>garbage")
    assert frames[0]["objects"][0]["score"] == 0.75 and frames[0]["objects"][1]["score"] is None
    assert len(problems) == 2


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_live_path_against_mock_api(tmp_path, monkeypatch):
    """Upload + streamed response + usage, with a 429 first to exercise Retry-After; second call is a cache hit."""
    monkeypatch.setattr(sam, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("META_API_KEY", "LLM_0_fake-test-key")
    clip = tmp_path / "test.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x334:rate=10",
                    "-t", "1", "-pix_fmt", "yuv420p", str(clip)], check=True)
    calls = {"files": 0, "responses": 0}
    lane = {"item_id": "msg_1", "output_index": 0, "content_index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer LLM_0_fake-test-key"
        if request.url.path == "/v1/files":
            calls["files"] += 1
            assert b'name="purpose"' in request.content and b"user_data" in request.content
            return httpx.Response(200, json={"id": "file-123", "object": "file", "status": "uploaded"})
        assert request.url.path == "/v1/responses"
        calls["responses"] += 1
        if calls["responses"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"},
                                  json={"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded",
                                                  "message": "slow down", "param": None}})
        body = json.loads(request.content)
        content = body["input"][0]["content"]
        assert content[0] == {"type": "input_text", "text": "scooter"}
        assert content[1] == {"type": "input_video", "file_id": "file-123"}
        assert body["model"] == "sam-3.1" and body["stream"] is True
        assert body["metadata"] == {"mask_encoding": "one_bit", "include_confidence": "true"}
        stream = _sse({"type": "response.created", "response": {"id": "resp_1"}},
                      {"type": "response.output_text.delta", "delta": DOC_LINE + "\n", **lane},
                      {"type": "response.output_text.done", "text": DOC_LINE + "\n", **lane},
                      {"type": "response.completed",
                       "response": {"id": "resp_1", "status": "completed",
                                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                                              "video_frames_processed": 50}}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await sam.segment_video(str(clip), "Scooter", mode="live", client=client)
            second = await sam.segment_video(str(clip), "scooter", mode="live", client=client)
        return first, second

    first, second = asyncio.run(run())
    assert calls == {"files": 1, "responses": 2}  # one 429 + one success, then the cache answers
    assert first["source"] == "meta-api" and first["cache"] == "miss" and second["cache"] == "hit"
    assert first["outcome"] == "completed" and first["response_id"] == "resp_1"
    assert first["frames_billed"] == 50 and first["cost_usd"] == pytest.approx(0.01)
    assert first["cost_basis"] == "usage.video_frames_processed"
    assert (first["width"], first["height"], first["fps"], first["frame_count"]) == (320, 334, 10.0, 10)
    assert first["clip"]["prepped"] is True
    assert int(sam.decode_mask(first["frames"][0]["objects"][0]).sum()) == 1539
    assert second["frames"] == first["frames"]
