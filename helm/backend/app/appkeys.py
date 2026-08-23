"""Resolve the API key each app will actually accept.

Keys are derived from KINE_SECRET so they are known before an app has
ever started, but seed adopts any pre-existing key rather than
overwriting it — so on-disk always wins over derived.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import xml.etree.ElementTree as ET

import yaml

from . import config

STACK = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))


def _secret() -> str:
    secret = os.environ.get("KINE_SECRET") or config.read().get("KINE_SECRET", "")
    if not secret:
        raise RuntimeError("KINE_SECRET is not set")
    return secret


def derived_key(app: str) -> str:
    return hashlib.sha256(f"{_secret()}:{app}".encode()).hexdigest()[:32]


def arr_key(app: str, stack: pathlib.Path | None = None) -> str | None:
    cfg = (stack or STACK) / "config" / app / "config.xml"
    if cfg.is_file():
        try:
            existing = ET.parse(cfg).getroot().findtext("ApiKey")
            if existing:
                return existing
        except ET.ParseError:
            pass
    try:
        return derived_key(app)
    except RuntimeError:
        return None


def bazarr_key(stack: pathlib.Path | None = None) -> str | None:
    root = stack or STACK
    for path in (
        root / "config" / "bazarr" / "config" / "config.yaml",
        root / "config" / "bazarr" / "config.yaml",
    ):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text()) or {}
            key = (data.get("auth") or {}).get("apikey")
            if key:
                return str(key)
        except (OSError, yaml.YAMLError):
            continue
    return None


def key_for(app: str, stack: pathlib.Path | None = None) -> str | None:
    return bazarr_key(stack) if app == "bazarr" else arr_key(app, stack)
