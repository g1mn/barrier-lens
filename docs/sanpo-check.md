# SANPO known-camera sample: camera pose and stereo cross-check

The *known camera* sample (`static/demo/sanpo-corridor-live.*`) is 10 s of the [SANPO](https://github.com/google-research-datasets/sanpo_dataset) dataset (Google Research, CC BY 4.0): session `Xoe7Kec_tA-bzqYd6hDEMMF6-rFrZx1b` (New York, sunny, walking), chest camera, left lens, frames 0–149 at 15 fps. Files were fetched one by one from the public bucket `https://storage.googleapis.com/gresearch/sanpo_dataset/v0/sanpo-real/<session>/camera_chest/left/` (`video_frames/NNNNNN.png`, `depth_maps/NNNNNN.float16.gz`, `description.json`, `fixed_camera_poses.csv`). Analysis done Oct 29, 2026; the numbers below were computed then and are not re-run by the test suite.

## Camera height and tilt (stand-ins for a calibration and the phone's tilt sensor)

- Ground plane: RANSAC plane fit to the CREStereo depth map (gzip, 4-byte header, float16, 2208×1242) over a band at the bottom centre of each frame. Camera axes are OpenCV (x right, y down, z forward); with plane normal *n* in camera axes, `pitch_down = -asin(n_z)` and `roll = atan2(n_x, -n_y)`, the same roll convention as `app/geometry.py`.
- Over frames 0–149 the fit gives height 1.125–1.195 m, pitch 9.6–14.2° down, roll +0.1 to −5.2°.
- Values given to the app: **height 1.156 m** (mean over frames 300–450 of the same session, standing in for a one-time calibration done earlier), **tilt 12.12° down, roll −2.26°** (mean over frames 0–149, standing in for the phone's averaged tilt sensor), horizontal field of view 58.32° (from `description.json`), chair 65 cm. Because the camera height is known, the seated-height check was not required for this run (noted in the sample's `notes`).
- The dataset's IMU poses (`fixed_camera_poses.csv`) were not used: in the same 10 s their tilt differs from the ground-plane fit by 2.3° to 9.5° (some frames are marked tracking `NOT_READY`).

## Result and cross-check

- App result (recorded live run, Muse Spark 1.3 + SAM 3.1): **passes, 131.2 cm, 95% band 100.9–167.5 cm**, between the construction fence on the left and the first water-filled barrier on the right.
- The frame the app measured (normalised clip frame 77) is source frame 116 (matched by mean squared error, 82.8).
- **Stereo depth at the same two gap ends:** 117.4 cm.
- **That frame's own ground plane** (height 1.146 m) applied to the same two image points: 124.4 cm.
- Both lie inside the app's band. CREStereo depth is itself an estimate, so this is a consistency check, not ground truth.

## What this run found in the app

The first live run reported "passes, 1324 cm": the old rule took the widest gap between any neighbouring barriers, here two objects beyond the barrier row. That led to the straight-ahead rule (README, *Limitations*).
