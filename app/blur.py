"""Local face blurring, run on every frame of the normalised clip BEFORE any frame or clip leaves the machine.

Detector: OpenCV's YuNet face detector (cv2.FaceDetectorYN, model face_detection_yunet_2023mar.onnx from
opencv_zoo, MIT licence). The brief asked for OpenCV's bundled Haar cascades, but the installed
opencv-python-headless 5.0 no longer ships cv2.data.haarcascades nor cv2.CascadeClassifier (checked 2026-10-28).
When an OpenCV 4.x build with the cascades is installed instead, the frontal + profile Haar cascades are used as a
fallback if the YuNet model cannot be loaded.

The model file is fetched once into data/cache/models/ and checked against a pinned SHA-256. If no detector can be
loaded, blur_video raises BlurError: the pipeline refuses to send an unblurred clip rather than skip the step.

Limitations, stated plainly:
- Faces turned well away from the camera (strong profile, back of the head), very small faces (under ~10 px) and
  faces under heavy motion blur can be missed. A detection is carried over to the next CARRY_FRAMES frames to cover
  one-frame dropouts, which helps but is no guarantee. Review a clip before publishing it.
- Licence plates are NOT detected: OpenCV 5 has no plate detector and the 4.x Haar plate cascade is trained on
  Russian plates. Counts report plates as None (not checked), never 0.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "cache" / "models"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + YUNET_FILE
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SCORE_THRESHOLD = 0.6   # below YuNet's 0.9 default: a missed face costs more than an extra blurred patch
CARRY_FRAMES = 3
VERSION = 3  # bump when detection or blurring changes; cached blurred clips are rebuilt
UPSCALE = 2
# YuNet's memory grows with its input area: a 2x 1280x720 frame (3.7 MP) took ~450 MB, over a 512 MB free host.
# So the upscaled pass runs in overlapping tiles of at most this many pixels, and a full-frame pass at 1x catches
# faces large enough to be cut by a tile edge.
TILE_MAX_PX = 1_000_000
TILE_OVERLAP = 96  # px in the upscaled image: wider than a small face there
PAD_X, PAD_Y = 0.30, 0.40  # grow each box so hair, ears and chin are covered


class BlurError(RuntimeError):
    """No face detector available, or the clip could not be read/written."""


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ensure_model() -> Path:
    """Path to the YuNet model, downloading it once (pinned hash) when missing."""
    p = MODEL_DIR / YUNET_FILE
    if p.exists() and _sha256(p) == YUNET_SHA256:
        return p
    import httpx
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = httpx.get(YUNET_URL, follow_redirects=True, timeout=60)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise BlurError(f"could not download the face detector model: {type(e).__name__}") from None
    if hashlib.sha256(r.content).hexdigest() != YUNET_SHA256:
        raise BlurError("face detector model hash mismatch; refusing to use it")
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(p)
    return p


class FaceDetector:
    """detect(img) -> list of (x0, y0, x1, y1) int boxes."""

    def __init__(self):
        self.name = None
        self._yunet = None
        self._haar: list = []
        try:
            self._yunet = cv2.FaceDetectorYN.create(str(ensure_model()), "", (320, 320), SCORE_THRESHOLD, 0.3, 5000)
            self.name = "OpenCV YuNet (face_detection_yunet_2023mar)"
        except (BlurError, cv2.error, AttributeError) as e:
            base = getattr(getattr(cv2, "data", None), "haarcascades", None)
            if base and hasattr(cv2, "CascadeClassifier"):
                for n in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
                    c = cv2.CascadeClassifier(str(Path(base) / n))
                    if not c.empty():
                        self._haar.append(c)
            if not self._haar:
                raise BlurError(f"no face detector available ({e})") from None
            self.name = "OpenCV Haar cascades (frontal + profile)"

    def detect(self, img: np.ndarray) -> list[tuple[int, int, int, int]]:
        h, w = img.shape[:2]
        boxes = []
        if self._yunet is not None:
            # 1x full frame (large faces), then 2x in tiles: pedestrians in a sidewalk clip are often small and
            # YuNet misses faces under ~10 px
            s = UPSCALE if max(w, h) * UPSCALE <= 2600 else 1
            passes = [(1, img)] + ([(s, cv2.resize(img, (w * s, h * s), interpolation=cv2.INTER_LINEAR))] if s > 1 else [])
            for sc, im in passes:
                for tx, ty, tw, th in tiles(im.shape[1], im.shape[0], TILE_MAX_PX, TILE_OVERLAP):
                    self._yunet.setInputSize((tw, th))
                    _, faces = self._yunet.detect(np.ascontiguousarray(im[ty:ty + th, tx:tx + tw]))
                    for f in faces if faces is not None else []:
                        x, y, bw, bh = ((float(f[0]) + tx) / sc, (float(f[1]) + ty) / sc, float(f[2]) / sc, float(f[3]) / sc)
                        boxes.append((x, y, x + bw, y + bh))
            boxes = dedupe(boxes)
        else:
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            for c in self._haar:
                for flip in (False, True):  # the profile cascade only knows one side
                    g = cv2.flip(gray, 1) if flip else gray
                    for x, y, bw, bh in c.detectMultiScale(g, 1.1, 4, minSize=(16, 16)):
                        x = w - x - bw if flip else x
                        boxes.append((x, y, x + bw, y + bh))
        out = []
        for x0, y0, x1, y1 in boxes:
            px, py = (x1 - x0) * PAD_X, (y1 - y0) * PAD_Y
            b = (int(max(0, x0 - px)), int(max(0, y0 - py)), int(min(w, x1 + px)), int(min(h, y1 + py)))
            if b[2] > b[0] and b[3] > b[1]:
                out.append(b)
        return out


def tiles(w: int, h: int, max_px: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """(x, y, width, height) tiles covering a w x h image, each at most ~max_px pixels, neighbours overlapping."""
    side = max(64, int(max_px ** 0.5))
    nx, ny = max(1, -(-w // side)), max(1, -(-h // side))
    out = []
    for j in range(ny):
        for i in range(nx):
            x0, y0 = max(0, i * w // nx - overlap // 2), max(0, j * h // ny - overlap // 2)
            x1, y1 = min(w, (i + 1) * w // nx + overlap // 2), min(h, (j + 1) * h // ny + overlap // 2)
            out.append((x0, y0, x1 - x0, y1 - y0))
    return out


def dedupe(boxes: list, iou: float = 0.3) -> list:
    """Drop a box that overlaps a bigger kept one by more than iou (the same face seen in two passes or tiles)."""
    kept: list = []
    for b in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
        def ov(a):
            ix, iy = max(0.0, min(a[2], b[2]) - max(a[0], b[0])), max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            inter = ix * iy
            union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
            return inter / union if union > 0 else 0.0
        if all(ov(a) <= iou for a in kept):
            kept.append(b)
    return kept


def blur_boxes(img: np.ndarray, boxes) -> np.ndarray:
    """Pixelate then smooth each box in place (irreversible at this resolution)."""
    for x0, y0, x1, y1 in boxes:
        roi = img[y0:y1, x0:x1]
        bw, bh = x1 - x0, y1 - y0
        small = cv2.resize(roi, (max(1, bw // 12), max(1, bh // 12)), interpolation=cv2.INTER_AREA)
        k = max(3, (min(bw, bh) // 4) | 1)
        img[y0:y1, x0:x1] = cv2.GaussianBlur(cv2.resize(small, (bw, bh), interpolation=cv2.INTER_NEAREST), (k, k), 0)
    return img


def blur_video(src: str | Path, dst: str | Path, fps: float) -> dict:
    """Read every frame of src, blur faces, write an H.264 MP4 (no audio) to dst at the same fps and size.

    Returns {"frames", "frames_with_faces", "faces", "plates", "detector", "limitations"}.
    """
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise BlurError(f"cannot read {Path(src).name}")
    det = FaceDetector()
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    dst = Path(dst)
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    enc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
         "-r", f"{fps:g}", "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    n = with_faces = faces = 0
    carry: list[tuple[list, int]] = []  # (boxes, frames left)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            boxes = det.detect(frame)
            n += 1
            if boxes:
                with_faces += 1
                faces += len(boxes)
            todo = boxes + [b for bs, _ in carry for b in bs]
            blur_boxes(frame, todo)
            carry = [(bs, k - 1) for bs, k in carry if k > 1] + ([(boxes, CARRY_FRAMES)] if boxes else [])
            enc.stdin.write(np.ascontiguousarray(frame).tobytes())
        enc.stdin.close()
        err = enc.stderr.read().decode(errors="replace")
        rc = enc.wait()
    except BaseException:
        enc.kill()
        tmp.unlink(missing_ok=True)
        raise
    finally:
        cap.release()
    if rc != 0 or n == 0:
        tmp.unlink(missing_ok=True)
        raise BlurError(f"blurred clip could not be written ({n} frames read): {err.strip()[:200]}")
    tmp.replace(dst)
    return {"frames": n, "frames_with_faces": with_faces, "faces": faces, "plates": None, "detector": det.name,
            "upscale": UPSCALE, "score_threshold": SCORE_THRESHOLD,
            "limitations": ["Strong profile or turned-away faces, faces under ~10 px and motion-blurred faces can "
                            "be missed.", "Licence plates are not detected (no plate detector in OpenCV 5)."]}
