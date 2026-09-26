"""Pi-hole beside the primary tunnel.

Untunnelled containers and the LAN use Pi-hole. Pi-hole's only upstream
is primary Gluetun, at a fixed address on the kine_dns bridge. Apps that
share a Gluetun namespace stay on that tunnel's own resolver.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess

SENTINEL = "change-me"
DEFAULT_SUBNET = "172.30.53.0/29"

# These core services have no Compose profile, so they are always running.
ALWAYS_RECREATE = ("traefik", "helm", "provision")
DNS_TARGETS: dict[str, tuple[str, ...]] = {
    "traefik": ("kine_edge", "kine_ctrl"),
    "helm": ("kine_edge", "kine_internal", "kine_ctrl"),
    "provision": ("kine_internal",),
    "emby": ("kine_internal", "kine_edge"),
    "tdarr": ("kine_internal", "kine_edge"),
    "beets": ("kine_internal", "kine_edge"),
    "seerr": ("kine_internal", "kine_edge"),
    "recyclarr": ("kine_internal",),
    "game-thumbs": ("kine_internal", "kine_edge"),
    "grafana": ("kine_internal", "kine_edge"),
    "prometheus": ("kine_internal",),
    "cadvisor": ("kine_internal", "kine_ctrl"),
    "node-exporter": ("kine_internal",),
}


class PiholeRejected(Exception):
    """Enable must stop. The message is safe to show the operator."""


def firewall_allows(firewall: str, subnet: str) -> bool:
    """True when ``subnet`` sits inside one of the comma-separated CIDRs."""
    target = ipaddress.ip_network(subnet.strip(), strict=False)
    for part in firewall.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        if target.subnet_of(net):
            return True
    return False


def password_update(current: str) -> str | None:
    """Hex secret when the value is empty or the example sentinel."""
    if current.strip() in ("", SENTINEL):
        import secrets
        return secrets.token_hex(16)
    return None


def port53_error(ss_text: str) -> str | None:
    """``ss -H -lntu sport = :53`` output. Empty means the port is free."""
    names: list[str] = []
    for line in ss_text.splitlines():
        if not line.strip():
            continue
        match = re.search(r'\("([^"]+)"', line)
        names.append(match.group(1) if match else "")
    if not names:
        return None
    shown = sorted({name for name in names if name})
    if not shown:
        return "TCP or UDP port 53 is already in use"
    return "TCP or UDP port 53 is already in use (" + ", ".join(shown) + ")"


def read_port53() -> str:
    try:
        proc = subprocess.run(
            ["ss", "-H", "-lntu", "sport", "=", ":53"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not check whether port 53 is free") from exc
    if proc.returncode != 0:
        raise RuntimeError("could not check whether port 53 is free")
    return proc.stdout


def rejection(env: dict, ss_text: str) -> str | None:
    subnet = (env.get("KINE_DNS_SUBNET") or DEFAULT_SUBNET).strip()
    firewall = env.get("FIREWALL_OUTBOUND_SUBNETS") or ""
    if not firewall_allows(firewall, subnet):
        return f"FIREWALL_OUTBOUND_SUBNETS must include {subnet}"
    return port53_error(ss_text)


def gate(env: dict, ss_text: str) -> dict[str, str]:
    """Password write, or raise ``PiholeRejected`` before the profile changes."""
    err = rejection(env, ss_text)
    if err:
        raise PiholeRejected(err)
    fresh = password_update(env.get("PIHOLE_WEBPASSWORD", ""))
    if fresh:
        return {"PIHOLE_WEBPASSWORD": fresh}
    return {}


def patches() -> dict[str, dict]:
    """Service fragments merged into the compose override while Pi-hole is on."""
    out: dict[str, dict] = {
        "gluetun": {
            "networks": {
                "kine_internal": {},
                "kine_edge": {},
                "kine_dns": {"ipv4_address": "${KINE_DNS_GLUETUN}"},
            },
        },
    }
    for name, nets in DNS_TARGETS.items():
        mapping = {net: {} for net in nets}
        mapping["kine_dns"] = {}
        out[name] = {
            "networks": mapping,
            "dns": ["${KINE_DNS_PIHOLE}"],
        }
    return out


def _profiles(env: dict) -> set[str]:
    return {p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",") if p.strip()}


def services_to_recreate(env: dict, *, store: dict | None = None) -> list[str]:
    """Pi-hole, enabled DNS consumers, and the tunnel group when the VPN is on.

    Gluetun is recreated with its peers. Recreating it alone drops every
    app that shares its network namespace.
    """
    profiles = _profiles(env)
    names: list[str] = []
    if "pihole" in profiles:
        names.append("pihole")
    for name in DNS_TARGETS:
        if name in ALWAYS_RECREATE or name in profiles:
            names.append(name)
    if "gluetun" in profiles and env.get("VPN_ENABLED") == "true":
        from . import vpn_profiles, vpn_routing

        if store is None:
            stack = env.get("STACK_ROOT") or "."
            store = vpn_profiles.migrate_from_wg0(stack)
        enabled = {
            app.strip()
            for app in env.get("VPN_TUNNELLED_APPS", "").split(",")
            if app.strip() and app.strip() in vpn_routing.APP_PORTS and app.strip() in profiles
        }
        for svc in vpn_routing.active_tunnel_services(store):
            names.extend(vpn_routing.recreate_group(store, svc, enabled))
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered
