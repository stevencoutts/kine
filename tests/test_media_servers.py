"""Parse Plex/Emby identity payloads for the Media overview card."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app.media_servers import parse_emby_info, parse_plex_identity  # noqa: E402


def test_parse_plex_identity():
    info = parse_plex_identity({
        "MediaContainer": {
            "friendlyName": "Osiris",
            "version": "1.41.0",
            "platform": "Linux",
        }
    })
    assert info["name"] == "Osiris"
    assert info["version"] == "1.41.0"


def test_parse_emby_info():
    info = parse_emby_info({"ServerName": "Home", "Version": "4.8.0.0", "OperatingSystem": "Linux"})
    assert info["name"] == "Home"
    assert info["version"] == "4.8.0.0"


def test_media_servers_route_mounted():
    backend = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    assert '@app.get("/api/media-servers")' in backend
