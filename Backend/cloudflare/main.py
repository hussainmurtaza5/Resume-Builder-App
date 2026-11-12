"""Vercel entrypoint for the Resume Builder FastAPI application."""

import sys
from pathlib import Path

_backend_src = str(Path(__file__).resolve().parent / "src")
if _backend_src not in sys.path:
    sys.path.insert(0, _backend_src)

from routers import app  # noqa: E402

__all__ = ["app"]

