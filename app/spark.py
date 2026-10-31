"""Muse Spark: clip triage and plain-language explanation.

Muse Spark proposes and explains; it never measures. Distances it writes are rejected, and
numbers in an explanation must come from the measurement it was given.

Backends, picked in this order unless SPARK_BACKEND=meta|baseten|replay forces one:
  meta     Muse Spark on the Meta Model API, when META_API_KEY is set.
           POST https://api.meta.ai/v1/chat/completions, model muse-spark-1.3,
           response_format json_schema, reasoning_effort, max_completion_tokens
           (https://dev.meta.ai/docs/api-reference/chat-completions/schemas,
            https://dev.meta.ai/docs/structured-output,
            https://dev.meta.ai/docs/image-understanding, read 2026-10-28).
  baseten  Muse Spark Open on Baseten (OpenAI-compatible), when BASETEN_API_KEY is set.
           Accepts the same json_schema and reasoning_effort fields (checked 2026-10-28).
  replay   Earlier results cached under data/cache/spark/, keyed by the input. Used when no key is set.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import BASETEN_BASE_URL, MUSE_SPARK_OPEN, key

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = Path(__file__).resolve().parent / "prompts"
CACHE_DIR = ROOT / "data" / "cache" / "spark"

META_BASE_URL = "https://api.meta.ai/v1"
META_SPARK_MODEL = "muse-spark-1.3"

TRIAGE_EFFORT = "low"
EXPLAIN_EFFORT = "low"
MAX_TOKENS = 4000  # reasoning counts against this; Spark Open needs >= 2000
RETRY_MAX_TOKENS = 8000  # a retry after a cut-off or empty reply gets more room
TIMEOUT_S = 180


class SparkError(RuntimeError):
    """Muse Spark could not give a valid answer (no key and no replay, HTTP error, or invalid output twice)."""


REF_KINDS = ("paper", "tactile paving", "none")


def ref_kind(ref: dict) -> str:
    """Which scale reference triage saw. Replies and recordings made before the "kind" field are read from the
    evidence text, where a strip is always called tactile."""
    if ref.get("kind") in REF_KINDS:
        return ref["kind"]
    if not ref.get("visible"):
        return "none"
    text = str(ref.get("evidence", "")).lower()
    return "tactile paving" if "tactile" in text else "paper"


# ---------------------------------------------------------------- backends

@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    model: str
    api_key: str | None
    max_tokens_field: str


def _backend() -> Backend:
    forced = (key("SPARK_BACKEND") or "").lower()
    meta_key, baseten_key = key("META_API_KEY"), key("BASETEN_API_KEY")
    meta = Backend("meta", f"{META_BASE_URL}/chat/completions", META_SPARK_MODEL, meta_key, "max_completion_tokens")
    baseten = Backend("baseten", f"{BASETEN_BASE_URL}/chat/completions", MUSE_SPARK_OPEN, baseten_key, "max_tokens")
    replay = Backend("replay", "", "replay", None, "")
    if forced == "meta":
        if not meta_key:
            raise SparkError("SPARK_BACKEND=meta but META_API_KEY is not set")
        return meta
    if forced == "baseten":
        if not baseten_key:
            raise SparkError("SPARK_BACKEND=baseten but BASETEN_API_KEY is not set")
        return baseten
    if forced == "replay":
        return replay
    if meta_key:
        return meta
    if baseten_key:
        return baseten
    return replay


def _retry_turns(content: str, note: str) -> list[dict]:
    """Follow-up turns after a bad reply. An empty assistant turn is never sent: the Meta Model API answers it with
    404 model_not_found (reproduced 2026-10-28), which reads like a missing model. An empty reply happens when
    reasoning uses up the token limit, so the retry also gets a larger limit (see RETRY_MAX_TOKENS)."""
    return ([{"role": "assistant", "content": content}] if content.strip() else []) + [{"role": "user", "content": note}]


async def _chat(b: Backend, messages: list[dict], schema: dict | None, effort: str,
                max_tokens: int = 0) -> dict:
    """One chat completion. Returns {"content", "model", "usage", "latency_s", "finish"}."""
    body: dict = {"model": b.model, "messages": messages, b.max_tokens_field: max_tokens or MAX_TOKENS,
                  "reasoning_effort": effort}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema["title"], "schema": schema["schema"], "strict": True}}
    headers = {"Authorization": f"Bearer {b.api_key}", "Content-Type": "application/json"}
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for attempt in range(2):
            try:
                r = await client.post(b.url, json=body, headers=headers)
            except httpx.TransportError as e:
                if attempt:
                    raise SparkError(f"{b.name}: {type(e).__name__}") from None
                await asyncio.sleep(2)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and not attempt:
                await asyncio.sleep(2)
                continue
            break
    latency = time.monotonic() - t0
    if r.status_code != 200:
        raise SparkError(f"{b.name}: HTTP {r.status_code}: {r.text[:300]}")
    try:
        j = r.json()
        choice = j["choices"][0]
        u = j.get("usage") or {}
        content = choice["message"].get("content") or ""
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise SparkError(f"{b.name}: malformed response {r.text[:100]!r}") from e
    return {
        "content": content,
        "finish": choice.get("finish_reason"),
        "model": j.get("model") or b.model,
        "latency_s": round(latency, 2),
        "usage": {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                  "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens")},
    }


def _sum_usage(calls: list[dict]) -> dict:
    total: dict = {}
    for c in calls:
        for k, v in c["usage"].items():
            if v is not None:
                total[k] = total.get(k, 0) + v
    return total


def _cache_path(task: str, digest: str) -> Path:
    return CACHE_DIR / f"{task}-{digest[:16]}.json"


def _cache_get(task: str, digest: str) -> dict:
    p = _cache_path(task, digest)
    if not p.exists():
        raise SparkError("no Muse Spark key is set and there is no cached result for this input (replay miss)")
    out = json.loads(p.read_text())
    out["model"] = f"{out['model']} (replay)"
    return out


def _cache_put(task: str, digest: str, result: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(task, digest).write_text(json.dumps(result, ensure_ascii=False, indent=1))


def _parse_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    i, k = text.find("{"), text.rfind("}")
    if i >= 0 and k > i:
        try:
            return json.loads(text[i:k + 1])
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- distance guard

_NUM = (r"(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"half(?: a)?|a few|a couple of|several|a single)")
_UNIT = (r"(?:cm|mm|km|m|meters?|metres?|centimet(?:er|re)s?|millimet(?:er|re)s?|inch(?:es)?|ft|feet|foot|"
         r"yards?|percent|%|센티미터|센티|미터)")
DISTANCE_RE = re.compile(rf"(?<![\w.]){_NUM}(?:\s*(?:-|–|to)\s*\d+(?:[.,]\d+)?)?\s*{_UNIT}(?![a-z])", re.I)


def _distances(s: str) -> list[str]:
    return [m.group(0) for m in DISTANCE_RE.finditer(s)]


# ---------------------------------------------------------------- triage

# open: nothing in the way; narrowed: something takes part of the width; closed: a sign, fence or barrier closes
# the sidewalk, or objects leave no way through. "closed" makes the verdict blocked (models may only make it
# more cautious, never more hopeful); see analyze.apply_path_and_mode.
PATH_STATES = ("open", "narrowed", "closed")
TRIAGE_SCHEMA = {
    "title": "barrier_triage",
    "schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "object",
                "properties": {k: {"type": "boolean"} for k in
                               ("ground_visible", "barrier_visible", "sharp", "seated_height")},
                "required": ["ground_visible", "barrier_visible", "sharp", "seated_height"],
                "additionalProperties": False,
            },
            "reasons": {"type": "array", "items": {"type": "string"}},
            "barriers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "concept": {"type": "string"},
                        "frames": {"type": "array", "items": {"type": "integer"}},
                        "evidence": {"type": "string"},
                    },
                    "required": ["concept", "frames", "evidence"],
                    "additionalProperties": False,
                },
            },
            "reference": {
                "type": "object",
                "properties": {
                    "visible": {"type": "boolean"},
                    "kind": {"type": "string", "enum": list(REF_KINDS)},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                    "evidence": {"type": "string"},
                },
                "required": ["visible", "kind", "frames", "evidence"],
                "additionalProperties": False,
            },
            "path": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": list(PATH_STATES)},
                    "frames": {"type": "array", "items": {"type": "integer"}},
                    "evidence": {"type": "string"},
                },
                "required": ["state", "frames", "evidence"],
                "additionalProperties": False,
            },
        },
        "required": ["checks", "reasons", "barriers", "reference", "path"],
        "additionalProperties": False,
    },
}
CHECKS = ("ground_visible", "barrier_visible", "sharp", "seated_height")
_CONCEPT_RE = re.compile(r"^[a-z][a-z-]*(?: [a-z][a-z-]*){0,2}$")


def _frame_list(v, n: int, where: str, errors: list[str]) -> list[int]:
    if not isinstance(v, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in v):
        errors.append(f"{where}.frames must be a list of integers")
        return []
    bad = [i for i in v if not 0 <= i < n]
    if bad:
        errors.append(f"{where}.frames has {bad}; frames are numbered 0 to {n - 1}")
    return sorted({i for i in v if 0 <= i < n})


def _validate_triage(obj, n: int) -> tuple[dict, list[str], list[str]]:
    """Returns (clean, structural_errors, distance_errors)."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return {}, ["reply is not a JSON object"], []
    checks = obj.get("checks")
    if not isinstance(checks, dict) or not all(isinstance(checks.get(k), bool) for k in CHECKS):
        errors.append(f"checks must have boolean {', '.join(CHECKS)}")
        checks = {}
    reasons = obj.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) and r.strip() for r in reasons):
        errors.append("reasons must be a list of non-empty strings")
        reasons = []
    merged: dict[str, dict] = {}
    raw = obj.get("barriers")
    if not isinstance(raw, list):
        errors.append("barriers must be a list")
        raw = []
    for i, b in enumerate(raw):
        if not isinstance(b, dict):
            errors.append(f"barriers[{i}] must be an object")
            continue
        concept = " ".join(str(b.get("concept", "")).lower().split())
        if not _CONCEPT_RE.match(concept):
            errors.append(f"barriers[{i}].concept {concept!r} must be one to three plain lower-case words")
            continue
        frames = _frame_list(b.get("frames"), n, f"barriers[{i}]", errors)
        if not frames:
            errors.append(f"barriers[{i}] needs at least one frame where it is visible")
        evidence = str(b.get("evidence", "")).strip()
        if not evidence:
            errors.append(f"barriers[{i}].evidence is empty")
        m = merged.setdefault(concept, {"concept": concept, "frames": [], "evidence": ""})
        m["frames"] = sorted(set(m["frames"]) | set(frames))
        m["evidence"] = "; ".join(e for e in (m["evidence"], evidence) if e)
    ref = obj.get("reference")
    if not isinstance(ref, dict) or not isinstance(ref.get("visible"), bool):
        errors.append("reference must be an object with boolean visible")
        ref = {"visible": False, "frames": [], "evidence": ""}
    ref = {"visible": ref["visible"], "kind": ref_kind(ref), "frames": _frame_list(ref.get("frames", []), n, "reference", errors),
           "evidence": str(ref.get("evidence", "")).strip()}
    path = obj.get("path")
    if not isinstance(path, dict) or path.get("state") not in PATH_STATES:
        errors.append(f"path must be an object with state one of {', '.join(PATH_STATES)}")
        path = {"state": None, "frames": [], "evidence": ""}
    path = {"state": path["state"], "frames": _frame_list(path.get("frames", []), n, "path", errors),
            "evidence": str(path.get("evidence", "")).strip()}
    if path["state"] == "closed" and not path["evidence"]:
        errors.append("path.evidence must say what shows the sidewalk is closed")

    clean = {"checks": {k: bool(checks.get(k)) for k in CHECKS}, "reasons": [r.strip() for r in reasons],
             "barriers": list(merged.values()), "reference": ref, "path": path}
    texts = clean["reasons"] + [b["evidence"] for b in clean["barriers"]] + [ref["evidence"], path["evidence"]]
    dist = [d for t in texts for d in _distances(t)]
    dist_errors = [f"remove the measurement {d!r}; never state distances or sizes" for d in dist]
    return clean, errors, dist_errors


def _scrub(clean: dict) -> dict:
    sub = lambda s: DISTANCE_RE.sub("[distance removed]", s)  # noqa: E731
    clean["reasons"] = [sub(r) for r in clean["reasons"]]
    for b in clean["barriers"]:
        b["evidence"] = sub(b["evidence"])
    clean["reference"]["evidence"] = sub(clean["reference"]["evidence"])
    if clean.get("path"):
        clean["path"]["evidence"] = sub(clean["path"]["evidence"])
    return clean


def _triage_messages(frames_jpeg: list[bytes], meta: dict) -> list[dict]:
    times = meta.get("times_s") or []
    parts: list[dict] = [{"type": "text", "text": (
        f"{len(frames_jpeg)} frames from one clip, in time order. Check the clip and reply with the JSON object.")}]
    for i, jpg in enumerate(frames_jpeg):
        label = f"Frame {i}" + (f" (t={times[i]:.1f}s)" if i < len(times) else "")
        parts.append({"type": "text", "text": label})
        parts.append({"type": "image_url",
                      "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}})
    return [{"role": "system", "content": (PROMPTS / "triage_system.txt").read_text()},
            {"role": "user", "content": parts}]


async def triage(frames_jpeg: list[bytes], meta: dict) -> dict:
    """Ask Muse Spark whether a clip is usable and which barriers it shows.

    frames_jpeg: 4-6 JPEG frames in time order (720p is plenty).
    meta (all optional): "times_s" [float] per frame, used as labels; "frame_indices" [int] per frame, the
      video frame numbers, used to map the answer back; "require_seated_height" (default True), set False
      for stock footage that was not filmed from a wheelchair.
    Returns {"usable", "reasons", "barriers": [{"concept", "frames", "evidence"}], "model"} plus
      "checks", "reference" {"visible", "frames", "evidence"}, "warnings" and "call" (backend, attempts,
      latency_s, usage). "frames" are video frame numbers when meta["frame_indices"] is given, else
      positions in frames_jpeg.
    Raises SparkError when there is no backend or the model gives invalid JSON twice.
    """
    n = len(frames_jpeg)
    if not 1 <= n <= 50:
        raise SparkError(f"triage needs 1-50 frames, got {n}")
    messages = _triage_messages(frames_jpeg, meta)
    h = hashlib.sha256()
    for f in frames_jpeg:
        h.update(hashlib.sha256(f).digest())
    digest = h.hexdigest()
    b = _backend()
    if b.name == "replay":
        result = _cache_get("triage", digest)
    else:
        calls: list[dict] = []
        warnings: list[str] = []
        for attempt in range(2):
            c = await _chat(b, messages, TRIAGE_SCHEMA, TRIAGE_EFFORT, RETRY_MAX_TOKENS if attempt else MAX_TOKENS)
            calls.append(c)
            clean, errors, dist_errors = _validate_triage(_parse_json(c["content"]), n)
            if c["finish"] == "length":
                errors.append("reply was cut off by the token limit")
            if not errors and not dist_errors:
                break
            if attempt == 0:
                messages = messages + _retry_turns(
                    c["content"], "That reply was invalid: " + "; ".join(errors + dist_errors)
                    + ". Reply again with only the corrected JSON object.")
        if errors:
            raise SparkError("triage output invalid twice: " + "; ".join(errors))
        if dist_errors:
            clean = _scrub(clean)
            warnings.append("model wrote distances twice; removed: " + "; ".join(dist_errors))
        result = {**clean, "model": calls[-1]["model"], "warnings": warnings,
                  "call": {"backend": b.name, "attempts": len(calls),
                           "latency_s": round(sum(c["latency_s"] for c in calls), 2), "usage": _sum_usage(calls)}}
        _cache_put("triage", digest, result)

    result = json.loads(json.dumps(result))  # detach from the cache copy
    checks = result["checks"]
    needed = [k for k in CHECKS if k != "seated_height" or meta.get("require_seated_height", True)]
    result["usable"] = all(checks[k] for k in needed) and bool(result["barriers"])
    if checks["barrier_visible"] and not result["barriers"]:
        result["reasons"].append("No barrier was named, so there is nothing to measure.")
    idx = meta.get("frame_indices")
    if idx and len(idx) == n:
        for item in result["barriers"] + [result["reference"]] + ([result["path"]] if result.get("path") else []):
            item["frames"] = [idx[i] for i in item["frames"]]
    return {"usable": result.pop("usable"), "reasons": result.pop("reasons"), "barriers": result.pop("barriers"),
            "model": result.pop("model"), **result}


# ---------------------------------------------------------------- explain

_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?")


def _numbers_in(obj) -> list[float]:
    if isinstance(obj, bool) or obj is None:
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, str):
        return [float(m.replace(",", ".")) for m in _NUMBER_RE.findall(obj)]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _numbers_in(v)]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _numbers_in(v)]
    return []


def _foreign_numbers(text: str, measurement: dict) -> list[str]:
    """Numbers in text that are not a number in measurement, that number to 1 decimal, or a whole number
    within 0.5 of it (68.5 may be written 68 or 69)."""
    known = _numbers_in(measurement)

    def ok(t: float) -> bool:
        return any(abs(t - x) < 1e-6 or abs(t - round(x, 1)) < 1e-6 or (t.is_integer() and abs(t - x) <= 0.5)
                   for x in known)
    return [m for m in _NUMBER_RE.findall(text) if not ok(float(m.replace(",", ".")))]


def _template(measurement: dict) -> str:
    """Deterministic fallback built only from the measurement's own values."""
    verdict = measurement.get("verdict", "unknown")
    gap, band = measurement.get("gap_cm"), measurement.get("band_cm")
    lead = {"passes": "Your wheelchair should fit through here.",
            "tight": "It is tight here: your wheelchair may just fit, with very little room.",
            "blocked": "Your wheelchair will not fit through here.",
            }.get(verdict, "We could not measure this gap, so we cannot say whether your wheelchair fits. "
                           "Try filming again with four A4 sheets taped flat on the ground near the gap, or a bright tactile paving strip, in view.")
    if verdict == "unknown" or not isinstance(gap, (int, float)):
        return lead
    size = f"The free gap measures about {round(gap)} cm"
    if isinstance(band, (list, tuple)) and len(band) == 2 and all(isinstance(v, (int, float)) for v in band):
        size += f" (between {round(band[0])} and {round(band[1])} cm)"
    return f"{lead} {size}."


# languages the page offers (static/i18n.js); explain writes in the one the page sends
LANGS = {"en": "English", "ko": "Korean", "ja": "Japanese", "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese",
         "es": "Spanish", "fr": "French", "de": "German", "vi": "Vietnamese", "id": "Indonesian"}


async def explain(measurement: dict) -> dict:
    """One or two plain sentences on a geometry measurement for a wheelchair user.

    measurement: the dict from geometry.measure_gap, optionally with extra fields such as
      "chair_width_cm", "barrier" or "lang" (a LANGS key, "en" default).
    Returns {"text", "model"} plus "warnings" and "call". Any number in the text is a number from the
    measurement; if the model breaks that twice, a fixed template is used and model is "template".
    """
    lang = LANGS.get(str(measurement.get("lang", "en")), "English")
    payload = {k: v for k, v in measurement.items() if k != "lang"}
    messages = [{"role": "system", "content": (PROMPTS / "explain_system.txt").read_text()},
                {"role": "user", "content": f"Write in {lang}.\nMeasurement JSON:\n"
                                            + json.dumps(payload, ensure_ascii=False)}]
    digest = hashlib.sha256(json.dumps(measurement, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    b = _backend()
    if b.name == "replay":
        try:
            return _cache_get("explain", digest)
        except SparkError:
            return {"text": _template(measurement), "model": "template", "warnings": ["replay miss"], "call": None}

    calls: list[dict] = []
    warnings: list[str] = []
    text, used_template = "", False
    for attempt in range(2):
        c = await _chat(b, messages, None, EXPLAIN_EFFORT, RETRY_MAX_TOKENS if attempt else MAX_TOKENS)
        calls.append(c)
        text = " ".join(c["content"].split())
        problems = [f"the number {m} is not in the measurement" for m in _foreign_numbers(text, measurement)]
        if not text:
            problems.append("the reply was empty")
        if not problems:
            break
        warnings.extend(problems)
        if attempt == 0:
            messages = messages + _retry_turns(
                c["content"], ("Not allowed: " + "; ".join(problems)
                               + ". Rewrite using only numbers from the measurement JSON.") if text else
                "Your reply was empty. Reply with one or two plain sentences as instructed.")
    else:
        text, used_template = _template(measurement), True
    result = {"text": text, "model": "template" if used_template else calls[-1]["model"], "warnings": warnings,
              "call": {"backend": b.name, "attempts": len(calls),
                       "latency_s": round(sum(c["latency_s"] for c in calls), 2), "usage": _sum_usage(calls)}}
    _cache_put("explain", digest, result)
    return result
