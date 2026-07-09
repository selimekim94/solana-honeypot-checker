"""Minimal .env loader (no python-dotenv dependency)."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> Path | None:
    """
    Load KEY=VALUE pairs from a .env file into os.environ.

    - Skips blank lines and # comments
    - Strips optional surrounding quotes
    - By default does not override already-set env vars
    - Searches script dir then cwd if path is None
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        # caller package / project root
        here = Path(__file__).resolve().parent
        candidates.append(here / ".env")
        candidates.append(Path.cwd() / ".env")

    env_path = next((p for p in candidates if p.is_file()), None)
    if env_path is None:
        return None

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # strip matching quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if override or key not in os.environ or os.environ.get(key, "") == "":
            os.environ[key] = val
    return env_path
