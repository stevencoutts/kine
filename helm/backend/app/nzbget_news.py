"""Thin wrapper so Helm can reuse the provision NZBGet recipe."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(os.environ.get("KINE_REPO", "/repo"))


def recipe():
    provision = str(_REPO / "provision")
    if provision not in sys.path:
        sys.path.insert(0, provision)
    from recipes import nzbget  # noqa: PLC0415 — lazy to keep Helm import light

    return nzbget
