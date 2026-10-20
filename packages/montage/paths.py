"""运行产物目录：cache / output / uploads 统一放在 var/ 下。

可通过环境变量 ``MONTAGE_VAR_DIR`` 覆盖根目录（默认 ``var``）。
"""

import os
from pathlib import Path


VAR_DIR = Path(os.getenv("MONTAGE_VAR_DIR", "var"))
CACHE_DIR = VAR_DIR / "cache"
OUTPUT_DIR = VAR_DIR / "output"
UPLOAD_DIR = VAR_DIR / "uploads"


def ensure_runtime_dirs() -> None:
    for p in (CACHE_DIR, OUTPUT_DIR, UPLOAD_DIR):
        p.mkdir(parents=True, exist_ok=True)
