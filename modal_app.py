"""Barrier Lens on Modal: the same FastAPI app as the Dockerfile, on a 4-CPU container so a live check (face blur on
every frame, then the Meta model calls) takes about a minute instead of several on a 0.1-CPU free host.

  modal secret create barrier-lens --from-dotenv <file OUTSIDE the repo, e.g. ~/.config/barrier-lens/modal.env, with
                                                  META_API_KEY, BL_DEMO_TOKEN, BL_SPEND_CAP_USD, SPARK_BACKEND>
  modal deploy modal_app.py

Keys live only in the Modal secret. Uploaded clips and the spend ledger are on the container's own disk, so they
go away when it scales down (the recorded sample ships with the image).
"""
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent  # paths work from any working directory
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(str(ROOT / "requirements.txt"))
    .workdir("/srv")
    .add_local_dir(ROOT / "app", "/srv/app", copy=True, ignore=["**/__pycache__/**"])
    .add_local_dir(ROOT / "static", "/srv/static", copy=True)
    .add_local_dir(ROOT / "data", "/srv/data", copy=True, ignore=["cache/**", "raw/**"])
    .run_commands("cd /srv && python -c 'from app import blur; print(blur.ensure_model())'")
)
app = modal.App("barrier-lens", image=image)


# max_containers: the spend cap and ledger live on each container's disk, so at most two caps can be spent at once
@app.function(secrets=[modal.Secret.from_name("barrier-lens")], cpu=4.0, memory=2048, timeout=900,
              scaledown_window=300, max_containers=2)
@modal.concurrent(max_inputs=4)
@modal.asgi_app()
def web():
    from app.main import app as fastapi_app
    return fastapi_app
