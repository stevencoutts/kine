"""Client VPN: WireGuard for devices, in front of the VPN Kine already has.

Pure data and command plans. Nothing here talks to Docker or to gluetun.
The access container is the only process that enters gluetun's network
namespace, and it never restarts that container.

Streaming stays on the existing tunnel: compose actions name only
``wg-access``, and routing policy exists only for the client subnet.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import pathlib
import re
import uuid

# Default client subnet. A saved ``client_cidr`` replaces it; the server
# is always the first host address and devices take the ones after it.
CLIENT_CIDR = "10.66.66.0/24"
SERVER_ADDRESS = "10.66.66.1"
CLIENT_PREFIX_MIN = 16
CLIENT_PREFIX_MAX = 29
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
        "client_cidr": CLIENT_CIDR,
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


def client_network(state: dict) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(str(state.get("client_cidr") or CLIENT_CIDR))


def server_address(state: dict) -> str:
    return str(next(client_network(state).hosts()))


def _next_address(peers: list[dict], network: ipaddress.IPv4Network) -> str:
    used = set()
    for peer in peers:
        try:
            used.add(ipaddress.IPv4Address(str(peer.get("address"))))
        except ipaddress.AddressValueError:
            continue
    hosts = network.hosts()
    next(hosts)
    for host in hosts:
        if host not in used:
            return str(host)
    raise ValueError("No free addresses left in the client VPN subnet")


def reserved_networks(
    *,
    docker_text: str = "",
    env: dict | None = None,
    status: dict | None = None,
) -> list[tuple[str, str]]:
    """Networks a client subnet must not overlap, as (label, cidr).

    ``docker_text`` is ``docker network inspect`` output, one
    ``name subnet...`` line per network. ``status`` is what wg-access
    last reported about the host's own interfaces.
    """
    env = env or {}
    found: list[tuple[str, str]] = []

    def add(label: str, value: str, *, strict: bool = True) -> None:
        try:
            net = ipaddress.ip_network(str(value).strip(), strict=strict)
        except ValueError:
            return
        if net.version == 4:
            found.append((label, str(net)))

    for raw in (docker_text or "").splitlines():
        parts = raw.split()
        for subnet in parts[1:]:
            add(parts[0], subnet)
    add("kine_dns (KINE_DNS_SUBNET)", env.get("KINE_DNS_SUBNET") or "")
    for item in str(env.get("WIREGUARD_ADDRESSES") or "").split(","):
        add("the gluetun tunnel address", item, strict=False)
    if isinstance(status, dict):
        for entry in status.get("host_networks") or []:
            if isinstance(entry, dict):
                add(f"the host network on {entry.get('name') or 'an interface'}", str(entry.get("cidr") or ""))
    if env.get("KINE_LAN_IP"):
        add("the host's internal address (KINE_LAN_IP)", f"{env['KINE_LAN_IP']}/32")
    add("the access container's tun link", TUN_ADDRESS, strict=False)
    return found


def validate_client_cidr(
    value: str,
    reserved: list[tuple[str, str]] | tuple = (),
    *,
    devices: int = 0,
) -> str:
    """A private IPv4 network for the server plus ``devices`` clients."""
    text = (value or "").strip()
    try:
        net = ipaddress.IPv4Network(text)
    except ipaddress.NetmaskValueError as exc:
        raise ValueError("Client subnet must be an IPv4 network like 10.66.66.0/24") from exc
    except ValueError as exc:
        try:
            loose = ipaddress.IPv4Network(text, strict=False)
        except ValueError:
            raise ValueError("Client subnet must be an IPv4 network like 10.66.66.0/24") from exc
        raise ValueError(f"Client subnet must start on its network address: {loose}") from exc
    if "/" not in text:
        raise ValueError("Client subnet needs a prefix length, like 10.66.66.0/24")
    if not CLIENT_PREFIX_MIN <= net.prefixlen <= CLIENT_PREFIX_MAX:
        raise ValueError(
            f"Client subnet must be between /{CLIENT_PREFIX_MIN} and /{CLIENT_PREFIX_MAX}"
        )
    private = [ipaddress.IPv4Network(p) for p in LAN_PREFIXES]
    if not any(net.subnet_of(p) for p in private):
        raise ValueError("Client subnet must be a private network inside 10.0.0.0/8, 172.16.0.0/12 or 192.168.0.0/16")
    if net in private:
        raise ValueError(f"Client subnet must be smaller than {net}, which is a whole private range")
    needed = 1 + max(devices, 1)
    if net.num_addresses - 2 < needed:
        raise ValueError(
            f"Client subnet {net} is too small for the server and {max(devices, 1)} device(s)"
        )
    for label, cidr in reserved:
        try:
            other = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            continue
        if net.overlaps(other):
            raise ValueError(f"Client subnet {net} overlaps {label} ({other})")
    return str(net)


def readdress(state: dict, cidr: str) -> dict:
    """Move every device into ``cidr``. Keys stay; each config must be fetched again."""
    updated = dict(state)
    updated["client_cidr"] = cidr
    network = ipaddress.IPv4Network(cidr)
    moved: list[dict] = []
    for peer in state.get("peers") or []:
        entry = dict(peer)
        entry["address"] = _next_address(moved, network)
        entry["needs_config"] = True
        moved.append(entry)
    updated["peers"] = moved
    return updated


def add_peer(state: dict, name: str) -> dict:
    clean = validate_name(name)
    if any(str(peer.get("name", "")).lower() == clean.lower() for peer in state.get("peers") or []):
        raise ValueError(f"A device named {clean} already exists")
    private, public = generate_keypair()
    peer = {
        "id": uuid.uuid4().hex,
        "name": clean,
        "address": _next_address(list(state.get("peers") or []), client_network(state)),
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


def config_fetched(state: dict, peer_id: str) -> bool:
    """Clear the re-download notice. True when the state changed."""
    peer = find_peer(state, peer_id)
    if not peer.get("needs_config"):
        return False
    peer.pop("needs_config", None)
    return True


def apply_settings(
    state: dict,
    *,
    enabled: bool,
    host_port: object,
    endpoint_host: str | None = None,
    client_cidr: str | None = None,
    reserved: list[tuple[str, str]] | tuple = (),
) -> dict:
    """Return a new state. Enabling requires the endpoint hostname.

    Endpoint addresses are not inputs. A hostname already stored is left
    as it is when ``endpoint_host`` is omitted, including a placeholder
    the operator has not replaced yet. The internal zone stays whatever
    was stored; the form does not edit it.

    A new ``client_cidr`` is checked against ``reserved`` and moves
    every device into it. The same subnet again is not a change.
    """
    updated = dict(state)
    current = str(client_network(state))
    if client_cidr is not None and client_cidr.strip() and client_cidr.strip() != current:
        cidr = validate_client_cidr(
            client_cidr, reserved, devices=len(state.get("peers") or []),
        )
        if cidr != current:
            updated = readdress(updated, cidr)
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

    Only the on/off flag returns to what it was. A failed recreate has
    already removed wg-access, so it is off, and the next save starts it.
    """
    kept = dict(updated)
    if needs_compose(previous, updated) == "recreate":
        kept["enabled"] = False
    else:
        kept["enabled"] = bool(previous.get("enabled"))
    return kept


def needs_compose(previous: dict, updated: dict) -> str | None:
    """Which compose action to run. None means leave containers alone.

    Peer edits are not a compose action: the access container reloads
    them. Turning the feature on or moving its host port is the only
    reason to start wg-access. A new client subnet while it is running
    recreates wg-access, so no route, rule or address from the old
    subnet is left in its network namespace. Never build: Helm's socket
    proxy rejects the build API.
    """
    turning_on = updated.get("enabled") and not previous.get("enabled")
    port_moved = updated.get("enabled") and previous.get("host_port") != updated.get("host_port")
    subnet_moved = client_network(previous) != client_network(updated)
    if previous.get("enabled") and updated.get("enabled") and subnet_moved:
        return "recreate"
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
        f"DNS = {server_address(state)}",
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
            "needs_config": bool(peer.get("needs_config")),
        })
    return {
        "enabled": bool(state.get("enabled")),
        "endpoint_host": state.get("endpoint_host") or "",
        "host_port": int(state.get("host_port") or INTERNAL_LISTEN_PORT),
        "client_cidr": str(client_network(state)),
        "server_address": server_address(state),
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
    """Tun for the client subnet only. auto_route stays off so this
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


def desired_routes(
    *, vpn_exit: bool, gateway: str, iface: str, cidr: str = CLIENT_CIDR,
) -> dict:
    """Routes inside the access container, looked up only for the client subnet."""
    ipaddress.IPv4Address(gateway)
    cidr = str(ipaddress.IPv4Network(cidr))
    if not iface or iface in {TUN_INTERFACE, "wg0", "lo"}:
        raise ValueError("gateway interface must be the container's normal NIC")
    routes = [
        {"dest": cidr, "dev": "wg0"},
    ]
    for prefix in LAN_PREFIXES:
        routes.append({"dest": prefix, "via": gateway, "dev": iface})
    if vpn_exit:
        default = {"dest": "default", "dev": TUN_INTERFACE}
    else:
        default = {"dest": "default", "via": gateway, "dev": iface}
    return {
        "rule_from": cidr,
        "table": 100,
        "routes": routes,
        "default": default,
        "nat": {"source": cidr, "iface": iface},
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


def internal_zones(lan_domain: str, resolv_text: str) -> list[str]:
    """Zones a client device asks the internal DNS server about.

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
    """Where a client device lookup is sent.

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


def resolver_text(host_text: str, container_text: str) -> str:
    """Host resolver text, then the servers Docker forwards to.

    When the host file is systemd-resolved's 127.0.0.53 stub, Docker has
    already looked past it: the container's own resolv.conf names the
    real upstream in an ``# ExtServers: [host(10.0.0.1)]`` comment.
    """
    extra: list[str] = []
    for raw in (container_text or "").splitlines():
        if raw.lstrip("# ").lower().startswith("extservers:"):
            extra += [f"nameserver {ip}" for ip in re.findall(r"\d+\.\d+\.\d+\.\d+", raw)]
    return "\n".join([host_text or "", container_text or "", *extra])


def _route_hex(value: str) -> ipaddress.IPv4Address:
    return ipaddress.IPv4Address(int(value, 16).to_bytes(4, "little"))


def host_networks_from_routes(route_text: str) -> list[dict]:
    """Every IPv4 network in the host's main routing table.

    ``route_text`` is ``/proc/1/net/route``, which a container sharing the
    host PID namespace can read without entering the host namespaces.
    Docker bridges are kept: a client subnet that overlaps any of them
    would hide it from devices. The default route is not a network.
    """
    found: list[dict] = []
    for raw in (route_text or "").splitlines()[1:]:
        parts = raw.split()
        if len(parts) < 8 or parts[0] == "lo":
            continue
        try:
            dest, mask = _route_hex(parts[1]), _route_hex(parts[7])
            net = ipaddress.IPv4Network(f"{dest}/{mask}")
        except ValueError:
            continue
        if net.prefixlen == 0:
            continue
        entry = {"name": parts[0], "cidr": str(net)}
        if entry not in found:
            found.append(entry)
    return found


def internal_resolver(resolv_text: str, fallback: str = "") -> str | None:
    """Address of the DNS server the host uses for internal names.

    A private nameserver in the host resolver is that server. Loopback
    and public resolvers are not: loopback means this machine is the
    resolver, so the query goes to ``fallback``, the host's internal address.
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
    return fallback.strip() or None


def socks5_udp_packet(dest_ip: str, dest_port: int, payload: bytes) -> bytes:
    ip = ipaddress.IPv4Address(dest_ip).packed
    return b"\x00\x00\x00\x01" + ip + int(dest_port).to_bytes(2, "big") + payload


def socks5_udp_payload(packet: bytes) -> bytes:
    if len(packet) < 10 or packet[2] != 0 or packet[3] != 1:
        raise ValueError("not a SOCKS5 IPv4 UDP packet")
    return packet[10:]
