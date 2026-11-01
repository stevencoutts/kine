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

# Compose only ever names this service. No --force-recreate, no gluetun.
COMPOSE_UP = ("up", "-d", "--no-deps", "wg-access")
COMPOSE_UP_BUILD = ("up", "-d", "--no-deps", "--build", "wg-access")
COMPOSE_STOP = ("stop", "wg-access")
COMPOSE_RM = ("rm", "-f", "wg-access")
COMPOSE_ACTIONS = (COMPOSE_UP, COMPOSE_UP_BUILD, COMPOSE_STOP, COMPOSE_RM)

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


def validate_lan_ip(value: str) -> str:
    text = (value or "").strip()
    try:
        return str(ipaddress.IPv4Address(text))
    except ipaddress.AddressValueError as exc:
        raise ValueError("LAN address must be an IPv4 address of this machine") from exc


def validate_wan_host(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if ":" in text or "/" in text or " " in text:
        raise ValueError("Public address is a hostname or IPv4 address, without a port")
    try:
        return str(ipaddress.IPv4Address(text))
    except ipaddress.AddressValueError:
        pass
    if not _HOST.match(text):
        raise ValueError("Public address is not a hostname")
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


def service_names(domain: str, subdomains: list[str]) -> list[str]:
    """Names the laptop resolver answers with Kine's LAN address."""
    base = (domain or "kine.local").strip().strip(".").lower()
    names = {base, f"kine-admin.{base}"}
    for sub in subdomains:
        label = (sub or "").strip().strip(".").lower()
        if label:
            names.add(f"{label}.{base}")
    return sorted(names)


def fresh_state(lan_domain: str, subdomains: list[str]) -> dict:
    private, public = generate_keypair()
    domain = (lan_domain or "kine.local").strip().strip(".").lower() or "kine.local"
    return {
        "version": 1,
        "enabled": False,
        "lan_endpoint": "",
        "wan_endpoint": "",
        "host_port": INTERNAL_LISTEN_PORT,
        "lan_domain": domain,
        "names": service_names(domain, subdomains),
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
    lan_endpoint: str,
    wan_endpoint: str,
    host_port: object,
    lan_domain: str,
    subdomains: list[str],
) -> dict:
    """Return a new state. Enabling requires the LAN address."""
    lan = ""
    if (lan_endpoint or "").strip():
        lan = validate_lan_ip(lan_endpoint)
    if enabled and not lan:
        raise ValueError("LAN address is required before the client VPN can be turned on")
    domain = (lan_domain or state.get("lan_domain") or "kine.local").strip().strip(".").lower()
    if not domain or "." not in domain or " " in domain:
        raise ValueError("Name suffix must look like kine.local")
    updated = dict(state)
    updated["enabled"] = bool(enabled)
    updated["lan_endpoint"] = lan
    updated["wan_endpoint"] = validate_wan_host(wan_endpoint)
    updated["host_port"] = validate_port(host_port)
    updated["lan_domain"] = domain
    updated["names"] = service_names(domain, subdomains)
    return updated


def needs_compose(previous: dict, updated: dict) -> str | None:
    """Which compose action to run. None means leave containers alone.

    Peer edits are not a compose action: the access container reloads
    them. Turning the feature on or moving its host port is the only
    reason to call compose, and then only for wg-access.
    """
    if updated.get("enabled") and not previous.get("enabled"):
        return "build"
    if updated.get("enabled") and previous.get("host_port") != updated.get("host_port"):
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


def client_config(state: dict, peer: dict, where: str) -> str:
    if where == "lan":
        host = state.get("lan_endpoint") or ""
        if not host:
            raise ValueError("Set the LAN address first")
    elif where == "wan":
        host = state.get("wan_endpoint") or ""
        if not host:
            raise ValueError("Set the public address first")
    else:
        raise ValueError("Config must be home or away")
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


def config_filename(peer: dict, where: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(peer.get("name") or "device")).strip("-") or "device"
    side = "home" if where == "lan" else "away"
    return f"kine-{slug}-{side}.conf"


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
        "lan_endpoint": state.get("lan_endpoint") or "",
        "wan_endpoint": state.get("wan_endpoint") or "",
        "host_port": int(state.get("host_port") or INTERNAL_LISTEN_PORT),
        "lan_domain": state.get("lan_domain") or "kine.local",
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


def dns_reply(query: bytes, ipv4: str | None) -> bytes | None:
    """Answer an A query with ipv4, or an empty NOERROR for AAAA of the same name.

    ``ipv4 is None`` means this name is not one of ours; the caller forwards it.
    """
    parsed = parse_qname(query)
    if parsed is None:
        return None
    _name, end, qtype = parsed
    if ipv4 is None:
        return None
    header = bytearray(query[:2])
    header += (0x8180).to_bytes(2, "big")
    header += (1).to_bytes(2, "big")  # qd
    if qtype == 1:
        header += (1).to_bytes(2, "big")  # an
    else:
        header += (0).to_bytes(2, "big")
    header += (0).to_bytes(2, "big")  # ns
    header += (0).to_bytes(2, "big")  # ar
    question = query[12:end]
    if qtype != 1:
        return bytes(header) + question
    answer = b"\xc0\x0c" + (1).to_bytes(2, "big") + (1).to_bytes(2, "big")
    answer += (60).to_bytes(4, "big") + (4).to_bytes(2, "big")
    answer += ipaddress.IPv4Address(ipv4).packed
    return bytes(header) + question + answer


def socks5_udp_packet(dest_ip: str, dest_port: int, payload: bytes) -> bytes:
    ip = ipaddress.IPv4Address(dest_ip).packed
    return b"\x00\x00\x00\x01" + ip + int(dest_port).to_bytes(2, "big") + payload


def socks5_udp_payload(packet: bytes) -> bytes:
    if len(packet) < 10 or packet[2] != 0 or packet[3] != 1:
        raise ValueError("not a SOCKS5 IPv4 UDP packet")
    return packet[10:]
