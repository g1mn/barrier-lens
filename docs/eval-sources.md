# Evaluation sources (public ground truth)

Images in `data/eval/` let us check a measurement against a number someone else wrote down, without filming. They are small (6 images, 2.7 MB) and all licences allow redistribution in this repo. `scripts/fetch_eval.py` re-checks licence and EXIF through the Commons API and re-downloads anything missing (long side ≤ 1600 px, re-encoded, EXIF stripped; the EXIF we need is in the manifest).

Every truth value comes from the photographer's own Commons description. Where we interpret which edges a number spans, the manifest says "OUR READING" or "OUR ASSUMPTION". Camera heights are guesses from the viewpoint, not data. Six images are a smoke test, not an accuracy claim.

| id | truth | licence | camera / HFOV | use |
|---|---|---|---|---|
| graz-kronesgasse-85 | clear width **0.85 m** (kerb edge → wall base) | CC0, SpeciesATosm | Canon IXUS 115 HS, 5.0 mm → 63.3° | main width check, narrow (should fail a 0.90 m chair) |
| graz-muenzgraben-corner | turning space **< 1.50 m** (bound only) | CC0, SpeciesATosm | same camera, 63.3° | bound check; also shows tactile paving |
| teltow-osdorfer-ruler-1m | **1.00 m** folding ruler lying on the path | CC BY 3.0, Alltagsradler Teltow | Panasonic DMC-TZ5, 40 mm equiv → 46.8° | ground-plane scale check |
| teltow-mahlower-rulers | **2.50 m** and **2.00 m** marked by rulers on the ground | CC BY 3.0, Alltagsradler Teltow | DMC-TZ5, 128 mm equiv → 15.4° | telephoto stress test; edge reading needs checking |
| altenplathow-bridge-230-a | path width **2.30 m** | CC BY-SA 4.0, Georgfotoart | Nikon D90, 27 mm equiv → 67.5° | weak (side view) |
| altenplathow-bridge-230-c | path width **2.30 m** between parapets | CC BY-SA 4.0, Georgfotoart | Nikon D90, 45 mm equiv → 43.7° | head-on width check, wide (should pass) |

HFOV is computed by `fetch_eval.py`: from the EXIF 35 mm-equivalent focal length with the diagonal convention (43.27 mm), or, for the Canon (no 35 mm field), from EXIF focal 5.0 mm and the nominal 1/2.3" sensor width 6.17 mm (spec-sheet value, an assumption).

## Attribution (required for CC BY / BY-SA)

- "Früherer Zustand, Einmündung der Osdorfer Straße in die Mahlower" and "Nordseite Mahlower Straße …" by Alltagsradler Teltow (via Panoramio), CC BY 3.0. Resized.
- "Steinbogen-Fußgängerbrücke Altenplathow 01 / 03" by Georgfotoart, CC BY-SA 4.0. Our resized copies stay under CC BY-SA 4.0.
- The two Graz photos by SpeciesATosm are CC0 (no attribution needed; we credit anyway).

Full URLs and licence links are in `data/eval/manifest.json`.

## Privacy

Before any of these appear in the demo video: blur the licence plate on the car at the far left of `graz-kronesgasse-85` and the distant pedestrian in `graz-muenzgraben-corner` (rule in AGENTS.md).

## Searched, not used

- Other SpeciesATosm Graz photos (kerb heights, rounded kerbs, `Gehsteig-Ecke-Petersgasse-21` "150×150 cm not given"): no width number.
- `Bad Aussee - Mercedes-Brücke.JPG` (CC BY-SA 4.0, deck width 2.0 m in the description): the photo is a distant view of the ring bridge; deck not measurable.
- `Steinbogen-Fußgängerbrücke Altenplathow 02`: deck not visible.
- Commons full-text search for "Gehweg Breite", "Gehwegbreite", "Restgehwegbreite", "Durchgangsbreite", "sidewalk width", "trottoir largeur", Polish/Spanish equivalents: mostly scanned books. Commons rate-limits search (HTTP 429) after a handful of rapid queries; the script backs off.

## Google SANPO (checked, not downloaded)

- Licence: CC BY 4.0 (README of github.com/google-research-datasets/sanpo_dataset). README says faces and plates are blurred and the full set is ~6 TB.
- Public bucket, no login: `https://storage.googleapis.com/gresearch/sanpo_dataset/v0/`. Listing via the JSON API works (`storage/v1/b/gresearch/o?prefix=…`).
- 702 real sessions under `sanpo-real/`. One session measured (`-5OCPnbrwJdu3jH70ieU7pUiFsOJQoeG`): 9.6 GB in total, chest and head ZED 2i stereo cameras, 528 frames per camera at 15 fps.
- Every frame is a separate file, so single frames can be fetched: video frame PNG ≈ 3.5 MB, metric depth map (`depth_maps/NNNNNN.float16.gz`) ≈ 0.6–0.9 MB, segmentation mask PNG ≈ 20–25 KB. `description.json` holds the intrinsics (this session, left camera: fx = fy = 1973.67 px, cx = 1134.8, cy = 655.6, zero distortion; with the 2208 px ZED 2K width implied by cx this is HFOV ≈ 58.4°, the width is our inference) and `camera_poses.csv` holds per-frame pose.
- One depth map decoded: 921,602 float16 values (1280×720 = 921,600, plus 2 extra values we did not identify), range 0.54–1280 m.
- `labelmap.json` includes `curb` (2), `sidewalk` (3), `crosswalk` (5), `obstacle` (20), `pole` (24), `bike rack` (26). Sidewalk mask + metric depth + intrinsics would give a measured sidewalk width per frame, plus a known camera height (chest camera) to test the default-height model. Not every session has masks (this one has them only on chest/left).
- Suggested use: pick 5–10 frames where `sidewalk` and `curb` are both labelled, fetch frame + depth + mask (~5 MB per frame), compute width from depth, compare with our default-assumption estimate. Do not commit SANPO frames in bulk; a few with attribution are allowed by CC BY 4.0.

## Phone camera default (note for the geometry owner)

Computed from 35 mm-equivalent focal length, 4:3 frame, diagonal convention: 26 mm equiv → HFOV 67.3°, 24 mm → 71.6°, 28 mm → 63.4°. The focal lengths of current phone main cameras (24–26 mm) are from maker spec pages as we remember them, not re-checked here: treat them as an assumption.
