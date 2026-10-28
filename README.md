# Barrier Lens

**Turn a 10-second phone clip, filmed at wheelchair height, into a measured answer: does the chair fit past this sidewalk barrier?**

Built during the Meta Global AI Developer Hackathon (Oct 27 – Nov 5, 2026) · Indie track · Theme: Push the Frontier. First commit: Oct 28, 2026 11:21 KST. Every line here was written in the build window.

**Live demo:** https://g1mn--barrier-lens-web.us-east.modal.run — tap **Check a spot**, then **Try a sample** or **Sample: closed sidewalk**. Both samples are recorded live runs (Muse Spark 1.3 and SAM 3.1 on the Meta Model API) replayed without model calls. Uploading your own clip needs the judge token from the submission's testing instructions, because every live check is a paid call.

## The idea in one line

**Muse Spark proposes. SAM 3.1 outlines. Geometry measures. People confirm.**

| Step | What it does | Where |
|---|---|---|
| Blur | Every frame is face-blurred on our server before anything leaves it (OpenCV YuNet, full frame plus a 2× tiled pass for small faces). | `app/blur.py` |
| Muse Spark 1.3 | Rejects unusable clips, names each barrier with frame evidence, and says whether the walking path is open, narrowed or closed. Strict JSON schema; any distance it writes is rejected. | `app/spark.py`, `app/prompts/triage_system.txt` |
| SAM 3.1 | Segments and tracks each named barrier through the clip, one concept per request, plus a scale reference when one is in view. | `app/sam.py` |
| Geometry | Turns the masks' ground contacts into a free width with a Monte Carlo error band, from a reference of known size (four A4 sheets, a 30 cm tactile block) or from the camera pose (phone height, field of view, tilt). Too wide a band is reported as **unknown**, never as "tight". | `app/geometry.py`, `app/pipeline.py`, `app/calibration.py` |
| Muse Spark 1.3 | Explains the verdict in one or two plain sentences. Every number must appear in the measurement; an "unknown" must name the measured cause (for example, the tilt was not recorded). Otherwise a fixed sentence is used. | `app/spark.py`, `app/analyze.py` |
| People | Confirm the pin on the map. Sharing it with the next person is not built yet. | `static/app.js` |

Two rules keep the models honest:

1. **Models may only make a verdict more cautious, never more hopeful.** If Muse Spark sees the sidewalk closed, the verdict is *blocked* whatever the geometry measured (sample: *closed sidewalk*). A *pass* is never reported from default assumptions alone; it needs a reference, the phone's tilt or a calibration.
2. **The width always comes from geometry.** The models name things, outline them and explain; they never estimate a distance.

## Calibrate once

Without a reference or a tilt reading, the error band from default assumptions is usually too wide to decide, and the app says so and offers the fix:

- **Phone tilt:** turn on *Use phone tilt* (automatic on Android, one tap on iPhone). The tilt is averaged while you film and sent with the clip.
- **Profile:** wheelchair width and armrest height (the phone's height), and optionally the phone's main-camera focal length.
- **One reference clip:** lay four A4 sheets (or one tactile block) flat on the floor, hold the phone at your armrest and film three seconds. The app solves the phone's height and tilt offset once (`solvePnP`) and uses them for every later check.

Every result is labelled *Estimated with default assumptions*, *Calibrated* or *Measured against a reference in the clip*.

## What we measured

Numbers below come from files in this repo. Samples are small and labelled as such.

**Public photos with a known width** (`data/eval/`, `scripts/eval_public.py`, no model calls; three images, a smoke test, not a benchmark):

| Photo | True width | Default (seated) | Camera height and lens known | Plus tilt |
|---|---|---|---|---|
| Graz, Kronesgasse (CC0) | 85 cm | unknown, 20–124 | unknown, 40–234 | unknown, 55–137 |
| Teltow, 1 m ruler (CC BY 3.0) | 100 cm | **blocked, 37–57 (wrong)** | tight, 74–124 | tight, 75–112 |
| Altenplathow bridge (CC BY-SA 4.0) | 230 cm | unknown, 64–420 | passes, 88–930 | — |

These photos were taken standing, so the seated default is the wrong assumption for them; in the app, Muse Spark's `seated_height` check rejects such clips. Camera heights in the middle columns are guesses from the viewpoint, and the "tilt" column takes the tilt from lines on the ground (not an independent sensor). The point they make: with default assumptions the app can be confidently wrong, which is why a pass needs confirmation and why calibration matters.

**Recorded live runs** (`static/demo/*.json`, Pexels clips, first 10 s):

| Clip | Muse Spark path | Verdict | Explanation |
|---|---|---|---|
| Sidewalk closed for construction | closed (the sign, cones and work equipment) | blocked (geometry alone measured a pass) | "You are blocked here — the sidewalk is closed with a sign and cones and work ahead of you." |
| Scooters parked along a fence | narrowed | unknown, best guess 33 cm, range 16–126 | names the cause (tilt not recorded) and the fix |

**Cost and time per live check** (measured on the deployed app, Modal, 4 CPUs): a 7 s clip takes about 56 s end to end (blur 12 s, Muse Spark 7 s, SAM 22 s, geometry 1 s, explanation 5 s); SAM costs $0.03 for a 7 s clip with two concepts and $0.06 for a 10 s clip with three. Muse Spark ran on the hackathon's free Meta Model API credits (not priced here).

**SAM 3.1 vocabulary** (`docs/vocab.md`): on the same clip "e-scooter" found 0 of 30 frames and "scooter" 30 of 30; "barricade", "road barrier" and "sawhorse" found nothing on a closed sidewalk while "sign" found the panel in 30 of 30. The triage prompt asks for the words that work.

## Tests

118 tests (`tests/`): geometry on synthetic scenes with known truth, the error-band and verdict rules, camera-pose solving, SAM replay fixtures, the explanation's number and cause checks, the path-closed and default-pass rules, the streaming route and the demo-token gate.

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Run it

```bash
# keys live outside the repo: ~/.config/barrier-lens/.env (META_API_KEY; optional SEOUL_OPEN_DATA_KEY)
.venv/bin/python -m uvicorn app.main:app --app-dir . --port 8799
```

Without `META_API_KEY` the app runs in replay mode (cached and fixture results only, no paid calls). Deploy: `modal deploy modal_app.py` (secrets in a Modal secret named `barrier-lens`), or the `Dockerfile` on any container host. Set `BL_DEMO_TOKEN` on a public deploy so only token holders can start paid checks; `BL_SPEND_CAP_USD` caps SAM spend per server process.

API: `POST /api/analyze` (one JSON result), `POST /api/analyze/stream` (one line per stage, then the result), `GET /api/sample?name=scooters|closed`, `POST /api/calibrate`, `GET /api/towing`, `GET /api/station`.

## Data and licences

- Seoul Open Data Plaza: e-scooter towing records OA-23057, station facilities OA-15994, OA-21212, OA-21211 — Korea Open Government License Type 1. Addresses geocoded with VWorld (국토교통부 브이월드). See `data/seoul/ATTRIBUTION.txt`.
- Station exits: © OpenStreetMap contributors, ODbL 1.0.
- Evaluation photos: Wikimedia Commons, CC0 / CC BY 3.0 / CC BY-SA 4.0, credited in `docs/eval-sources.md`.
- Demo clips: Pexels License, credited in `static/demo/ATTRIBUTION.txt`.
- Face detector: OpenCV Zoo YuNet (MIT), downloaded at build time with a pinned hash.

## Limitations

- Licence plates are not detected (no plate detector in OpenCV 5). We check demo frames by eye.
- The measurement uses one frame; masks that do not reach the ground make a barrier look further away than it is.
- The way through is taken to be the widest gap between neighbouring barriers (or a barrier and the walkway edge). That is not always a gap a wheelchair could reach, for example two scooters parked side by side against a fence.
- No medical or health advice, by design: the app only compares a measured width with a stated chair width.

## License

MIT — see [LICENSE](LICENSE).
