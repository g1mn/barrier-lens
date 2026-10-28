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
