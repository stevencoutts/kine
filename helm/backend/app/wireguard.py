"""Parsing a client WireGuard .conf for onboarding.

Named providers (protonvpn, mullvad, ...) only need a private key and
tunnel address. A full peer config with an IP ``Endpoint`` is treated as
gluetun's ``custom`` provider.
"""
from __future__ import annotations

import ipaddress
import re


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_conf(text: str) -> dict[str, str]:
    """Extract WireGuard fields from a client ``.conf``.

    Returns gluetun environment keys. When the peer ``Endpoint`` is an IP
    address and a peer ``PublicKey`` is present, also sets
    ``VPN_SERVICE_PROVIDER=custom`` plus endpoint fields. Otherwise only
    private key and address are returned (named-provider mode).
    """
    interface: dict[str, str] = {}
    peer: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        key_l = key.lower()
        if section == "interface":
            if key_l == "privatekey":
                interface["privatekey"] = value
            elif key_l == "address":
                interface["address"] = value.split(",")[0].strip()
        elif section == "peer":
            if key_l == "publickey":
                peer["publickey"] = value
            elif key_l == "presharedkey":
                peer["presharedkey"] = value
            elif key_l == "endpoint":
                peer["endpoint"] = value

    if "privatekey" not in interface:
        return {}

    out: dict[str, str] = {"WIREGUARD_PRIVATE_KEY": interface["privatekey"]}
    if "address" in interface:
        out["WIREGUARD_ADDRESSES"] = interface["address"]

    endpoint = peer.get("endpoint", "")
    public_key = peer.get("publickey", "")
    if not endpoint or not public_key:
        return out

    host, sep, port = endpoint.rpartition(":")
    if not sep or not host or not port:
        raise ValueError("WireGuard Endpoint must look like host:port")
    host = host.strip().strip("[]")
    if not re.fullmatch(r"\d{1,5}", port):
        raise ValueError(f"invalid WireGuard Endpoint port: {port}")
    if not _is_ip(host):
        # Hostname endpoints are for named providers (Proton, etc.) where
        # gluetun picks the server. Ignore the peer block.
        return out

    out.update(
        {
            "VPN_SERVICE_PROVIDER": "custom",
            "VPN_PORT_FORWARDING": "off",
            "WIREGUARD_ENDPOINT_IP": host,
            "WIREGUARD_ENDPOINT_PORT": port,
            "WIREGUARD_PUBLIC_KEY": public_key,
        }
    )
    if "presharedkey" in peer:
        out["WIREGUARD_PRESHARED_KEY"] = peer["presharedkey"]
    return out


def proton_clears() -> dict[str, str]:
    """Env keys to blank when switching to gluetun ``custom`` WireGuard."""
    return {
        "VPN_SERVER_COUNTRIES": "",
        "VPN_PORT_FORWARDING_PROVIDER": "",
    }


def write_gluetun_conf(text: str, stack_root: str, *, custom: bool) -> None:
    """Persist or remove ``wg0.conf`` under ``${STACK_ROOT}/config/gluetun``."""
    import pathlib

    root = pathlib.Path(stack_root) / "config" / "gluetun" / "wireguard"
    conf = root / "wg0.conf"
    if custom:
        root.mkdir(parents=True, exist_ok=True)
        conf.write_text(text.strip() + "\n")
    elif conf.exists():
        conf.unlink()
