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
    except ValueError:
        text = raw.strip()
        return text or None
    if not isinstance(data, dict):
        return None
    ip = data.get("public_ip") or data.get("ip")
    if isinstance(ip, str):
        ip = ip.strip()
    return ip or None


def parse_vpn_status(raw: str) -> str | None:
    """/v1/vpn/status stays running when the public IP lookup stored nothing."""
    raw = _payload(raw)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if not isinstance(status, str):
        return None
    status = status.strip().lower()
    return status or None


def parse_forwarded_port(raw: str) -> int | None:
    try:
        port = int(json.loads(_payload(raw)).get("port", 0))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return port or None


def coalesce_forwarded_port(*candidates: int | None) -> int | None:
    """Prefer Gluetun's live PF API, then a static provider port (Njalla)."""
    for port in candidates:
        try:
            value = int(port)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None
