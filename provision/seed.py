"""Seed application config files before their first start.

The *arr applications generate a random API key on first run and write
it into config.xml. Jackett does the same in ServerConfig.json.
Transmission writes settings.json with /downloads/* paths that do not
match our /data mount. If we write those files first, the apps adopt
our settings instead. This must happen before the container starts,
which is why install.sh runs `seed` before `docker compose up`.
"""
import json
import os
import pathlib
import xml.etree.ElementTree as ET

import yaml

from keys import api_key, resolve_key

STACK = pathlib.Path("/stack")
# Official Seerr image runs as node, not the stack PUID/PGID.
SEERR_UID = 1000
SEERR_GID = 1000

ARR_DEFAULTS = {
    "sonarr": {"Port": "8989", "UrlBase": ""},
    "radarr": {"Port": "7878", "UrlBase": ""},
    "lidarr": {"Port": "8686", "UrlBase": ""},
    "prowlarr": {"Port": "9696", "UrlBase": ""},
}

ARR_AUTH_METHOD = "External"
ARR_AUTH_REQUIRED = "DisabledForLocalAddresses"
_ARR_AUTH_UNSET = {"", "none"}


def _xml_set(root: ET.Element, tag: str, value: str) -> bool:
    el = root.find(tag)
    if el is None:
        ET.SubElement(root, tag).text = value
        return True
    if (el.text or "").strip() == value:
        return False
    el.text = value
    return True


def ensure_arr_external_auth(root: ET.Element) -> bool:
    """Stamp External auth when the *arr UI would block on method None."""
    method = (root.findtext("AuthenticationMethod") or "").strip()
    if method.lower() not in _ARR_AUTH_UNSET:
        return False
    changed = _xml_set(root, "AuthenticationMethod", ARR_AUTH_METHOD)
    changed = _xml_set(root, "AuthenticationRequired", ARR_AUTH_REQUIRED) or changed
    return changed


def seed_arr(app: str) -> None:
    cfg_dir = STACK / "config" / app
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.xml"

    if cfg.exists():
        # Already initialised. Adopt whatever key is there rather than
        # overwriting it, because changing an existing install's key
        # silently breaks every client that holds the old one.
        tree = ET.parse(cfg)
        root = tree.getroot()
        existing = root.findtext("ApiKey")
        if existing:
            if ensure_arr_external_auth(root):
                tree.write(cfg, encoding="utf-8", xml_declaration=True)
                print(f"  {app}: existing API key retained; auth -> External")
            else:
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
        "AuthenticationMethod": ARR_AUTH_METHOD,
        "AuthenticationRequired": ARR_AUTH_REQUIRED,
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


def seed_transmission() -> None:
    cfg_dir = STACK / "config" / "transmission"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "settings.json"

    if cfg.exists():
        print("  transmission: existing settings retained")
        return

    settings = {
        "download-dir": "/data/downloads/complete",
        "incomplete-dir": "/data/downloads/incomplete",
        "incomplete-dir-enabled": True,
        "rpc-enabled": True,
        "rpc-port": 9091,
        "rpc-url": "/transmission/",
        "rpc-bind-address": "[::]",
        "rpc-authentication-required": False,
    }
    cfg.write_text(json.dumps(settings, indent=4) + "\n")
    print("  transmission: seeded settings.json with /data download paths")


def seed_seerr() -> None:
    """Prepare /app/config for the official image (node 1000:1000).

    Docker creates the bind-mount source as root when the directory is
    missing; Seerr then cannot mkdir logs/ and crashes on start.
    """
    cfg_dir = STACK / "config" / "seerr"
    logs_dir = cfg_dir / "logs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    for path in (cfg_dir, logs_dir):
        os.chown(path, SEERR_UID, SEERR_GID)
    for root, dirs, files in os.walk(cfg_dir):
        os.chown(root, SEERR_UID, SEERR_GID)
        for name in dirs:
            os.chown(os.path.join(root, name), SEERR_UID, SEERR_GID)
        for name in files:
            os.chown(os.path.join(root, name), SEERR_UID, SEERR_GID)
    print("  seerr: prepared config for node (1000:1000)")


def seed_bazarr(enabled: set[str]) -> None:
    """Bazarr reads config from /config/config/config.yaml inside the container."""
    cfg_dir = STACK / "config" / "bazarr" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.yaml"

    if cfg.exists():
        try:
            data = yaml.safe_load(cfg.read_text()) or {}
            existing = (data.get("auth") or {}).get("apikey")
        except (OSError, yaml.YAMLError):
            existing = None
        if existing:
            print("  bazarr: existing API key retained")
            return

    config = {
        "auth": {
            "type": "None",
            "username": "",
            "password": "",
            "apikey": api_key("bazarr"),
        },
        "general": {
            "use_sonarr": "sonarr" in enabled,
            "use_radarr": "radarr" in enabled,
        },
        "sonarr": {
            "ip": "127.0.0.1",
            "port": 8989,
            "base_url": "/",
            "ssl": False,
            "apikey": resolve_key("sonarr") if "sonarr" in enabled else "",
        },
        "radarr": {
            "ip": "127.0.0.1",
            "port": 7878,
            "base_url": "/",
            "ssl": False,
            "apikey": resolve_key("radarr") if "radarr" in enabled else "",
        },
    }
    cfg.write_text(yaml.safe_dump(config, sort_keys=False))
    print("  bazarr: seeded config/config.yaml with derived API key")


BEETS_PLUGINS = ("web", "fetchart", "embedart", "lastgenre", "scrub")


def _beets_plugins(existing) -> str:
    if isinstance(existing, str):
        names = existing.split()
    elif isinstance(existing, list):
        names = [str(p) for p in existing]
    else:
        names = []
    for plugin in BEETS_PLUGINS:
        if plugin not in names:
            names.append(plugin)
    return " ".join(names)


def seed_beets() -> None:
    """Web UI plus in-place tagging so Lidarr keeps folders and Beets writes tags."""
    cfg_dir = STACK / "config" / "beets"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.yaml"
    existed = cfg.exists()
    data = yaml.safe_load(cfg.read_text()) if existed else None
    if not isinstance(data, dict):
        data = {}
    data["directory"] = "/music"
    data["library"] = data.get("library") or "/config/library.db"
    data["plugins"] = _beets_plugins(data.get("plugins"))
    imported = dict(data.get("import") or {})
    imported.update({
        "copy": False,
        "move": False,
        "write": True,
        "incremental": True,
        "timid": False,
    })
    data["import"] = imported
    web = dict(data.get("web") or {})
    web.setdefault("host", "0.0.0.0")
    web.setdefault("port", 8337)
    data["web"] = web
    cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    if existed:
        print("  beets: stamped in-place import (copy/move no, write yes)")
    else:
        print("  beets: seeded config.yaml with web UI on 0.0.0.0:8337")


def seed_all(enabled: set[str]) -> None:
    print("Seeding application config...")
    for app in ARR_DEFAULTS:
        if app in enabled:
            seed_arr(app)
    if "jackett" in enabled:
        seed_jackett()
    if "transmission" in enabled:
        seed_transmission()
    if "recyclarr" in enabled:
        from recipes import recyclarr

        recyclarr.seed(print)
    if "seerr" in enabled:
        seed_seerr()
    if "bazarr" in enabled:
        seed_bazarr(enabled)
    if "beets" in enabled:
        seed_beets()
    if "nzbget" in enabled:
        from recipes import nzbget

        nzbget.seed(STACK, enabled)
    from recipes import metrics

    metrics.seed(STACK, enabled)
