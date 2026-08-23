"""LAN IPv4 selection for mDNS advertisements."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mdns"))

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
