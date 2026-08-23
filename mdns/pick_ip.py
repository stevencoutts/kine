"""Pick the LAN IPv4 address to advertise over mDNS.

Docker bridge addresses (172.16/12) and veth/br interfaces are skipped
so .local names resolve to the host LAN IP clients can actually reach.
"""
from __future__ import annotations

import ipaddress
import os
import subprocess
import sys

# Docker default and user-defined bridge networks live here.
_DOCKER_IPV4 = ipaddress.ip_network("172.16.0.0/12")


def _is_bad_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr in _DOCKER_IPV4
    )


def _is_bad_dev(dev: str) -> bool:
    return (
        dev == "lo"
        or dev.startswith(("docker", "veth", "br-"))
    )


def _run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)


def _default_route_dev() -> str | None:
    try:
        line = _run("ip", "-4", "route", "show", "default").splitlines()[0]
    except (subprocess.CalledProcessError, IndexError):
        return None
    parts = line.split()
    try:
        dev = parts[parts.index("dev") + 1]
    except (ValueError, IndexError):
        return None
    return None if _is_bad_dev(dev) else dev


def _ip_on_dev(dev: str) -> str | None:
    try:
        lines = _run("ip", "-4", "-o", "addr", "show", "dev", dev, "scope", "global").splitlines()
    except subprocess.CalledProcessError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip = parts[3].split("/", 1)[0]
        if not _is_bad_ip(ip):
            return ip
    return None


def _first_global_ip() -> str | None:
    try:
        lines = _run("ip", "-4", "-o", "addr", "show", "scope", "global").splitlines()
    except subprocess.CalledProcessError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        dev = parts[1].split("@", 1)[0]
        ip = parts[3].split("/", 1)[0]
        if _is_bad_dev(dev) or _is_bad_ip(ip):
            continue
        return ip
    return None


def pick_host_ip() -> str:
    override = os.environ.get("MDNS_HOST_IP") or os.environ.get("KINE_HOST_IP")
    if override and override.strip():
        return override.strip()

    dev = _default_route_dev()
    if dev:
        ip = _ip_on_dev(dev)
        if ip:
            return ip

    ip = _first_global_ip()
    if ip:
        return ip

    raise SystemExit("no suitable LAN IPv4 found; set MDNS_HOST_IP in .env")


if __name__ == "__main__":
    try:
        print(pick_host_ip())
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        raise
