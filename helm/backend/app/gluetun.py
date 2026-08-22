"""Parsing gluetun's control-server responses.

Split out (like wireguard.py) so it's testable without FastAPI.
"""
import json


def parse_public_ip(raw: str) -> str | None:
    """gluetun's /v1/publicip/ip has returned both a bare IP and a JSON
    object across versions; take whichever this instance gives us.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("public_ip") or data.get("ip")
    except ValueError:
        return raw
