"""Complete Emby's first-run wizard and create the libraries.

Emby ships a startup wizard that blocks the API until it is finished.
We drive it once, unattended, so the appliance boots into a working
server with Movies, TV and Sports libraries already pointing at the
right paths. If the wizard has already been completed (a restore, or a
second provision run) every call here 404s or 400s and we move on.
"""
import time

import httpx

BASE = "http://emby:8096"

LIBRARIES = [
    ("Movies", "movies", "/data/media/movies"),
    ("TV", "tvshows", "/data/media/tv"),
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
