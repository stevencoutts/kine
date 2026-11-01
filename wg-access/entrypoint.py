"""WireGuard server for laptops, beside gluetun.

This process never restarts gluetun and never changes its firewall.
The only contact with that container is a read of its VPN status and,
when the status is up, a new process placed in its network namespace
so the laptop's internet can leave through the tunnel that is already
up. If that path is missing, the laptop uses the home connection.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, "/app")
import client_vpn  # noqa: E402

STATE = pathlib.Path("/config/state.json")
STATUS = pathlib.Path("/config/status.json")
SOCKS_JSON = pathlib.Path("/config/socks.json")
ROUTER_JSON = pathlib.Path("/config/router.json")
WG_CONF = pathlib.Path("/etc/wireguard/wg0.conf")
UPSTREAM_DNS = ("1.1.1.1", 53)

STOP = threading.Event()
RUNTIME = {
    "lock": threading.Lock(),
    "lan_ip": "",
    "names": frozenset(),
    "vpn_exit": False,
    "socks_ip": "",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def link_exists(name: str) -> bool:
    return run(["ip", "link", "show", name]).returncode == 0


def our_helper_pids() -> list[int]:
    found: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if client_vpn.is_our_helper(pid, cmdline):
            found.append(pid)
    return found


def stop_helpers(marker: bytes | None = None) -> None:
    for pid in our_helper_pids():
        if marker is not None:
            try:
                cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            if marker not in cmdline:
                continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def gluetun_pid() -> int | None:
    found: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1:
            continue
        try:
            environ = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if client_vpn.process_is_gluetun(environ, cmdline):
            found.append(pid)
    return min(found) if found else None


def gluetun_ip() -> str | None:
    try:
        sock = socket.create_connection(("gluetun", 8000), timeout=2)
    except OSError:
        return None
    try:
        return sock.getpeername()[0]
    finally:
        sock.close()


def vpn_running() -> bool:
    try:
        with urllib.request.urlopen("http://gluetun:8000/v1/vpn/status", timeout=2) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return False
    return client_vpn.vpn_is_up(body)


def default_gateway() -> tuple[str, str] | None:
    result = run(["ip", "-4", "route", "show", "default"])
    parts = (result.stdout or "").split()
    if "via" not in parts or "dev" not in parts:
        return None
    return parts[parts.index("via") + 1], parts[parts.index("dev") + 1]


def write_json(path: pathlib.Path, payload: dict) -> bool:
    text = json.dumps(payload, indent=2) + "\n"
    if path.is_file() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


def write_status(exit_mode: str, detail: str) -> None:
    write_json(STATUS, {"exit": exit_mode, "detail": detail})


def ensure_forwarding() -> None:
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    run(["sysctl", "-w", "net.ipv4.conf.all.rp_filter=2"])


def ensure_wg(state: dict, previous: str) -> str:
    conf = client_vpn.server_config(state)
    WG_CONF.parent.mkdir(parents=True, exist_ok=True)
    if not link_exists("wg0"):
        created = run(["ip", "link", "add", "wg0", "type", "wireguard"])
        if created.returncode != 0:
            raise RuntimeError(created.stdout.strip() or "could not create wg0")
        run(["ip", "link", "set", "wg0", "mtu", str(client_vpn.CLIENT_MTU), "up"])
    run(["ip", "addr", "replace", f"{client_vpn.SERVER_ADDRESS}/24", "dev", "wg0"])
    if not link_exists("wg0"):
        raise RuntimeError("wg0 did not come up")
    if conf != previous or not previous:
        WG_CONF.write_text(conf)
        os.chmod(WG_CONF, 0o600)
        # setconf the first time. After that syncconf so a new device
        # does not drop a laptop that is already connected.
        tool = "setconf" if not previous else "syncconf"
        synced = run(["wg", tool, "wg0", str(WG_CONF)])
        if synced.returncode != 0:
            log(synced.stdout.strip() or f"wg {tool} failed")
            return previous
    return conf


def helper_alive(marker: bytes) -> bool:
    for pid in our_helper_pids():
        try:
            cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            continue
        if marker in cmdline:
            return True
    return False


def start_helper(args: list[str]) -> None:
    log("start " + " ".join(args))
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_socks(listen_ip: str) -> None:
    changed = write_json(SOCKS_JSON, client_vpn.socks_config(listen_ip))
    if changed:
        stop_helpers(b"/config/socks.json")
        time.sleep(0.2)
    if helper_alive(b"/config/socks.json"):
        return
    pid = gluetun_pid()
    if pid is None:
        return
    start_helper(client_vpn.socks_process_args(pid))


def ensure_router(socks_ip: str) -> None:
    changed = write_json(ROUTER_JSON, client_vpn.router_config(socks_ip))
    if changed:
        stop_helpers(b"/config/router.json")
        time.sleep(0.2)
    if helper_alive(b"/config/router.json"):
        return
    start_helper(["/usr/local/bin/sing-box", "run", "-c", "/config/router.json"])


def apply_routes(vpn_exit: bool) -> str | None:
    gateway = default_gateway()
    if gateway is None:
        return "no default route in the access container"
    gw, iface = gateway
    try:
        plan = client_vpn.desired_routes(vpn_exit=vpn_exit, gateway=gw, iface=iface)
    except ValueError as exc:
        return str(exc)
    rules = run(["ip", "rule", "show"]).stdout or ""
    needle = f"from {plan['rule_from']} lookup {plan['table']}"
    if needle not in rules:
        run([
            "ip", "rule", "add", "from", plan["rule_from"],
            "lookup", str(plan["table"]), "priority", "100",
        ])
    for route in plan["routes"] + [plan["default"]]:
        cmd = ["ip", "route", "replace", route["dest"]]
        if route.get("via"):
            cmd += ["via", route["via"]]
        cmd += ["dev", route["dev"], "table", str(plan["table"])]
        result = run(cmd)
        if route["dest"] == "default" and result.returncode != 0:
            if vpn_exit:
                return apply_routes(False)
            return result.stdout.strip() or "could not install the default route"
    nat = plan["nat"]
    check = [
        "iptables", "-t", "nat", "-C", "POSTROUTING",
        "-s", nat["source"], "-o", nat["iface"], "-j", "MASQUERADE",
    ]
    if run(check).returncode != 0:
        run([
            "iptables", "-t", "nat", "-A", "POSTROUTING",
            "-s", nat["source"], "-o", nat["iface"], "-j", "MASQUERADE",
        ])
    return None


def teardown() -> None:
    stop_helpers()
    if link_exists("wg0"):
        run(["ip", "link", "del", "wg0"])
    write_status("off", "Client VPN is off.")


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("short socks read")
        buf += chunk
    return buf


def forward_plain(query: bytes) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        sock.sendto(query, UPSTREAM_DNS)
        data, _ = sock.recvfrom(2048)
        return data
    except OSError:
        return None
    finally:
        sock.close()


def forward_socks(query: bytes, socks_ip: str) -> bytes | None:
    tcp = socket.create_connection((socks_ip, client_vpn.SOCKS_PORT), timeout=3)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.settimeout(3)
    try:
        tcp.sendall(b"\x05\x01\x00")
        if _read_exact(tcp, 2) != b"\x05\x00":
            return None
        tcp.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        head = _read_exact(tcp, 4)
        if head[1] != 0 or head[3] != 1:
            return None
        addr = socket.inet_ntoa(_read_exact(tcp, 4))
        port = int.from_bytes(_read_exact(tcp, 2), "big")
        if addr == "0.0.0.0":
            addr = socks_ip
        packet = client_vpn.socks5_udp_packet(UPSTREAM_DNS[0], UPSTREAM_DNS[1], query)
        udp.sendto(packet, (addr, port))
        reply, _ = udp.recvfrom(2048)
        return client_vpn.socks5_udp_payload(reply)
    except (OSError, ValueError):
        return None
    finally:
        udp.close()
        tcp.close()


def dns_loop() -> None:
    while not STOP.is_set():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((client_vpn.SERVER_ADDRESS, 53))
        except OSError:
            sock.close()
            STOP.wait(1)
            continue
        sock.settimeout(1)
        while not STOP.is_set():
            try:
                query, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            reply = answer(query)
            if reply:
                try:
                    sock.sendto(reply, addr)
                except OSError:
                    pass
        sock.close()


def answer(query: bytes) -> bytes | None:
    parsed = client_vpn.parse_qname(query)
    if parsed is None:
        return None
    name, _end, _qtype = parsed
    key = name.lower().rstrip(".")
    with RUNTIME["lock"]:
        names = RUNTIME["names"]
        lan_ip = RUNTIME["lan_ip"]
        vpn_exit = RUNTIME["vpn_exit"]
        socks_ip = RUNTIME["socks_ip"]
    if key in names and lan_ip:
        return client_vpn.dns_reply(query, lan_ip)
    if vpn_exit and socks_ip:
        forwarded = forward_socks(query, socks_ip)
        if forwarded:
            return forwarded
    return forward_plain(query)


def publish_runtime(state: dict, vpn_exit: bool, socks_ip: str) -> None:
    names = {str(name).lower().rstrip(".") for name in state.get("names") or []}
    with RUNTIME["lock"]:
        RUNTIME["lan_ip"] = str(state.get("lan_endpoint") or "")
        RUNTIME["names"] = frozenset(names)
        RUNTIME["vpn_exit"] = vpn_exit
        RUNTIME["socks_ip"] = socks_ip


def tick(previous_conf: str) -> str:
    state = client_vpn.load_state(STATE)
    if state is None or not state.get("enabled"):
        teardown()
        return ""
    conf = ensure_wg(state, previous_conf)
    ip = gluetun_ip()
    if ip:
        ensure_socks(ip)
        ensure_router(ip)
    tun_up = link_exists(client_vpn.TUN_INTERFACE)
    socks_up = helper_alive(b"/config/socks.json")
    vpn_exit = bool(ip) and vpn_running() and tun_up and socks_up
    problem = apply_routes(vpn_exit and tun_up)
    publish_runtime(state, vpn_exit, ip or "")
    if problem:
        write_status("wan", problem)
    elif vpn_exit:
        write_status("vpn", "Laptop internet is leaving through the VPN.")
    else:
        write_status("wan", "Laptop internet is leaving through the internal network connection.")
    return conf


def main() -> None:
    def _stop(_signum: int, _frame: object) -> None:
        STOP.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    ensure_forwarding()
    threading.Thread(target=dns_loop, name="dns", daemon=True).start()
    previous = ""
    while not STOP.is_set():
        try:
            previous = tick(previous)
        except Exception as exc:
            log(f"client vpn: {exc}")
            write_status("wan", str(exc))
        STOP.wait(3)
    teardown()


if __name__ == "__main__":
    main()
