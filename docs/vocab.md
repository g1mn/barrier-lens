# SAM 3.1 concept vocabulary (probe 2026-10-28)

What phrases SAM 3.1 (Meta Model API, `sam-3.1`) actually segments on our dev footage. One concept per request.
Produced by `scripts/vocab_probe.py`; every number below comes from its output (`results.json` in the run's
scratch dir) or from the replay fixtures in `data/fixtures/sam/`. Small sample: 3 clips, 3 s each.

## Setup

- Cuts: 3 s, 10 fps, 720 px short side, H.264, 30 frames each.
  - `scooters-row-0-3s.mp4` = the exact clip SAM saw in the earlier `scooter` / `e-scooter` calls
    (copy of the prepped `69bee8c8ddaaabb4-10fps-720x1280.mp4`), so frame indices line up with those fixtures.
  - `sidewalk-closed-0-3s.mp4` = `sidewalk-closed-35845191.mp4` 0–3 s, 720x1280, crf 26.
  - `scooters-city-6-9s.mp4` = `scooters-city-36383602.mp4` 6–9 s, 1280x720, crf 26. Chosen because the
    tactile strip is in frame and no person is visible (checked at 2 fps over 5–11 s).
- Replay: fixtures are keyed by the sha256 of the cut. `scooters-city-6-9s.mp4` and `scooters-row-0-3s.mp4` are
  copied to `data/fixtures/clips/` (251 KB, 854 KB). `*.mp4` is gitignored, so they need `git add -f` (or a
  `!data/fixtures/clips/*.mp4` line). The sidewalk-closed cut was not copied: road workers are in the background.
- Cost: every request billed 50 frames (`usage.video_frames_processed` = 50) = $0.01. 10 paid calls, $0.10 total.
  Re-running the probe is free (content-hash cache).

## Phrase table

All calls returned HTTP 200 and `outcome: completed`. "Score" is the per-object confidence. It was the same
in every frame for a given object.

| Phrase | Clip | Frames with objects | Objects / frame | Scores | Result | Cost |
|---|---|---|---|---|---|---|
| `scooter` (earlier call) | scooters-row | 30/30 | 2 | 0.734, 0.762 | front scooter + a partial object at the right edge | $0.01 |
| `e-scooter` (earlier call) | scooters-row | 0/30 | 0 | – | nothing | $0.01 |
| `electric scooter` | scooters-row | 30/30 | 1 | 0.714 | front scooter only (same box as `scooter` id 1) | $0.01 |
| `barricade` | sidewalk-closed | 0/30 | 0 | – | nothing | $0.01 |
| `road barrier` | sidewalk-closed | 0/30 | 0 | – | nothing | $0.01 |
| `sawhorse` | sidewalk-closed | 0/30 | 0 | – | nothing | $0.01 |
| `sign` | sidewalk-closed | 30/30 | 1 | 0.961 | the SIDEWALK CLOSED panel | $0.01 |
| `tactile paving` | scooters-city | 0/30 | 0 | – | nothing | $0.01 |
| `yellow tactile paving` | scooters-city | 0/30 | 0 | – | nothing | $0.01 |
| `tactile paving strip` | scooters-city | 0/30 | 0 | – | nothing | $0.01 |
| `yellow tile` | scooters-city | 0/30 | 0 | – | nothing | $0.01 |
| `scooter` | scooters-city | 30/30 | 3 | 0.914, 0.938, 0.949 | 3 of 4 scooters, one object each | $0.01 |

Latency (stream, per call): 2.2–5.0 s.

## What the masks cover (contact sheets, 4 frames each, checked by eye)

- **`electric scooter` on scooters-row**: one mask on the nearest (yellow) scooter of a row of five parked
  scooters. The box is 66% of the frame width because that scooter spans it, but the mask is only 4.7% of the
  frame area and follows that one scooter's deck, stem and wheels. The four scooters behind it get no mask.
  No merging. It also missed them. `scooter` found the same front scooter plus one more object, so it is at
  least as good here.
- **`sign` on sidewalk-closed**: a tight mask on the rectangular sign panel in all 30 frames. The sawhorse
  legs and the white crossboard under it are not in the mask. The barricade itself was found by none of
  `barricade`, `road barrier` or `sawhorse`. The cones in the background are not masked either.
- **`scooter` on scooters-city**: three separate masks, each on one standing scooter (left, middle-front,
  right-front). The right-hand scooter that stands partly behind another gets no mask of its own, and its
  visible deck is not folded into the front one's mask. **No merging** of several scooters into one object. The
  shared e-bike in the middle is correctly left out. 21 of 30 frames are kept in the fixture to stay under 250 KB.
- **Tactile paving**: nothing for all four phrases. That means no strip mask and no block masks. On this clip the
  strip is faded, worn yellow directional bars inside a brick pavement, seen at a low oblique angle.

## Recommended concepts for the pipeline

| Use | Concept | Notes |
|---|---|---|
| Parked scooters (obstacle) | `scooter` | Separate object per scooter; occluded ones can be missed. Don't use `e-scooter`. `electric scooter` found fewer objects. |
| Closure / temporary signs | `sign` | Gives the panel, not the stand. Treat it as evidence of a closure, not as the obstacle footprint. |
| Barricades | none found yet | `barricade`, `road barrier`, `sawhorse` all returned zero on a white A-frame sawhorse. Untested: `traffic cone`, `fence`. Re-test on Korean footage (different barricade styles). |
| Tactile paving (scale reference) | none found yet | Four phrases returned zero on worn paving. The 0.3 m block reference cannot come from SAM on this footage. Re-test on fresh Korean footage with bright yellow blocks before relying on it. Fallback: the known-rectangle/A4 reference in `app/geometry.py`. |

Next probes worth a call each, on Korean footage: `tactile paving`, `yellow paving`, `barricade`,
`traffic cone`, `bollard`.

## Ground concepts for horizon / pitch (2026-10-28)

Goal: a SAM 3.1 phrase whose mask edges give straight ground lines, ideally two that converge, so pitch can
be estimated from a vanishing point. Five paid calls (one concept each, 50 frames billed each, $0.05 total),
run with `scripts/vocab_probe.py data/fixtures/clips --budget 5 --fixtures`. Fixtures:
`data/fixtures/sam/scooters-row-0-3s-{sidewalk,curb,road}.json`,
`scooters-city-6-9s-{sidewalk,pavement}.json`, all under 250 KB, all frames kept (28-30).

Straightness was measured on frames 0, 9, 19, 29: for each mask column, take the top (or bottom) boundary
row, RANSAC line fit (3 px tolerance), then least squares on the inliers. "Span" = inlier x-extent / frame
width. Slope is in image coordinates (y down; negative = rising to the right). Ranges are over the 4 frames.

| Phrase | Clip | Frames | Objects | Score | What the mask covers (by eye, 4 frames) | Boundary line (4 frames) | Latency |
|---|---|---|---|---|---|---|---|
| `sidewalk` | scooters-row | 30/30 | 1 | 0.918 | whole near sidewalk, bottom 44% of the frame; top edge = curb, but cut by the scooter wheels and shadows standing on it | top: only 35-36% of columns on one line, +5.6..+6.1 deg, span 0.51-0.52, rms 1.0-1.2 px (fit lands on the occluded part, not the curb) | 3.9 s |
| `curb` | scooters-row | 28/30 | 1 | 0.704 | thin strip on the right half of the curb only (box 43% of width); missing in 2 frames incl. f0 | top: -7.5..-7.6 deg, all columns inliers, span 0.43, rms 1.2 px | 3.5 s |
| `road` | scooters-row | 30/30 | 1 | 0.844 | asphalt between curb and the parked motorbikes; full width | bottom (= curb): -7.5..-7.8 deg, inliers 0.50-0.53, span 0.75-0.77, rms 1.2-1.3 px. top: ~0 deg, but it is the motorbikes' occlusion edge, not a ground line | 3.7 s |
| `sidewalk` | scooters-city | 30/30 | 1 | 0.789 | **wrong surface**: a small yellow-painted curb behind the fence (box 20% x 7%), not the brick pavement in front | top: +2.4 deg, span 0.19 | 2.5 s |
| `pavement` | scooters-city | 30/30 | 2 | 0.754, 0.863 | id1: the brick pavement, bottom 56% of the frame (top edge = wall base, broken by scooter wheels); id0: the same far yellow curb as `sidewalk` | id1 top: only 22% of columns on one line, -1.0..-1.1 deg (f0: -4.6), span 0.64-0.66 | 4.3 s |

What the edges give:

- **One line per clip, never two converging ones.** Every ground mask here is bounded by the frame edges on
  the left, right and bottom, so its only real ground edge is one curb or wall base. On scooters-row the
  curb is the same line in `curb` (-7.6 deg) and in the bottom of `road` (-7.5..-7.8 deg), stable over 30
  frames to 0.3 deg, rms about 1.3 px. That is a good *single* line (roll / one direction), not a vanishing
  point.
- **Objects standing on the ground cut the top edge** of `sidewalk` and `pavement` (scooter wheels, shadows),
  so the edge on the side of the obstacle is the worst one to fit. `road` (on the far side of the curb) gives
  the cleaner curb line on scooters-row.
- **Lines inside the mask (free, no API call).** Canny + HoughLinesP restricted to the dilated ground mask,
  segments >= 15% of width, frames 0 and 19:
  - scooters-row `sidewalk`: 2 segments per frame, angles 7.7 / 80.1 deg (f0) and 7.4 / 158.1 deg (f19).
    One is a slab joint running into depth; the other is a scooter-shadow edge. Not a consistent pair.
  - scooters-city `pavement`: 5-6 segments per frame, all at 2.3-3.1 deg: the tactile-strip edges, which
    run across the image. They are nearly parallel in the image (spread under 1 deg), so their
    intersection is far away and badly conditioned. This is the failure mode the end-to-end eval saw:
    pitch from near-parallel lines needs a wide, data-driven sd, or should be refused.

**Recommendation.** Use `road` as the ground-line concept where a curb is in view (its bottom edge is the
cleanest, longest straight ground line here: span 0.75, rms about 1.3 px), with `curb` as a cross-check.
Use `sidewalk` / `pavement` only as a *region* to search for joints, not for their own edges. On these two
cuts no phrase gives two converging ground lines, so the pitch must come from the sensor or the default
prior, and a line-based pitch should only be used when two lines with clearly different image angles
(say, over 10 deg apart) are found. `sidewalk` is unreliable on scooters-city: it picked a far yellow curb.
Not tested yet: `crosswalk`, `lane marking`, `tile joint`, and any Korean footage.
