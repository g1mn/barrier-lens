# Barrier Lens

**Turn a 10-second phone clip, filmed at wheelchair height, into a measured answer: does the chair fit past this sidewalk barrier?**

Built during the Meta Global AI Developer Hackathon (Oct 27 – Nov 5, 2026) · Indie track · Theme: Push the Frontier. First commit: Oct 28, 2026 11:21 KST. Every line here was written in the build window.

**Live demo:** https://g1mn--barrier-lens-web.us-east.modal.run — tap **Check a spot**, then **Try a sample**, **Sample: closed sidewalk** or **Sample: known camera**. All three are recorded live runs (Muse Spark 1.3 and SAM 3.1 on the Meta Model API) replayed without model calls. Uploading your own clip needs the judge token from the submission's testing instructions, because every live check is a paid call.

## The idea in one line

**Muse Spark proposes. SAM 3.1 outlines. Geometry measures. People confirm.**

| Step | What it does | Where |
|---|---|---|
| Blur | Every frame is face-blurred on our server before anything leaves it (OpenCV YuNet, full frame plus a 2× tiled pass for small faces). | `app/blur.py` |
| Muse Spark 1.3 | Rejects unusable clips, names each barrier with frame evidence, and says whether the walking path is open, narrowed or closed. Strict JSON schema; any distance it writes is rejected. | `app/spark.py`, `app/prompts/triage_system.txt` |
| SAM 3.1 | Segments and tracks each named barrier through the clip, one concept per request, plus a scale reference when one is in view. | `app/sam.py` |
| Geometry | Turns the masks' ground contacts into a free width with a Monte Carlo error band, from a reference of known size (four A4 sheets, a 30 cm tactile block) or from the camera pose (phone height, field of view, tilt). Only the gap straight ahead counts. Too wide a band is reported as **unknown**, never as "tight". | `app/geometry.py`, `app/pipeline.py`, `app/calibration.py` |
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

Every result is labelled *Estimated with default assumptions*, *Calibrated* or *Measured against a reference in the clip*. The *known camera* sample shows what calibration buys: a public dataset clip whose camera height and tilt are known gets a decisive answer from the same pipeline (below).

## What we measured

Numbers below come from files in this repo. Samples are small and labelled as such.

**Public photos with a known length on the ground** (`data/eval/`, `scripts/eval_public.py`, no model calls; three images, a smoke test, not a benchmark). Each cell is the 95% range; ✓ means it contains the true value:

| Photo | What is known | Default (seated) | Camera height and lens known | Plus tilt |
|---|---|---|---|---|
| Graz, Kronesgasse (CC0) | clear width 85 cm, kerb edge to wall base | 20–124 ✓ | 40–234 ✓ | 55–137 ✓ |
| Teltow (CC BY 3.0) | a 1 m folding ruler lying across the path | **37–57 ✗** | 74–124 ✓ | 75–112 ✓ |
| Altenplathow bridge (CC BY-SA 4.0) | path width 230 cm between parapets | 64–420 ✓ | 88–930 ✓ | — |

The ranges are honest but wide, so the verdicts here are mostly "unknown". These photos were taken standing, so the seated default is the wrong assumption for them (in the app, Muse Spark's `seated_height` check rejects such clips); camera heights in the middle columns are guesses from the viewpoint; the "tilt" column takes the tilt from lines on the ground, not from an independent sensor. The Teltow row is the point: with default assumptions the app can be confidently wrong about scale, which is why a pass needs confirmation and why calibration matters.

**Recorded live runs** (`static/demo/*.json`, Pexels clips, first 10 s):

| Clip | Muse Spark path | Verdict | Explanation |
|---|---|---|---|
| Sidewalk closed for construction (Pexels) | closed (the sign, cones and work equipment) | blocked (geometry alone could not decide: 38 cm, range 12–116) | "You cannot get through because the sidewalk is closed off with a barricade and cones ahead." |
| Scooters parked along a fence, filmed facing the row (Pexels) | narrowed | unknown: no gap between the outlined scooters lies straight ahead | asks to film facing the gap, with both sides in view |
| Construction corridor, New York (SANPO, known camera) | narrowed | passes, 131 cm, range 101–168 (65 cm chair) | "You have about 131 cm to get past, so you should fit through with room to spare." |

The known-camera clip is 10 s of the [SANPO](https://github.com/google-research-datasets/sanpo_dataset) dataset (Google Research, CC BY 4.0), filmed at chest height. Its camera height (1.16 m, from frames 300–450 of the same walk) and tilt (12.1° down, averaged over the clip) come from the dataset's stereo depth by a ground-plane fit, standing in for a one-time calibration and the phone's tilt sensor; the dataset's own IMU poses drift by 2–10° against the ground within 10 s, so they were not used. **Check:** at the two gap ends the app measured, the dataset's stereo depth gives 117 cm, and that frame's own ground plane gives 124 cm; both lie inside the app's 101–168 cm range. The stereo depth is itself an estimate (CREStereo), so this is a consistency check, not ground truth.

**Cost and time per live check** (measured on the deployed app, Modal, 4 CPUs): a 7 s clip takes about 56 s end to end (blur 12 s, Muse Spark 7 s, SAM 22 s, geometry 1 s, explanation 5 s); SAM costs $0.03 for a 7 s clip with two concepts and $0.06 for a 10 s clip with three. Muse Spark ran on the hackathon's free Meta Model API credits (not priced here).

**SAM 3.1 vocabulary** (`docs/vocab.md`): on the same clip "e-scooter" found 0 of 30 frames and "scooter" 30 of 30; "barricade", "road barrier" and "sawhorse" found nothing on a closed sidewalk while "sign" found the panel in 30 of 30. The triage prompt asks for the words that work.

## Tests

119 tests (`tests/`): geometry on synthetic scenes with known truth, the error-band and verdict rules, the straight-ahead gap rule, camera-pose solving, SAM replay fixtures, the explanation's number and cause checks, the path-closed and default-pass rules, the streaming route and the demo-token gate.

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

API: `POST /api/analyze` (one JSON result), `POST /api/analyze/stream` (one line per stage, then the result), `GET /api/sample?name=scooters|closed|known`, `POST /api/calibrate`, `GET /api/towing`, `GET /api/station`.

## Data and licences

- Seoul Open Data Plaza: e-scooter towing records OA-23057, station facilities OA-15994, OA-21212, OA-21211 — Korea Open Government License Type 1. Addresses geocoded with VWorld (국토교통부 브이월드). See `data/seoul/ATTRIBUTION.txt`.
- Station exits: © OpenStreetMap contributors, ODbL 1.0.
- Evaluation photos: Wikimedia Commons, CC0 / CC BY 3.0 / CC BY-SA 4.0, credited in `docs/eval-sources.md`.
- Demo clips: Pexels License (two clips) and SANPO, CC BY 4.0 (one clip, changes listed), credited in `static/demo/ATTRIBUTION.txt`.
- Face detector: OpenCV Zoo YuNet (MIT), downloaded at build time with a pinned hash.

## Limitations

- Licence plates are not detected (no plate detector in OpenCV 5). We check demo frames by eye.
- The measurement uses one frame; masks that do not reach the ground make a barrier look further away than it is.
- The way through is the widest gap straight ahead: a neighbouring pair that straddles the image's centre column and stands side by side, not one behind the other. Gaps elsewhere are listed but never chosen. This assumes you film facing the gap you want to pass; a barrier dead ahead is measured only on the side of the centre line it leaves open. (Before 10-29 the widest gap anywhere was chosen, and a corridor between a fence and a row of jersey barriers measured 13 m, to objects beyond the row.)
- No medical or health advice, by design: the app only compares a measured width with a stated chair width.

## License

MIT — see [LICENSE](LICENSE).
