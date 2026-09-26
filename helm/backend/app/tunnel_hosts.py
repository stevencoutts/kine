"""Resolve internal HTTP bases for tunnelled apps from VPN profile assignment."""
from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

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


def sibling_base(data: dict, app_id: str, port: int) -> str:
    """URL a Live TV container should use to reach a peer.

    Inside a shared Gluetun namespace that is loopback. Direct mode gives
    each app its own network, so the Docker DNS name is the only address
    that still reaches the peer.
    """
    if vpn_profiles.app_goes_direct(data, app_id):
        return f"http://{app_id}:{port}"
    return f"http://127.0.0.1:{port}"


def align_peer_url(url: str, data: dict, app_id: str, port: int) -> str | None:
    """Correct a stored loopback or service URL for the current mode.

    Returns None when ``url`` does not address this peer, or already matches.
    """
    parsed = urlparse((url or "").strip())
    if parsed.port != port:
        return None
    if (parsed.hostname or "") not in {"127.0.0.1", "localhost", app_id}:
        return None
    origin = urlparse(sibling_base(data, app_id, port))
    fixed = urlunparse(parsed._replace(scheme=origin.scheme, netloc=origin.netloc))
    if fixed.rstrip("/") == (url or "").strip().rstrip("/"):
        return None
    return fixed


def runtime_internal(app_id: str, entry: dict) -> str:
    """Catalogue internal URLs are docs defaults; resolve live tunnel host when tunnelled."""
    doc_internal = (entry.get("internal") or "").strip()
    if not doc_internal:
        return ""
    if entry.get("tunnelled") != "forced":
        return doc_internal.rstrip("/")
    port = urlparse(doc_internal).port
    if not port:
        return doc_internal.rstrip("/")
    return internal_base_for_app(app_id, port)
