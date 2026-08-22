"""mDNS name list: which hostnames get advertised for a given profile."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mdns"))

from gen_hosts import build_names  # noqa: E402

CATALOGUE = {
    "emby": {"subdomain": "emby"},
    "sonarr": {"subdomain": "sonarr"},
    "unpackerr": {},  # no subdomain: headless, must never get an entry
}


def test_apex_plus_enabled_subdomains():
    names = build_names("media.local", {"emby", "sonarr"}, CATALOGUE)
    assert names == ["media.local", "emby.media.local", "sonarr.media.local"]


def test_disabled_app_gets_no_entry():
    names = build_names("media.local", {"emby"}, CATALOGUE)
    assert "sonarr.media.local" not in names


def test_subdomain_less_app_never_gets_an_entry():
    names = build_names("media.local", {"emby", "sonarr", "unpackerr"}, CATALOGUE)
    assert len(names) == 3  # apex + emby + sonarr, nothing for unpackerr


def test_non_local_domain_used_as_is():
    names = build_names("media.lan", {"emby"}, CATALOGUE)
    assert names[0] == "media.lan"
