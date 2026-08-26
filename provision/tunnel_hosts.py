"""Resolve internal HTTP bases for tunnelled apps from VPN profile assignment."""
from __future__ import annotations

import os
import pathlib
import sys

_REPO = pathlib.Path(__file__).resolve().parents[1]
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
