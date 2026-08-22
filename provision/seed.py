"""Seed application config files before their first start.

The *arr applications generate a random API key on first run and write
it into config.xml. If we write that file first, they adopt our key
instead. This must happen before the container starts, which is why
install.sh runs `seed` before `docker compose up`.
"""
import pathlib
import xml.etree.ElementTree as ET

from keys import api_key

STACK = pathlib.Path("/stack")

ARR_DEFAULTS = {
    "sonarr": {"Port": "8989", "UrlBase": ""},
    "radarr": {"Port": "7878", "UrlBase": ""},
    "prowlarr": {"Port": "9696", "UrlBase": ""},
}


def seed_arr(app: str) -> None:
    cfg_dir = STACK / "config" / app
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.xml"

    if cfg.exists():
        # Already initialised. Adopt whatever key is there rather than
        # overwriting it, because changing an existing install's key
        # silently breaks every client that holds the old one.
        tree = ET.parse(cfg)
        existing = tree.getroot().findtext("ApiKey")
        if existing:
            print(f"  {app}: existing API key retained")
            return

    root = ET.Element("Config")
    values = {
        "BindAddress": "*",
        "Port": ARR_DEFAULTS[app]["Port"],
        "SslPort": "9898",
        "EnableSsl": "False",
        "LaunchBrowser": "False",
        "ApiKey": api_key(app),
        "AuthenticationMethod": "External",
        "AuthenticationRequired": "DisabledForLocalAddresses",
        "Branch": "master",
        "LogLevel": "info",
        "UrlBase": ARR_DEFAULTS[app]["UrlBase"],
        "InstanceName": app.capitalize(),
        "AnalyticsEnabled": "False",
    }
    for k, v in values.items():
        ET.SubElement(root, k).text = v

    ET.ElementTree(root).write(cfg, encoding="utf-8", xml_declaration=True)
    print(f"  {app}: seeded config.xml with derived API key")


def seed_all(enabled: set[str]) -> None:
    print("Seeding application config...")
    for app in ARR_DEFAULTS:
        if app in enabled:
            seed_arr(app)
