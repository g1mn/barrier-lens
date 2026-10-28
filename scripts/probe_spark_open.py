"""Probe Muse Spark Open on Baseten: latency, token usage, and whether an image is accepted.

usage: python scripts/probe_spark_open.py [image.jpg]
Prints timings and usage only; never prints the key.
"""
import base64
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import BASETEN_BASE_URL, MUSE_SPARK_OPEN, key  # noqa: E402


def main() -> None:
    api_key = key("BASETEN_API_KEY")
    if not api_key:
        sys.exit("BASETEN_API_KEY missing")
    content: list[dict] = [{"type": "text", "text": (
        "You are checking a sidewalk photo for a wheelchair user. In one JSON object reply with "
        '{"usable": bool, "barriers": [str], "why": str}. Do not estimate distances.')}]
    if len(sys.argv) > 1:
        b64 = base64.b64encode(Path(sys.argv[1]).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    body = {"model": MUSE_SPARK_OPEN, "max_tokens": 2000, "messages": [{"role": "user", "content": content}]}
    t0 = time.monotonic()
    r = httpx.post(f"{BASETEN_BASE_URL}/chat/completions", json=body,
                   headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
    dt = time.monotonic() - t0
    print("status", r.status_code, f"{dt:.2f}s")
    if r.status_code != 200:
        print(r.text[:500])
        return
    j = r.json()
    msg = j["choices"][0]["message"]
    print("usage", json.dumps(j.get("usage")))
    print("finish", j["choices"][0].get("finish_reason"))
    print("content", (msg.get("content") or "")[:600])


if __name__ == "__main__":
    main()
