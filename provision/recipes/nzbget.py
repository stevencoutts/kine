"""NZBGet news servers and default extensions.

News servers live in .env as NZBGET_NEWS_SERVERS (JSON list) and are written
into nzbget.conf as Server1.*, Server2.*, …

Three post-processing extensions are always installed when NZBGet is enabled:
Extended Unpacker, Fake Detector, and Remove Samples — the same set used on
the previous appliance.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
import zipfile

STACK = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))
CONF = STACK / "config" / "nzbget" / "nzbget.conf"
SCRIPTS = STACK / "config" / "nzbget" / "scripts"
ENV_KEY = "NZBGET_NEWS_SERVERS"

_SERVER_LINE = re.compile(r"^Server\d+\.", re.IGNORECASE)
_EXTENSION_OPT = re.compile(
    r"^(ExtendedUnpacker|FakeDetector|RemoveSamples):", re.IGNORECASE
)

# Pinned official release zips from nzbgetcom/nzbget-extensions.
DEFAULT_EXTENSIONS = (
    {
        "name": "ExtendedUnpacker",
        "url": (
            "https://github.com/nzbgetcom/Extension-ExtendedUnpacker/"
            "releases/download/v1.0/extendedunpacker-1.0-dist.zip"
        ),
    },
    {
        "name": "FakeDetector",
        "url": (
            "https://github.com/nzbgetcom/Extension-FakeDetector/"
            "releases/download/v3.1/fakedetector-3.1-dist.zip"
        ),
    },
    {
        "name": "RemoveSamples",
        "url": (
            "https://github.com/nzbgetcom/Extension-RemoveSamples/"
            "releases/download/v1.1.0/removesamples-1.1.0-dist.zip"
        ),
    },
)

# Sensible defaults matching the previous kore install.
EXTENSION_DEFAULTS = {
    "ExtendedUnpacker:UnrarCmd": "",
    "ExtendedUnpacker:UnrarArgs": "e -idp -ai -o-",
    "ExtendedUnpacker:SevenZipCmd": "",
    "ExtendedUnpacker:SevenZipArgs": "e -aos",
    "ExtendedUnpacker:WaitTime": "0",
    "ExtendedUnpacker:DeleteLeftover": "yes",
    "FakeDetector:BannedExtensions": "",
    "RemoveSamples:RemoveDirectories": "Yes",
    "RemoveSamples:RemoveFiles": "Yes",
    "RemoveSamples:TestMode": "No",
    "RemoveSamples:BlockImportDuringTest": "No",
    "RemoveSamples:Debug": "No",
    "RemoveSamples:VideoSizeThresholdMB": "150",
    "RemoveSamples:RelativePercent": "8",
    "RemoveSamples:VideoExts": (
        ".mkv,.mp4,.avi,.mov,.wmv,.flv,.webm,.ts,.m4v,.vob,.mpg,.mpeg,.iso"
    ),
    "RemoveSamples:AudioSizeThresholdMB": "2",
    "RemoveSamples:AudioExts": ".mp3,.flac,.aac,.ogg,.wma,.m4a,.opus,.wav,.alac,.ape",
    "RemoveSamples:ProtectedPaths": "poster.jpg,*/subs/*,*.srt",
    "RemoveSamples:DenyPatterns": "*sample*.jpg,proof*.txt",
    "RemoveSamples:ImageSamples": "No",
    "RemoveSamples:JunkExtras": "No",
    "RemoveSamples:CategoryThresholds": "",
    "RemoveSamples:QuarantineMode": "No",
    "RemoveSamples:QuarantineMaxAgeDays": "7",
}

EXTENSIONS_VALUE = ", ".join(ext["name"] for ext in DEFAULT_EXTENSIONS)


CONTROL_USER = "nzbget"
CONTROL_PASSWORD = "nzbget"
DEST_DIR = "/data/downloads/complete"
INTER_DIR = "/data/incomplete"

# Quiet the stock-image warnings; values match the previous kore appliance.
RUNTIME_DEFAULTS = {
    "DestDir": DEST_DIR,
    "InterDir": INTER_DIR,
    "ControlUsername": CONTROL_USER,
    "ControlPassword": CONTROL_PASSWORD,
    "WriteBuffer": "1024",
    "ArticleCache": "500",
    "WriteLog": "reset",
    "RotateLog": "3",
    "DirectUnpack": "yes",
    "DirectRename": "yes",
}

# Match Sonarr/Radarr/Lidarr download-client categories (and ensure_data_tree dirs).
CATEGORIES = (
    {"name": "tv-sonarr", "dest": f"{DEST_DIR}/tv-sonarr"},
    {"name": "radarr", "dest": f"{DEST_DIR}/radarr"},
    {"name": "lidarr", "dest": f"{DEST_DIR}/lidarr"},
)

_CATEGORY_LINE = re.compile(r"^Category\d+\.", re.IGNORECASE)

# Image / empty placeholders that mean "not really configured".
_PLACEHOLDER_HOSTS = frozenset({
    "",
    "my.newsserver.com",
    "news.example.com",
    "localhost",
})


def parse_servers(raw: str | None) -> list[dict]:
    """Parse NZBGET_NEWS_SERVERS JSON into a normalised list."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for i, row in enumerate(data, start=1):
        if not isinstance(row, dict):
            continue
        host = str(row.get("host") or "").strip()
        if not host or host.lower() in _PLACEHOLDER_HOSTS:
            continue
        encryption = bool(row.get("encryption", True))
        try:
            port = int(row.get("port") or (563 if encryption else 119))
        except (TypeError, ValueError):
            port = 563 if encryption else 119
        try:
            connections = int(row.get("connections") or 8)
        except (TypeError, ValueError):
            connections = 8
        name = str(row.get("name") or "").strip() or f"Server {i}"
        out.append({
            "name": name,
            "host": host,
            "port": max(1, min(port, 65535)),
            "username": str(row.get("username") or ""),
            "password": str(row.get("password") or ""),
            "encryption": encryption,
            "connections": max(1, min(connections, 60)),
        })
    return out


def serialize_servers(servers: list[dict]) -> str:
    return json.dumps(parse_servers(json.dumps(servers)), separators=(",", ":"))


def servers_from_env() -> list[dict]:
    return parse_servers(os.environ.get(ENV_KEY, ""))


def _upsert_conf_key(lines: list[str], key: str, value: str) -> list[str]:
    """Set key=value, replacing an existing assignment or appending."""
    prefix = key + "="
    for i, line in enumerate(lines):
        if line.startswith(prefix) or line.startswith("#" + prefix):
            lines[i] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def apply_servers(conf_path: pathlib.Path, servers: list[dict]) -> None:
    """Replace ServerN.* blocks in nzbget.conf, preserving everything else.

    NZBGet requires contiguous Server1..N numbering with no holes.
    """
    servers = parse_servers(json.dumps(servers))
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    lines = conf_path.read_text().splitlines() if conf_path.is_file() else []

    kept = [ln for ln in lines if not _SERVER_LINE.match(ln.strip())]
    while kept and not kept[-1].strip():
        kept.pop()

    block: list[str] = []
    if servers:
        if kept:
            block.append("")
        block.append("### NEWS-SERVERS (managed by Kine) ###")
        for i, srv in enumerate(servers, start=1):
            prefix = f"Server{i}"
            block.extend([
                f"{prefix}.Active=yes",
                f"{prefix}.Name={srv['name']}",
                f"{prefix}.Level=0",
                f"{prefix}.Optional=no",
                f"{prefix}.Group=0",
                f"{prefix}.Host={srv['host']}",
                f"{prefix}.Port={srv['port']}",
                f"{prefix}.Username={srv['username']}",
                f"{prefix}.Password={srv['password']}",
                f"{prefix}.Encryption={'yes' if srv['encryption'] else 'no'}",
                f"{prefix}.Connections={srv['connections']}",
                f"{prefix}.Cipher=",
            ])
    conf_path.write_text("\n".join(kept + block) + ("\n" if kept or block else ""))


def apply_extensions(conf_path: pathlib.Path) -> None:
    """Enable the three default extensions and their option defaults."""
    if not conf_path.is_file():
        return
    lines = conf_path.read_text().splitlines()
    # Drop previous managed option lines so a re-seed does not duplicate.
    lines = [ln for ln in lines if not _EXTENSION_OPT.match(ln.strip())]
    lines = _upsert_conf_key(lines, "Extensions", EXTENSIONS_VALUE)
    lines = _upsert_conf_key(lines, "ScriptDir", "${MainDir}/scripts")
    for key, value in EXTENSION_DEFAULTS.items():
        lines = _upsert_conf_key(lines, key, value)
    conf_path.write_text("\n".join(lines) + "\n")


def apply_runtime_defaults(conf_path: pathlib.Path) -> None:
    """Fix stock image paths, password, and buffer/log defaults."""
    if not conf_path.is_file():
        return
    lines = conf_path.read_text().splitlines()
    for key, value in RUNTIME_DEFAULTS.items():
        lines = _upsert_conf_key(lines, key, value)
    conf_path.write_text("\n".join(lines) + "\n")


def apply_categories(conf_path: pathlib.Path) -> None:
    """Replace stock Categories with Sonarr/Radarr download-client names."""
    if not conf_path.is_file():
        return
    lines = [ln for ln in conf_path.read_text().splitlines()
             if not _CATEGORY_LINE.match(ln.strip())]
    while lines and not lines[-1].strip():
        lines.pop()
    block = [""]
    block.append("### CATEGORIES (managed by Kine) ###")
    for i, cat in enumerate(CATEGORIES, start=1):
        prefix = f"Category{i}"
        block.extend([
            f"{prefix}.Name={cat['name']}",
            f"{prefix}.DestDir={cat['dest']}",
            f"{prefix}.Unpack=yes",
            f"{prefix}.Extensions=",
            f"{prefix}.Aliases=",
        ])
    conf_path.write_text("\n".join(lines + block) + "\n")


def _extension_ready(dest: pathlib.Path) -> bool:
    return (dest / "manifest.json").is_file() and (
        (dest / "main.py").is_file() or any(dest.glob("*.py"))
    )


def _download_zip(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "kine-provision/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — pinned release URLs
        return resp.read()


def install_extensions(scripts_dir: pathlib.Path, log=print) -> list[str]:
    """Download and unpack missing default extensions into ScriptDir."""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for ext in DEFAULT_EXTENSIONS:
        dest = scripts_dir / ext["name"]
        if _extension_ready(dest):
            installed.append(ext["name"])
            continue
        try:
            raw = _download_zip(ext["url"])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"  nzbget: could not download {ext['name']} ({exc})")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            # Dist zips are either flat or wrapped in a single top folder.
            names = [n for n in zf.namelist() if not n.endswith("/")]
            roots = {n.split("/", 1)[0] for n in zf.namelist() if "/" in n}
            strip = len(roots) == 1 and next(iter(roots)) == ext["name"]
            for info in zf.infolist():
                if info.is_dir():
                    continue
                member = info.filename
                if strip and member.startswith(ext["name"] + "/"):
                    member = member[len(ext["name"]) + 1 :]
                elif "/" in member and member.split("/", 1)[0] in roots and len(roots) == 1:
                    member = member.split("/", 1)[1]
                if not member or member.endswith("/"):
                    continue
                target = dest / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                # Scripts must be executable inside the container.
                if target.suffix == ".py" or target.name == "main":
                    target.chmod(target.stat().st_mode | 0o111)
        if _extension_ready(dest):
            installed.append(ext["name"])
            log(f"  nzbget: installed extension {ext['name']}")
        else:
            log(f"  nzbget: {ext['name']} zip did not contain a usable extension")
    return installed


def seed(stack: pathlib.Path, enabled: set[str], log=print) -> None:
    if "nzbget" not in enabled:
        return
    cfg_dir = stack / "config" / "nzbget"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "scripts").mkdir(parents=True, exist_ok=True)

    installed = install_extensions(cfg_dir / "scripts", log)
    if installed:
        log(f"  nzbget: extensions ready ({', '.join(installed)})")

    servers = servers_from_env()
    conf = cfg_dir / "nzbget.conf"
    if not servers:
        log("  nzbget: no NZBGET_NEWS_SERVERS configured yet")
    if not conf.is_file():
        # Let the image create a full default conf on first start; wire
        # patches ServerN.* and Extensions once it exists.
        if servers:
            log(f"  nzbget: {len(servers)} news server(s) ready; will apply after first start")
        return
    if servers:
        apply_servers(conf, servers)
        log(f"  nzbget: updated {len(servers)} news server(s) in nzbget.conf")
    apply_runtime_defaults(conf)
    apply_categories(conf)
    apply_extensions(conf)
    log(f"  nzbget: enabled extensions {EXTENSIONS_VALUE}")


def configure(enabled: set[str], log) -> None:
    """Wait for nzbget.conf, install extensions, apply servers + Extensions."""
    if "nzbget" not in enabled:
        return
    scripts = SCRIPTS
    scripts.mkdir(parents=True, exist_ok=True)
    installed = install_extensions(scripts, log)

    conf = CONF
    for _ in range(30):
        if conf.is_file():
            break
        time.sleep(2)
    else:
        log("nzbget: timed out waiting for nzbget.conf; configure later from Settings")
        return

    servers = servers_from_env()
    if servers:
        apply_servers(conf, servers)
        log(f"nzbget: applied {len(servers)} news server(s)")
    else:
        log("nzbget: no news servers in .env yet")
    apply_runtime_defaults(conf)
    apply_categories(conf)
    apply_extensions(conf)
    log(f"nzbget: control password set; paths {DEST_DIR} / {INTER_DIR}")
    log(
        f"nzbget: enabled extensions "
        f"{', '.join(installed) if installed else EXTENSIONS_VALUE}"
    )
