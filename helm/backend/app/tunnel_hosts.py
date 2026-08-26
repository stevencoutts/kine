"""Resolve internal HTTP bases for tunnelled apps from VPN profile assignment."""
from __future__ import annotations

import os

from . import config, vpn_profiles


def _stack_root() -> str:
    env = config.read()
    return env.get("STACK_ROOT") or os.environ.get("KINE_ROOT", "/stack")


def load_profiles() -> dict:
    return vpn_profiles.load(_stack_root())


def internal_base(data: dict, app_id: str, port: int) -> str:
    host = vpn_profiles.tunnel_service(data, app_id)
    return f"http://{host}:{port}"


def internal_base_for_app(app_id: str, port: int) -> str:
    return internal_base(load_profiles(), app_id, port)


def runtime_internal(app_id: str, entry: dict) -> str:
    """Catalogue internal URLs are docs defaults; resolve live tunnel host when tunnelled."""
    from urllib.parse import urlparse

    doc_internal = (entry.get("internal") or "").strip()
    if not doc_internal:
        return ""
    if entry.get("tunnelled") != "forced":
        return doc_internal.rstrip("/")
    port = urlparse(doc_internal).port
    if not port:
        return doc_internal.rstrip("/")
    return internal_base_for_app(app_id, port)
