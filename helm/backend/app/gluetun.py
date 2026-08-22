"""Parsing gluetun's control-server responses.

Split out (like wireguard.py) so it's testable without FastAPI.
"""
import json


def connection_label(vpn_type: str) -> str:
    kind = vpn_type.strip().lower()
    return {"wireguard": "WireGuard", "openvpn": "OpenVPN"}.get(
        kind, vpn_type.strip() or "VPN"
    )


def _payload(raw: str) -> str:
    """Return the API payload after any Docker Compose warnings."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def parse_public_ip(raw: str) -> str | None:
    """gluetun's /v1/publicip/ip has returned both a bare IP and a JSON
    object across versions; take whichever this instance gives us.
    """
    raw = _payload(raw)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("public_ip") or data.get("ip")
    except ValueError:
        return raw


def parse_forwarded_port(raw: str) -> int | None:
    try:
        port = int(json.loads(_payload(raw)).get("port", 0))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return port or None
