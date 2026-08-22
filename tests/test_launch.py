"""Per-app URLs for production domains and local Docker Desktop."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

try:
    import launch  # noqa: E402
except ModuleNotFoundError:
    launch = None


def test_loopback_helm_uses_resolvable_traefik_aliases():
    assert launch is not None
    for app in ("sonarr", "radarr", "prowlarr", "jackett", "transmission"):
        assert launch.app_url(
            app, "kine.local", "127.0.0.1", "127.0.0.1.nip.io"
        ) == f"https://{app}.127.0.0.1.nip.io"


def test_production_helm_uses_configured_domain():
    assert launch.app_url(
        "sonarr", "media.example.com", "admin.media.example.com",
        "127.0.0.1.nip.io",
    ) == "https://sonarr.media.example.com"


def test_app_without_subdomain_has_no_launch_url():
    assert launch.app_url(None, "kine.local", "127.0.0.1", "127.0.0.1.nip.io") is None
