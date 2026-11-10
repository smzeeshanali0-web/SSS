from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    (d / "samples").mkdir(parents=True, exist_ok=True)
    return d


def api_key() -> str:
    return os.getenv("API_KEY", "")


MAX_UPLOAD_MB = lambda: int(os.getenv("MAX_UPLOAD_MB", "20"))
ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
