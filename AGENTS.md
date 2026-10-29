# Barrier Lens — rules for anyone (human or agent) working in this repo

Built during the Meta Global AI Developer Hackathon (build window Oct 27 – Nov 4, 2026, deadline 9 PM ET; Indie Developer track, theme: Push the Frontier).

## Non-negotiable
- **New work only.** Every line of code here is written during the build window. Do not copy code from earlier projects or from `aidd-log/hackathon-prep/`; read them for ideas, then write fresh.
- **No secrets in git.** Keys live in `~/.config/barrier-lens/.env` (mode 600) and are loaded at runtime. The pre-commit hook in `scripts/hooks/pre-commit` blocks any value from that file and the judge token in `~/.config/barrier-lens/demo-token`. Never print a key.
- **Meta models do the model work.** SAM 3.1 (segmentation and tracking) and Muse Spark (clip triage, structured verdict, explanation, tool calls) through the Meta Model API; Muse Spark Open (open weights) through Baseten where noted.
- **Models propose, geometry decides.** A width in centimetres comes from geometry (masks + either a reference of known size, or the camera pose: phone height, field of view and tilt), never from a model's guess. Without a reference we measure from default assumptions and say so; every uncertain input goes into the Monte Carlo band, and a band too wide to decide is reported as "unknown", never as "tight". A per-user calibration narrows the band and the result is labelled "calibrated".
- **No medical advice, no diagnosis.** We report whether a measured gap fits a stated wheelchair width. Nothing about health.
- **Honest numbers.** Every number in the README or video comes from a file in this repo (eval output, logs). Small samples are labelled as such.
- **People in footage.** Blur faces and licence plates in footage we film or record before anything is published; third-party photos (Wikimedia Commons, under their licences) are used as published and kept out of the video. Film only with consent.

## Commits
- Small commits, one push at a time is fine. Author `g1mn`.
- Never add AI co-author trailers.
