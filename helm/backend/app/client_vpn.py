"""Laptop WireGuard in front of the VPN Kine already has.

Pure data and command plans. Nothing here talks to Docker or to gluetun.
The access container is the only process that enters gluetun's network
namespace, and it never restarts that container.

Streaming stays on the existing tunnel: compose actions name only
``wg-access``, and routing policy exists only for the laptop subnet.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import pathlib
import re
import uuid

CLIENT_CIDR = "10.66.66.0/24"
SERVER_ADDRESS = "10.66.66.1"
INTERNAL_LISTEN_PORT = 51820
SOCKS_PORT = 10808
TUN_ADDRESS = "172.31.255.1/30"
TUN_INTERFACE = "wgexit"
CLIENT_MTU = 1280
LAN_PREFIXES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
GLUETUN_HOSTNAME = "kine-gluetun"
HELPER_MARKERS = (b"/config/socks.json", b"/config/router.json")
SINGBOX_PATH = b"/usr/local/bin/sing-box"

# Compose only ever names this service. No build, no --force-recreate,
# no gluetun. The image is built on the host; the socket proxy cannot build.
COMPOSE_UP = ("up", "-d", "--no-deps", "wg-access")
COMPOSE_STOP = ("stop", "wg-access")
COMPOSE_RM = ("rm", "-f", "wg-access")
COMPOSE_ACTIONS = (COMPOSE_UP, COMPOSE_STOP, COMPOSE_RM)

_RESERVED_PORTS = {53, 80, 443, 8080, 8443, 8600}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")

_P = 2**255 - 19
_BASE = bytes([9]) + bytes(31)


def _x25519(scalar: bytes, point: bytes) -> bytes:
    """RFC 7748 X25519. Used so Helm can mint WireGuard keys without wg(1)."""
    clamped = bytearray(scalar)
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    k = int.from_bytes(clamped, "little")
    u_bytes = bytearray(point)
    u_bytes[31] &= 127
    x1 = int.from_bytes(u_bytes, "little")
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = (a * a) % _P
        b = (x2 - z2) % _P
        bb = (b * b) % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = (d * a) % _P
        cb = (c * b) % _P
        x3 = pow((da + cb) % _P, 2, _P)
        z3 = (x1 * pow((da - cb) % _P, 2, _P)) % _P
        x2 = (aa * bb) % _P
        z2 = (e * (aa + 121665 * e)) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    out = (x2 * pow(z2, _P - 2, _P)) % _P
    return out.to_bytes(32, "little")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    secret = bytearray(os.urandom(32))
    secret[0] &= 248
    secret[31] &= 127
    secret[31] |= 64
    raw = bytes(secret)
    return _b64(raw), _b64(_x25519(raw, _BASE))


def generate_preshared() -> str:
    return _b64(os.urandom(32))


def public_from_private(private_b64: str) -> str:
    raw = base64.b64decode(private_b64)
    if len(raw) != 32:
        raise ValueError("WireGuard private key must be 32 bytes")
    return _b64(_x25519(raw, _BASE))


def validate_endpoint_host(value: str) -> str:
    """Hostname that split DNS points at the internal or public address.

    The WireGuard config carries this name, never a raw address. Kine
    does not choose it from the app search domain.
    """
    text = (value or "").strip().strip(".").lower()
    if not text:
        return ""
    if ":" in text or "/" in text or " " in text:
        raise ValueError("Endpoint hostname is a hostname, without a port")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        raise ValueError("Endpoint hostname must be a hostname, not an IP address")
    if not _HOST.match(text) or "." not in text:
        raise ValueError("Endpoint hostname must be a hostname")
    return text


def validate_port(value: object) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("UDP port must be a number") from exc
    if port < 1024 or port > 65535 or port in _RESERVED_PORTS:
        raise ValueError("UDP port must be between 1024 and 65535 and not a port Kine already uses")
    return port


def validate_name(value: str) -> str:
    text = (value or "").strip()
    if not _NAME.match(text):
        raise ValueError("Device name must be 1–40 letters, numbers, spaces, dots, or dashes")
    return text


def fresh_state(lan_domain: str, subdomains: list[str] | None = None) -> dict:
    """New state. ``lan_domain`` is the internal zone, not a list of names.

    The second argument used to be catalogue labels that were answered
    with one address. It is ignored so older callers keep working.
    """
    del subdomains
    private, public = generate_keypair()
    domain = (lan_domain or "kine.local").strip().strip(".").lower() or "kine.local"
    return {
        "version": 1,
        "enabled": False,
        "endpoint_host": "",
        "host_port": INTERNAL_LISTEN_PORT,
        "lan_domain": domain,
        "server_private": private,
        "server_public": public,
        "peers": [],
    }


def state_path(stack_root: pathlib.Path) -> pathlib.Path:
    return stack_root / "config" / "wg-access" / "state.json"


def status_path(stack_root: pathlib.Path) -> pathlib.Path:
    return stack_root / "config" / "wg-access" / "status.json"


def load_state(path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    return data


def save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(state, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(blob)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _next_address(peers: list[dict]) -> str:
    used = set()
    for peer in peers:
        try:
            used.add(ipaddress.IPv4Address(str(peer.get("address"))))
        except ipaddress.AddressValueError:
            continue
    network = ipaddress.IPv4Network(CLIENT_CIDR)
    server = ipaddress.IPv4Address(SERVER_ADDRESS)
    for host in network.hosts():
        if host == server or host in used:
            continue
        return str(host)
    raise ValueError("No free addresses left in the client VPN subnet")


def add_peer(state: dict, name: str) -> dict:
    clean = validate_name(name)
    if any(str(peer.get("name", "")).lower() == clean.lower() for peer in state.get("peers") or []):
        raise ValueError(f"A device named {clean} already exists")
    private, public = generate_keypair()
    peer = {
        "id": uuid.uuid4().hex,
        "name": clean,
        "address": _next_address(list(state.get("peers") or [])),
        "private_key": private,
        "public_key": public,
        "preshared_key": generate_preshared(),
    }
    state.setdefault("peers", []).append(peer)
    return peer


def remove_peer(state: dict, peer_id: str) -> None:
    peers = list(state.get("peers") or [])
    kept = [peer for peer in peers if peer.get("id") != peer_id]
    if len(kept) == len(peers):
        raise ValueError("No device with that id")
    state["peers"] = kept


def find_peer(state: dict, peer_id: str) -> dict:
    for peer in state.get("peers") or []:
        if peer.get("id") == peer_id:
            return peer
    raise ValueError("No device with that id")


def apply_settings(
    state: dict,
    *,
    enabled: bool,
    host_port: object,
    endpoint_host: str | None = None,
) -> dict:
    """Return a new state. Enabling requires the endpoint hostname.

    Addresses are not inputs. A hostname already stored is left as it
    is when ``endpoint_host`` is omitted, including a placeholder the
    operator has not replaced yet. The internal zone stays whatever was
    stored; the form does not edit it.
    """
    updated = dict(state)
    if endpoint_host is None:
        host = str(state.get("endpoint_host") or "")
    else:
        host = validate_endpoint_host(endpoint_host)
    if enabled and not host:
        raise ValueError("Endpoint hostname is required before the client VPN can be turned on")
    updated["enabled"] = bool(enabled)
    updated["endpoint_host"] = host
    updated["host_port"] = validate_port(host_port)
    # Addresses are not settings. Drop them so a save cannot write a
    # placeholder public address back. The endpoint hostname is kept,
    # including a placeholder the operator has not replaced.
    for stale in ("lan_endpoint", "wan_endpoint", "names"):
        updated.pop(stale, None)
    return updated


def failed_start_state(previous: dict, updated: dict) -> dict:
    """Container did not start. Keep the hostname and devices already stored.

    Only the on/off flag returns to what it was.
    """
    kept = dict(updated)
    kept["enabled"] = bool(previous.get("enabled"))
    return kept


def needs_compose(previous: dict, updated: dict) -> str | None:
    """Which compose action to run. None means leave containers alone.

    Peer edits are not a compose action: the access container reloads
    them. Turning the feature on or moving its host port is the only
    reason to call compose, and then only to start wg-access. Never
    build: Helm's socket proxy rejects the build API.
    """
    turning_on = updated.get("enabled") and not previous.get("enabled")
    port_moved = updated.get("enabled") and previous.get("host_port") != updated.get("host_port")
    if turning_on or port_moved:
        return "up"
    if previous.get("enabled") and not updated.get("enabled"):
        return "stop"
    return None


def server_config(state: dict) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {state['server_private']}",
        f"ListenPort = {INTERNAL_LISTEN_PORT}",
        "",
    ]
    for peer in state.get("peers") or []:
        lines.extend([
            "[Peer]",
            f"PublicKey = {peer['public_key']}",
            f"PresharedKey = {peer['preshared_key']}",
            f"AllowedIPs = {peer['address']}/32",
            "",
        ])
    return "\n".join(lines)


def client_config(state: dict, peer: dict) -> str:
    """One config. Endpoint is the hostname the operator publishes in DNS."""
    host = str(state.get("endpoint_host") or "").strip()
    if not host:
        raise ValueError("Set the endpoint hostname first")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Endpoint hostname must be a hostname, not an IP address")
    port = int(state.get("host_port") or INTERNAL_LISTEN_PORT)
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {peer['private_key']}",
        f"Address = {peer['address']}/32",
        f"DNS = {SERVER_ADDRESS}",
        f"MTU = {CLIENT_MTU}",
        "",
        "[Peer]",
        f"PublicKey = {state['server_public']}",
        f"PresharedKey = {peer['preshared_key']}",
        f"Endpoint = {host}:{port}",
        "AllowedIPs = 0.0.0.0/0",
        "PersistentKeepalive = 25",
        "",
    ])


def config_filename(peer: dict) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(peer.get("name") or "device")).strip("-") or "device"
    return f"kine-{slug}.conf"


def summary(state: dict, status: dict | None) -> dict:
    enabled = bool(state.get("enabled"))
    exit_mode = "starting" if enabled else "off"
    detail = ""
    if enabled and isinstance(status, dict):
        exit_mode = str(status.get("exit") or "starting")
        detail = str(status.get("detail") or "")
    peers = []
    for peer in state.get("peers") or []:
        peers.append({
            "id": peer.get("id"),
            "name": peer.get("name"),
            "address": peer.get("address"),
        })
    return {
        "enabled": bool(state.get("enabled")),
        "endpoint_host": state.get("endpoint_host") or "",
        "host_port": int(state.get("host_port") or INTERNAL_LISTEN_PORT),
        "exit": exit_mode,
        "detail": detail,
        "peers": peers,
    }


def vpn_is_up(body: str) -> bool:
    text = (body or "").strip()
    if not text:
        return False
    line = [ln.strip() for ln in text.splitlines() if ln.strip()]
    raw = line[-1] if line else ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    return str(data.get("status") or "").strip().lower() == "running"


def socks_config(listen_ip: str) -> dict:
    ipaddress.IPv4Address(listen_ip)
    return {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "socks",
            "tag": "socks-in",
            "listen": listen_ip,
            "listen_port": SOCKS_PORT,
        }],
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }


def router_config(socks_ip: str) -> dict:
    """Tun for the laptop subnet only. auto_route stays off so this
    process cannot take the container's, the host's, or gluetun's default route.
    """
    ipaddress.IPv4Address(socks_ip)
    return {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "tun",
            "tag": "tun-in",
            "interface_name": TUN_INTERFACE,
            "address": [TUN_ADDRESS],
            "mtu": 1400,
            "auto_route": False,
            "strict_route": False,
        }],
        "outbounds": [{
            "type": "socks",
            "tag": "vpn",
            "server": socks_ip,
            "server_port": SOCKS_PORT,
        }],
        "route": {
            "auto_detect_interface": True,
            "final": "vpn",
        },
    }


def socks_process_args(pid: int) -> list[str]:
    """Enter gluetun's network namespace only. Never pid 1, and never
    a signal to that namespace's init.
    """
    if pid <= 1:
        raise ValueError("refusing to enter pid 1")
    return [
        "nsenter", "-t", str(pid), "-n", "--",
        "/usr/local/bin/sing-box", "run", "-c", "/config/socks.json",
    ]


def is_our_helper(pid: int, cmdline: bytes) -> bool:
    """True only for the sing-box processes this feature started."""
    if pid <= 1:
        return False
    text = cmdline.replace(b"\0", b" ")
    if SINGBOX_PATH not in text and b"sing-box" not in text:
        return False
    return any(marker in text for marker in HELPER_MARKERS)


def desired_routes(*, vpn_exit: bool, gateway: str, iface: str) -> dict:
    """Routes inside the access container, looked up only for the laptop subnet."""
    ipaddress.IPv4Address(gateway)
    if not iface or iface in {TUN_INTERFACE, "wg0", "lo"}:
        raise ValueError("gateway interface must be the container's normal NIC")
    routes = [
        {"dest": CLIENT_CIDR, "dev": "wg0"},
    ]
    for prefix in LAN_PREFIXES:
        routes.append({"dest": prefix, "via": gateway, "dev": iface})
    if vpn_exit:
        default = {"dest": "default", "dev": TUN_INTERFACE}
    else:
        default = {"dest": "default", "via": gateway, "dev": iface}
    return {
        "rule_from": CLIENT_CIDR,
        "table": 100,
        "routes": routes,
        "default": default,
        "nat": {"source": CLIENT_CIDR, "iface": iface},
    }


def hostname_is_gluetun(environ: bytes) -> bool:
    return b"HOSTNAME=" + GLUETUN_HOSTNAME.encode() in environ.split(b"\0")


def process_is_gluetun(environ: bytes, cmdline: bytes) -> bool:
    """True for the running kine-gluetun process.

    Compose does not set a hostname, so Docker names the container after
    its id and HOSTNAME=kine-gluetun is absent. The entrypoint binary is
    the mark that still identifies that process, which is the one whose
    network namespace the relay has to enter.
    """
    if hostname_is_gluetun(environ):
        return True
    text = cmdline.replace(b"\0", b" ").strip()
    return text == b"/gluetun-entrypoint" or text.startswith(b"/gluetun-entrypoint ")


def parse_qname(packet: bytes) -> tuple[str, int, int] | None:
    """Return (name, question end, qtype) for a single uncompressed question."""
    if len(packet) < 12:
        return None
    qd = int.from_bytes(packet[4:6], "big")
    if qd != 1:
        return None
    labels: list[str] = []
    offset = 12
    for _ in range(20):
        if offset >= len(packet):
            return None
        length = packet[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0:
            return None
        offset += 1
        if offset + length > len(packet):
            return None
        try:
            labels.append(packet[offset:offset + length].decode("ascii"))
        except UnicodeError:
            return None
        offset += length
    else:
        return None
    if offset + 4 > len(packet):
        return None
    qtype = int.from_bytes(packet[offset:offset + 2], "big")
    return ".".join(labels), offset + 4, qtype


_SKIP_IFACE = re.compile(
    r"^(lo|docker\d*|virbr\d*|tailscale\d*|veth.*|tun\d*|br-[0-9a-f]+|wg\d*|wgexit)$"
)


def internal_zones(lan_domain: str, resolv_text: str) -> list[str]:
    """Zones the laptop asks the internal DNS server about.

    The appliance domain is one. Search domains from the host resolver
    are the others. This is not a list of names to answer ourselves.
    """
    zones: list[str] = []

    def add(value: str) -> None:
        text = (value or "").strip().strip(".").lower()
        if text and "." in text and " " not in text and text not in zones:
            zones.append(text)

    add(lan_domain)
    for raw in (resolv_text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        parts = line.split()
        if not parts or parts[0].lower() not in {"search", "domain"}:
            continue
        for item in parts[1:]:
            add(item)
    return zones


def name_in_internal_zone(name: str, zones: list[str] | tuple[str, ...]) -> bool:
    key = (name or "").lower().rstrip(".")
    if not key:
        return False
    for zone in zones:
        base = (zone or "").lower().rstrip(".")
        if not base:
            continue
        if key == base or key.endswith("." + base):
            return True
    return False


def dns_exit(name: str, zones: list[str] | tuple[str, ...], *, vpn_exit: bool) -> str:
    """Where a laptop lookup is sent.

    ``internal`` is the LAN resolver, reached toward the internal network.
    It never follows the provider exit. ``vpn`` is that exit, and only for
    other names while the provider tunnel is up. ``plain`` is the internal
    network connection.
    """
    if name_in_internal_zone(name, zones):
        return "internal"
    if vpn_exit:
        return "vpn"
    return "plain"


def nameservers_from_resolv(text: str) -> list[str]:
    found: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.lower().startswith("nameserver"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            found.append(parts[1])
    return found


def internal_address_from_addrs(addr_text: str) -> str | None:
    """This machine's internal address, from ``ip -4 -o addr`` text.

    Docker bridges and the laptop tunnel are skipped. A route or a DNS
    query uses this only when the host resolver is on the machine itself.
    """
    private: list[str] = []
    other: list[str] = []
    for raw in (addr_text or "").splitlines():
        match = re.search(
            r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/",
            raw.strip(),
        )
        if not match:
            continue
        iface, ip = match.group(1), match.group(2)
        if _SKIP_IFACE.match(iface):
            continue
        try:
            addr = ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        if addr.is_loopback or addr.is_link_local or addr.is_multicast:
            continue
        if addr.is_private:
            private.append(str(addr))
        else:
            other.append(str(addr))
    if private:
        return private[0]
    return other[0] if other else None


def internal_resolver(resolv_text: str, addr_text: str) -> str | None:
    """Address of the DNS server the host uses for internal names.

    A private nameserver in the host resolver is that server. Loopback
    and public resolvers are not: loopback means this machine is the
    resolver, so the query goes to the machine's internal address.
    """
    for raw in nameservers_from_resolv(resolv_text):
        try:
            ip = ipaddress.IPv4Address(raw)
        except ipaddress.AddressValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            continue
        if ip.is_private:
            return str(ip)
    return internal_address_from_addrs(addr_text)


def socks5_udp_packet(dest_ip: str, dest_port: int, payload: bytes) -> bytes:
    ip = ipaddress.IPv4Address(dest_ip).packed
    return b"\x00\x00\x00\x01" + ip + int(dest_port).to_bytes(2, "big") + payload


def socks5_udp_payload(packet: bytes) -> bytes:
    if len(packet) < 10 or packet[2] != 0 or packet[3] != 1:
        raise ValueError("not a SOCKS5 IPv4 UDP packet")
    return packet[10:]
