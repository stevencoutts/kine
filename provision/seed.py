"""Seed application config files before their first start.

The *arr applications generate a random API key on first run and write
it into config.xml. Jackett does the same in ServerConfig.json. If we
write those files first, the apps adopt our key instead. This must
happen before the container starts, which is why install.sh runs `seed`
before `docker compose up`.
"""
import json
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


def seed_jackett() -> None:
    cfg_dir = STACK / "config" / "jackett" / "Jackett"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "ServerConfig.json"

    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text()).get("APIKey")
        except (OSError, json.JSONDecodeError):
            existing = None
        if existing:
            print("  jackett: existing API key retained")
            return

    config = {
        "Port": 9117,
        "LocalBindAddress": "127.0.0.1",
        "AllowExternal": True,
        "AllowCORS": False,
        "APIKey": api_key("jackett"),
        "AdminPassword": None,
        "BlackholeDir": None,
        "UpdateDisabled": False,
        "UpdatePrerelease": False,
        "BasePathOverride": None,
        "BaseUrlOverride": None,
        "CacheEnabled": True,
        "CacheTtl": 2100,
        "CacheMaxResultsPerIndexer": 1000,
        "FlareSolverrUrl": None,
        "FlareSolverrMaxTimeout": 55000,
        "OmdbApiKey": None,
        "OmdbApiUrl": None,
        "ProxyType": 0,
        "ProxyUrl": None,
        "ProxyPort": None,
        "ProxyUsername": None,
        "ProxyPassword": None,
        "ProxyIsAnonymous": True,
    }
    cfg.write_text(json.dumps(config, indent=2) + "\n")
    print("  jackett: seeded ServerConfig.json with derived API key")


def seed_all(enabled: set[str]) -> None:
    print("Seeding application config...")
    for app in ARR_DEFAULTS:
        if app in enabled:
            seed_arr(app)
    if "jackett" in enabled:
        seed_jackett()
