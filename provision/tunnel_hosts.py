"""Resolve internal HTTP bases for tunnelled apps from VPN profile assignment."""
from __future__ import annotations

import os
import pathlib
import sys
from urllib.parse import urlparse, urlunparse

_REPO = pathlib.Path(
    os.environ.get("KINE_REPO")
    or pathlib.Path(__file__).resolve().parents[1]
)
_HELM_BACKEND = _REPO / "helm" / "backend"
if str(_HELM_BACKEND) not in sys.path:
    sys.path.insert(0, str(_HELM_BACKEND))

from app import vpn_profiles  # noqa: E402


def _stack_root() -> str:
    return os.environ.get("KINE_ROOT", "/stack")


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
