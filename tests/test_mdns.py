"""LAN IPv4 selection for mDNS advertisements."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mdns"))
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from gen_hosts import build_names  # noqa: E402
from mdns_policy import should_run  # noqa: E402
from pick_ip import _is_bad_dev, _is_bad_ip, pick_host_ip  # noqa: E402


def test_skips_docker_bridge_ips():
    assert _is_bad_ip("172.24.0.1") is True
    assert _is_bad_ip("172.17.0.1") is True
    assert _is_bad_ip("10.100.100.34") is False


def test_skips_bridge_interfaces():
    assert _is_bad_dev("br-abc123") is True
    assert _is_bad_dev("docker0") is True
    assert _is_bad_dev("eth0") is False


def test_override_env(monkeypatch):
    monkeypatch.setenv("MDNS_HOST_IP", "10.100.100.34")
    assert pick_host_ip() == "10.100.100.34"


CAT = {
    "sonarr": {"subdomain": "sonarr"},
    "helm": {"subdomain": "kine-admin"},
}


def test_build_names_for_local_domain():
    names = build_names("kine.local", {"sonarr"}, CAT)
    assert "kine.local" in names
    assert "sonarr.kine.local" in names


def test_build_names_skips_real_dns_domain():
    assert build_names("couttsnet.com", {"sonarr", "mdns"}, CAT) == []


def test_mdns_runs_only_for_local_domain():
    assert should_run("kine.local", ["mdns", "sonarr"]) is True
    assert should_run("couttsnet.com", ["mdns", "sonarr"]) is False
    assert should_run("kine.local", ["sonarr"]) is False


def test_entrypoint_clears_stale_dbus_pid():
    text = (ROOT / "mdns" / "entrypoint.sh").read_text()
    assert "dbus.pid" in text


def test_refresh_mdns_stops_when_domain_is_not_local():
    text = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert "mdns_policy.should_run" in text
    assert '"--profile", "mdns"' in text or "'--profile', 'mdns'" in text
