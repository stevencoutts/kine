"""Proxy Dispatcharr M3U / EPG account APIs for Helm Settings."""
from __future__ import annotations

from typing import Any

import httpx

from . import config

DISPATCHARR_BASE = "http://gluetun:9191"
M3U_CREATE_TIMEOUT = 600.0
DEFAULT_TIMEOUT = 60.0

_M3U_PUBLIC = (
    "id", "name", "server_url", "account_type", "status",
    "is_active", "last_message", "max_streams",
)
_EPG_PUBLIC = (
    "id", "name", "source_type", "url", "status",
    "is_active", "last_message",
)


def enabled() -> bool:
    return "dispatcharr" in config.profiles()


def configured() -> bool:
    return bool((config.read().get("DISPATCHARR_TOKEN") or "").strip())


async def ensure_ready() -> None:
    """Try to auto-provision a Dispatcharr API token before API calls."""
    from . import dispatcharr_token

    if configured():
        return
    await dispatcharr_token.ensure_token(write_env=True)


def _token() -> str:
    token = (config.read().get("DISPATCHARR_TOKEN") or "").strip()
    if not token:
        raise ValueError("Set a Dispatcharr API token in Live TV settings first")
    return token


def _headers() -> dict[str, str]:
    return {
        "X-API-Key": _token(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _public(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row.get(k) for k in keys}


def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    if not enabled():
        raise ValueError("Dispatcharr is not enabled")
    url = f"{DISPATCHARR_BASE}{path}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.request(method, url, headers=_headers(), json=json)
    if resp.status_code >= 400:
        detail = resp.text.strip()
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = (
                    body.get("detail")
                    or body.get("message")
                    or body.get("error")
                    or detail
                )
        except ValueError:
            pass
        raise ValueError(str(detail) or f"Dispatcharr returned {resp.status_code}")
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def list_m3u() -> list[dict[str, Any]]:
    data = _request("GET", "/api/m3u/accounts/")
    return [_public(row, _M3U_PUBLIC) for row in _unwrap_list(data)]


def create_m3u(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    account_type = str(body.get("account_type") or "XC").strip().upper()
    if account_type not in {"XC", "STD"}:
        raise ValueError("account_type must be XC or STD")
    server_url = str(body.get("server_url") or "").strip()
    if not server_url:
        raise ValueError("Server URL is required")
    payload: dict[str, Any] = {
        "name": name,
        "account_type": account_type,
        "server_url": server_url,
        "is_active": bool(body.get("is_active", True)),
    }
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if username:
        payload["username"] = username
    if password:
        payload["password"] = password
    raw_max = body.get("max_streams", 0)
    try:
        max_streams = int(raw_max if raw_max is not None and raw_max != "" else 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_streams must be a non-negative integer") from exc
    if max_streams < 0:
        raise ValueError("max_streams must be a non-negative integer")
    payload["max_streams"] = max_streams
    data = _request(
        "POST",
        "/api/m3u/accounts/",
        json=payload,
        timeout=M3U_CREATE_TIMEOUT,
    )
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Dispatcharr")
    return _public(data, _M3U_PUBLIC)


def delete_m3u(account_id: int) -> None:
    _request("DELETE", f"/api/m3u/accounts/{account_id}/")


def refresh_m3u(account_id: int) -> None:
    _request("POST", f"/api/m3u/refresh/{account_id}/")


def list_epg() -> list[dict[str, Any]]:
    data = _request("GET", "/api/epg/sources/")
    return [_public(row, _EPG_PUBLIC) for row in _unwrap_list(data)]


def create_epg(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    source_type = str(body.get("source_type") or "xmltv").strip().lower()
    allowed = {"xmltv", "schedules_direct", "dummy"}
    if source_type not in allowed:
        raise ValueError("source_type must be xmltv, schedules_direct, or dummy")
    payload: dict[str, Any] = {
        "name": name,
        "source_type": source_type,
        "is_active": bool(body.get("is_active", True)),
    }
    url = str(body.get("url") or "").strip()
    if url:
        payload["url"] = url
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if username:
        payload["username"] = username
    if password:
        payload["password"] = password
    if source_type == "xmltv" and not url:
        raise ValueError("URL is required for XMLTV EPG sources")
    data = _request("POST", "/api/epg/sources/", json=payload, timeout=DEFAULT_TIMEOUT)
    if not isinstance(data, dict):
        raise ValueError("Unexpected response from Dispatcharr")
    return _public(data, _EPG_PUBLIC)


def delete_epg(source_id: int) -> None:
    _request("DELETE", f"/api/epg/sources/{source_id}/")


def refresh_epg(source_id: int) -> None:
    _request("POST", "/api/epg/import/", json={"id": source_id})
