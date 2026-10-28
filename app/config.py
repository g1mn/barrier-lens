"""Runtime settings. Keys come from the environment, falling back to ~/.config/barrier-lens/.env for local runs."""
import os
from pathlib import Path

_ENV_FILE = Path.home() / ".config" / "barrier-lens" / ".env"


def _load_env_file() -> None:
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env_file()


def key(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


BASETEN_BASE_URL = "https://inference.baseten.co/v1"
MUSE_SPARK_OPEN = "meta-models/muse-spark-open-1026"
