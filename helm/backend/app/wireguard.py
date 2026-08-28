"""Parsing a client WireGuard .conf for onboarding.

Kine always runs gluetun in ``custom`` mode using the pasted ``wg0.conf``.
No named-provider defaults (Proton, Mullvad, …) are applied.
"""
from __future__ import annotations

import ipaddress
import pathlib
import re

VPN_ENV_KEYS = (
    "VPN_SERVICE_PROVIDER",
    "VPN_TYPE",
    "VPN_PORT_FORWARDING",
    "VPN_SERVER_COUNTRIES",
    "VPN_PORT_FORWARDING_PROVIDER",
    "FIREWALL_VPN_INPUT_PORTS",
    "WIREGUARD_PRIVATE_KEY",
    "WIREGUARD_ADDRESSES",
    "WIREGUARD_PUBLIC_KEY",
    "WIREGUARD_PRESHARED_KEY",
    "WIREGUARD_ENDPOINT_IP",
    "WIREGUARD_ENDPOINT_PORT",
)


def empty_vpn_env() -> dict[str, str]:
    """Blank every VPN credential key (used when VPN is disabled)."""
    return {key: "" for key in VPN_ENV_KEYS}


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def parse_conf(text: str) -> dict[str, str]:
    """Validate a pasted WireGuard ``.conf`` and map it to gluetun env keys.

    Requires ``[Interface]`` PrivateKey and Address, plus ``[Peer]``
    PublicKey and Endpoint. Always selects gluetun ``custom`` mode; the
    full config is also written to ``wg0.conf``.
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

    if "address" not in interface:
        raise ValueError("WireGuard config must include Address under [Interface]")

    public_key = peer.get("publickey", "")
    endpoint = peer.get("endpoint", "")
    if not public_key or not endpoint:
        raise ValueError(
            "WireGuard config must include [Peer] PublicKey and Endpoint"
        )

    host, sep, port = endpoint.rpartition(":")
    if not sep or not host or not port:
        raise ValueError("WireGuard Endpoint must look like host:port")
    host = host.strip().strip("[]")
    if not re.fullmatch(r"\d{1,5}", port):
        raise ValueError(f"invalid WireGuard Endpoint port: {port}")

    out = empty_vpn_env()
    out.update(
        {
            "VPN_SERVICE_PROVIDER": "custom",
            "VPN_TYPE": "wireguard",
            "VPN_PORT_FORWARDING": "off",
            "WIREGUARD_PRIVATE_KEY": interface["privatekey"],
            "WIREGUARD_ADDRESSES": interface["address"],
            "WIREGUARD_PUBLIC_KEY": public_key,
        }
    )
    if "presharedkey" in peer:
        out["WIREGUARD_PRESHARED_KEY"] = peer["presharedkey"]
    if _is_ip(host):
        out["WIREGUARD_ENDPOINT_IP"] = host
        out["WIREGUARD_ENDPOINT_PORT"] = port
    return out


def write_gluetun_conf_at(text: str, conf_dir: pathlib.Path) -> None:
    """Persist ``wg0.conf`` under ``conf_dir/wireguard/``."""
    root = pathlib.Path(conf_dir) / "wireguard"
    root.mkdir(parents=True, exist_ok=True)
    (root / "wg0.conf").write_text(text.strip() + "\n")


def write_gluetun_conf(text: str, stack_root: str) -> None:
    """Persist the pasted config as ``wg0.conf`` under ``${STACK_ROOT}/config/gluetun``."""
    write_gluetun_conf_at(text, pathlib.Path(stack_root) / "config" / "gluetun")


def write_secondary_conf(stack_root: str, short_id: str, text: str) -> None:
    """Persist secondary tunnel conf under ``config/gluetun-<shortId>/wireguard/``."""
    write_gluetun_conf_at(
        text, pathlib.Path(stack_root) / "config" / f"gluetun-{short_id}"
    )


def remove_gluetun_conf(stack_root: str) -> None:
    """Remove ``wg0.conf`` when VPN is disabled."""
    conf = pathlib.Path(stack_root) / "config" / "gluetun" / "wireguard" / "wg0.conf"
    if conf.exists():
        conf.unlink()
