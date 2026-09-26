"""Pi-hole beside the primary tunnel.

Untunnelled containers and the LAN use Pi-hole. Pi-hole's only upstream
is primary Gluetun, at a fixed address on the kine_dns bridge. Apps that
share a Gluetun namespace stay on that tunnel's own resolver.
"""
from __future__ import annotations

import ipaddress
import pathlib
import re
import subprocess

from . import config

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


# Docker bridge ids, libvirt, and Tailscale are not where LAN clients look up DNS.
_SKIP_IFACE = re.compile(
    r"^(lo|docker0|virbr\d*|tailscale\d*|veth.*|tun\d*|br-[0-9a-f]{12})$"
)
_WILDCARD_ADDRS = {"0.0.0.0", "*", "::"}


def _listeners(ss_text: str) -> list[tuple[str, str]]:
    """``(address, process)`` rows from ``ss -H -lntup sport = :53``."""
    found: list[tuple[str, str]] = []
    for line in ss_text.splitlines():
        if not line.strip():
            continue
        local = ""
        for part in line.split():
            if part.endswith(":53") or part.endswith("]:53"):
                local = part
                break
        if not local:
            continue
        if local.startswith("["):
            addr = local[1:].split("]", 1)[0]
        else:
            addr = local.rsplit(":", 1)[0]
        match = re.search(r'\("([^"]+)"', line)
        found.append((addr, match.group(1) if match else ""))
    return found


def publishable_addrs(addr_text: str) -> list[str]:
    """Global IPv4s on interfaces a LAN client would use as the DNS host."""
    ips: list[str] = []
    for line in addr_text.splitlines():
        match = re.search(
            r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/",
            line.strip(),
        )
        if not match:
            continue
        iface, ip = match.group(1), match.group(2)
        if _SKIP_IFACE.match(iface):
            continue
        ips.append(ip)
    return ips


def bind_addresses(ss_text: str, addr_text: str = "") -> list[str]:
    """Host IPs that can take port 53.

    An empty list means nothing is listening, so ``0.0.0.0:53`` is fine.
    A listener on one address (libvirt's dnsmasq on virbr0) does not block
    the others. A wildcard listener blocks every address.
    """
    listeners = _listeners(ss_text)
    if not listeners:
        return []
    taken = {addr for addr, _ in listeners}
    if taken & _WILDCARD_ADDRS:
        raise PiholeRejected(
            port53_error(ss_text) or "TCP or UDP port 53 is already in use"
        )
    free = [ip for ip in publishable_addrs(addr_text) if ip not in taken]
    if not free:
        raise PiholeRejected(
            port53_error(ss_text) or "TCP or UDP port 53 is already in use"
        )
    return free


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


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _host_dns_failed(exc: Exception | None = None) -> RuntimeError:
    err = RuntimeError("could not check whether port 53 is free")
    if exc is not None:
        raise err from exc
    return err


def read_host_dns() -> tuple[str, str]:
    """Host ``ss`` listeners on port 53, and global IPv4 addresses.

    Helm does not share the host network, and its image is what knows
    ``ss``. A one-shot of that image on the host network sees the sockets
    Docker will actually try to publish.
    """
    try:
        if pathlib.Path("/.dockerenv").is_file():
            proc = _run(
                [
                    "docker", "run", "--rm",
                    "--network", "host", "--pid", "host",
                    "--entrypoint", "sh", "kine/helm:local",
                    "-c",
                    "ss -H -lntup sport = :53; printf '\\n---ADDR---\\n'; "
                    "ip -4 -o addr show scope global",
                ],
                20,
            )
            if proc.returncode != 0:
                raise _host_dns_failed()
            ss_text, _, addr_text = proc.stdout.partition("\n---ADDR---\n")
            return ss_text, addr_text
        ss = _run(["ss", "-H", "-lntup", "sport", "=", ":53"], 5)
        addr = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"], 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _host_dns_failed(exc) from exc
    if ss.returncode != 0 or addr.returncode != 0:
        raise _host_dns_failed()
    return ss.stdout, addr.stdout


def rejection(env: dict, ss_text: str, addr_text: str = "") -> str | None:
    subnet = (env.get("KINE_DNS_SUBNET") or DEFAULT_SUBNET).strip()
    firewall = env.get("FIREWALL_OUTBOUND_SUBNETS") or ""
    if not firewall_allows(firewall, subnet):
        return f"FIREWALL_OUTBOUND_SUBNETS must include {subnet}"
    try:
        bind_addresses(ss_text, addr_text)
    except PiholeRejected as exc:
        return str(exc)
    return None


def gate(env: dict, ss_text: str, addr_text: str = "") -> dict[str, str]:
    """Env writes, or raise ``PiholeRejected`` before the profile changes."""
    err = rejection(env, ss_text, addr_text)
    if err:
        raise PiholeRejected(err)
    updates: dict[str, str] = {}
    fresh = password_update(env.get("PIHOLE_WEBPASSWORD", ""))
    if fresh:
        updates["PIHOLE_WEBPASSWORD"] = fresh
    bind = bind_addresses(ss_text, addr_text)
    if bind:
        updates["KINE_DNS_BIND"] = ",".join(bind)
    elif (env.get("KINE_DNS_BIND") or "").strip():
        updates["KINE_DNS_BIND"] = ""
    return updates


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
    bind = (config.read().get("KINE_DNS_BIND") or "").strip()
    if bind:
        ports: list[str] = []
        for ip in bind.split(","):
            ip = ip.strip()
            if not ip:
                continue
            ports.append(f"{ip}:53:53/tcp")
            ports.append(f"{ip}:53:53/udp")
        if ports:
            out["pihole"] = {"ports": ports}
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
