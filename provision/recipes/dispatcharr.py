"""Wire Dispatcharr to Emby Live TV and export API token to ECM/Teamarr."""
from __future__ import annotations

DISPATCHARR_HDHR = "http://dispatcharr:9191/hdhr"
DISPATCHARR_BASE = "http://dispatcharr:9191"
EMBY_BASE = "http://emby:8096"


def tuner_host_payload() -> dict:
    return {
        "Type": "hdhomerun",
        "Url": DISPATCHARR_HDHR,
        "FriendlyName": "Dispatcharr",
        "ImportFavoritesOnly": False,
    }


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def tuner_already_linked(hosts: list) -> bool:
    want = _norm_url(DISPATCHARR_HDHR)
    for host in hosts:
        if not isinstance(host, dict):
            continue
        if _norm_url(str(host.get("Url") or "")) == want:
            return True
    return False
