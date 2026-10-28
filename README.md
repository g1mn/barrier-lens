# Barrier Lens

**Turn a 10-second phone clip, filmed at wheelchair height, into a measured answer: does the chair fit past this sidewalk barrier?**

Built during the Meta Global AI Developer Hackathon (Oct 27 – Nov 5, 2026) · Indie track · Theme: Push the Frontier.

> Work in progress. This README is rewritten at the end of the build with measured results only.

## Idea in one line
Muse Spark proposes. SAM 3.1 outlines. Geometry measures. People confirm.

- **Muse Spark** rejects unusable clips, names the barrier with frame evidence, explains the verdict and calls a step-free detour tool. Its own distance guesses are discarded.
- **SAM 3.1** segments and tracks each barrier and the reference object, one concept per request.
- **Geometry** turns masks plus a reference of known size into a gap width with an error band, then a verdict: passes / tight / blocked / unknown.
- **People** confirm the pin before it is shared with the next person.

## License
MIT — see [LICENSE](LICENSE).
