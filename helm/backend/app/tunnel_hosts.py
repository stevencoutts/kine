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
