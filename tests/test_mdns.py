"""LAN IPv4 selection for mDNS advertisements."""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mdns"))
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

from gen_hosts import build_names, dns_names  # noqa: E402
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
    assert names[0] == "kine.local"
    assert "kine-admin.kine.local" in names
    assert "sonarr.kine.local" in names
    assert names.count("kine-admin.kine.local") == 1


def test_build_names_includes_admin_without_app_profiles():
    # Fresh install: COMPOSE_PROFILES is only mdns, and helm is not a catalogue app.
    names = build_names("kine.local", {"mdns"}, {"sonarr": {"subdomain": "sonarr"}})
    assert names == ["kine.local", "kine-admin.kine.local"]


def test_admin_name_not_duplicated_when_catalogue_also_lists_it():
    names = build_names("kine.local", {"helm"}, CAT)
    assert names.count("kine-admin.kine.local") == 1


def test_install_hosts_uses_build_names():
    text = (ROOT / "install.sh").read_text()
    assert "from gen_hosts import build_names" in text


def test_build_names_skips_real_dns_domain():
    assert build_names("couttsnet.com", {"sonarr", "mdns"}, CAT) == []


# Names Traefik serves from catalogue.yml, in file order, plus the admin
# hostname. Hidden apps in that file have no subdomain. The apex is not
# served. Fresh installs enable only mdns, so this list is not filtered
# by the profiles running now.
CATALOGUE_DNS = [
    "kine-admin",
    "emby",
    "tdarr",
    "sonarr",
    "radarr",
    "lidarr",
    "beets",
    "prowlarr",
    "jackett",
    "bazarr",
    "transmission",
    "nzbget",
    "seerr",
    "tv",
    "channels",
    "mcp",
    "thumbs",
    "sports",
    "grafana",
    "pihole",
]


def test_dns_names_lists_every_published_subdomain_once_apps_are_enabled():
    catalogue = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]
    names = dns_names("couttsnet.com", catalogue)
    assert names == [f"{sub}.couttsnet.com" for sub in CATALOGUE_DNS]
    assert "couttsnet.com" not in names
    assert "traefik.couttsnet.com" not in names
    assert "dispatcharr.couttsnet.com" not in names


def test_dns_names_ignores_which_profiles_are_on_now():
    catalogue = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]
    assert "pihole.couttsnet.com" in dns_names("couttsnet.com", catalogue)
    assert "kine-admin.couttsnet.com" in dns_names("couttsnet.com", catalogue)


def test_dns_names_skips_hidden_apps_without_a_public_name():
    catalogue = {
        "prometheus": {"hidden": True},
        "gluetun": {"hidden": True, "mandatory": True},
        "sonarr": {"subdomain": "sonarr"},
    }
    assert dns_names("example.com", catalogue) == [
        "kine-admin.example.com",
        "sonarr.example.com",
    ]


def test_dns_names_includes_a_hidden_app_only_when_it_publishes_a_subdomain():
    catalogue = {
        "metrics-ui": {"hidden": True, "subdomain": "metrics"},
        "sonarr": {"subdomain": "sonarr"},
    }
    assert dns_names("example.com", catalogue) == [
        "kine-admin.example.com",
        "metrics.example.com",
        "sonarr.example.com",
    ]


def test_dns_names_omits_the_apex():
    assert dns_names("example.com", {"sonarr": {"subdomain": "sonarr"}})[0] == (
        "kine-admin.example.com"
    )
    assert "example.com" not in dns_names("example.com.", {})


def test_install_explains_mdns_and_what_changing_the_domain_needs():
    text = (ROOT / "install.sh").read_text()
    tail = text.split('bold "Ready"', 1)[1]
    local_branch, custom_branch = tail.split("\nelse\n", 1)

    assert "multicast DNS, not the DNS server" in local_branch
    assert "/etc/hosts" in local_branch
    assert "Avahi and libnss-mdns" in local_branch
    assert "mdns4_minimal" in local_branch
    assert "NXDOMAIN" in local_branch
    assert "If you change the domain, add an A record for each name below." in local_branch
    assert "Each name is <host>.<your domain>" in local_branch
    assert "from gen_hosts import dns_names" in local_branch
    assert "A single wildcard A record for *.<your domain>" in local_branch
    # Labels come from dns_names. The default message names no stand-in domain.
    assert "example.com" not in local_branch
    assert "couttsnet.com" not in local_branch
    assert "*.kine.local" not in local_branch
    assert "pihole" not in local_branch.lower()
    assert "Pi-hole" not in local_branch

    assert "mDNS will not be how other machines find" in custom_branch
    assert "from gen_hosts import dns_names" in custom_branch
    assert 'print(f"  {name}  A  {ip}")' in custom_branch
    assert "A single wildcard A record for *.${KINE_DOMAIN}" in custom_branch


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
