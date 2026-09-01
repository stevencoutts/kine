"""Complete Emby's first-run wizard and create the libraries.

Emby ships a startup wizard that blocks the API until it is finished.
We drive it once, unattended, so the appliance boots into a working
server with Movies, TV and Sports libraries already pointing at the
right paths. If the wizard has already been completed (a restore, or a
second provision run) every call here 404s or 400s and we move on.
"""
import os
import time

import httpx

BASE = "http://emby:8096"

LIBRARIES = [
    ("Movies", "movies", "/data/media/movies"),
    ("TV", "tvshows", "/data/media/tv"),
    ("Music", "music", "/data/media/music"),
    ("Sports", "tvshows", "/data/media/sports"),
]


def _wait(timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/System/Info/Public", timeout=10)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(5)
    return False


def configure(admin_user: str, admin_pass: str, log) -> None:
    if not _wait():
        log("emby: no response, skipping wiring")
        return

    http = httpx.Client(base_url=BASE, timeout=30.0)

    try:
        state = http.get("/System/Info/Public").json()
        if state.get("IsStartupWizardCompleted"):
            log("emby: already configured, leaving alone")
            return
    except (httpx.HTTPError, ValueError):
        pass

    steps = [
        ("/Startup/Configuration", {
            "UICulture": "en-GB",
            "MetadataCountryCode": "GB",
            "PreferredMetadataLanguage": "en",
        }),
        ("/Startup/User", {"Name": admin_user, "Password": admin_pass}),
    ]
    for path, payload in steps:
        try:
            http.post(path, json=payload).raise_for_status()
        except httpx.HTTPError as exc:
            log(f"emby: step {path} failed ({exc}); continuing")

    for name, kind, path in LIBRARIES:
        try:
            http.post(
                "/Library/VirtualFolders",
                params={"name": name, "collectionType": kind, "refreshLibrary": "false"},
                json={"LibraryOptions": {"PathInfos": [{"Path": path}]}},
            ).raise_for_status()
            log(f"emby: library {name} -> {path}")
        except httpx.HTTPError as exc:
            log(f"emby: library {name} not created ({exc})")

    try:
        http.post("/Startup/Complete").raise_for_status()
        log("emby: startup wizard completed")
    except httpx.HTTPError as exc:
        log(f"emby: could not complete wizard ({exc})")


def _env_bool(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def remote_from_env() -> tuple[str, str] | None:
    """Helm Settings Emby (bundled or remote)."""
    key = (os.environ.get("EMBY_API_KEY") or "").strip()
    host = (os.environ.get("EMBY_HOST") or "").strip().rstrip("/")
    if not key or not host:
        return None
    use_ssl = _env_bool("EMBY_USE_SSL")
    raw_port = (os.environ.get("EMBY_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else (443 if use_ssl else 8096)
    except ValueError:
        port = 443 if use_ssl else 8096
    if use_ssl and port == 8096:
        port = 443
    scheme = "https" if use_ssl else "http"
    if (use_ssl and port == 443) or (not use_ssl and port == 80):
        url = f"{scheme}://{host}"
    else:
        url = f"{scheme}://{host}:{port}"
    return url, key


def channel_number_key(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text


def _livetv_channels(emby_http: httpx.Client) -> list[dict]:
    for path in ("/emby/LiveTv/Channels", "/LiveTv/Channels"):
        try:
            resp = emby_http.get(path, params={"Fields": "ImageTags"})
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        items = data.get("Items") if isinstance(data, dict) else data
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def sync_event_channel_art(
    channels: list[dict],
    log,
    *,
    emby_http: httpx.Client | None = None,
) -> int:
    """Replace Emby Live TV Primary art with Teamarr's current Game-Thumbs URL."""
    wanted: dict[str, str] = {}
    for row in channels:
        if not isinstance(row, dict):
            continue
        number = channel_number_key(row.get("channel_number") or row.get("Number"))
        url = str(row.get("logo_url") or "").strip()
        if number and url:
            wanted[number] = url
    if not wanted:
        return 0

    own = emby_http is None
    if emby_http is None:
        target = remote_from_env()
        if not target:
            log("emby: no Helm Emby host/API key, skipping event art sync")
            return 0
        base, key = target
        emby_http = httpx.Client(
            base_url=base, timeout=30.0, headers={"X-Emby-Token": key},
        )
    updated = 0
    try:
        by_number = {
            channel_number_key(item.get("Number") or item.get("ChannelNumber")): item
            for item in _livetv_channels(emby_http)
        }
        for number, url in wanted.items():
            item = by_number.get(number)
            item_id = str((item or {}).get("Id") or "").strip()
            if not item_id:
                continue
            resp = emby_http.post(
                f"/emby/Items/{item_id}/Images/Primary/0/Url",
                params={"Url": url},
            )
            if resp.status_code >= 400:
                resp = emby_http.post(
                    f"/Items/{item_id}/Images/Primary/0/Url",
                    params={"Url": url},
                )
            if resp.status_code < 300:
                log(f"emby: channel {number} art -> {url}")
                updated += 1
            else:
                log(f"emby: channel {number} art HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        log(f"emby: event art sync failed ({exc})")
        return updated
    finally:
        if own:
            emby_http.close()
    return updated


def sync_from_teamarr(teamarr_http: httpx.Client, log) -> int:
    """Read Teamarr managed event channels and push logos to Emby."""
    try:
        resp = teamarr_http.get("/api/v1/channels/managed")
        resp.raise_for_status()
        data = resp.json() if resp.content else []
    except httpx.HTTPError as exc:
        log(f"emby: teamarr channels unavailable ({exc})")
        return 0
    rows = data if isinstance(data, list) else (
        data.get("channels") or data.get("items") or []
    )
    if not isinstance(rows, list):
        rows = []
    return sync_event_channel_art(rows, log)
