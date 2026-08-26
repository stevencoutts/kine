"""Newznab indexers for Prowlarr, configured from Helm / .env.

Prowlarr fullSync pushes these into Sonarr and Radarr. Direct *arr
Newznab entries are out of scope — Settings writes here only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

ENV_KEY = "PROWLARR_NEWZNAB_INDEXERS"

# Broad Newznab set so one Prowlarr indexer covers both Sonarr and Radarr.
DEFAULT_CATEGORIES = [
    2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060,  # Movies
    5000, 5010, 5020, 5030, 5040, 5045, 5050, 5060, 5070, 5080,  # TV
]


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_categories(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        return list(DEFAULT_CATEGORIES)
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out or list(DEFAULT_CATEGORIES)


def parse_indexers(raw: str | list | None) -> list[dict]:
    """Parse PROWLARR_NEWZNAB_INDEXERS JSON into normalised rows."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(data, list):
        return []
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("baseUrl") or "").strip().rstrip("/")
        api_key = str(item.get("api_key") or item.get("apiKey") or "").strip()
        if not url or not api_key:
            continue
        if url.lower() in {"https://api.example.com", "http://localhost"}:
            continue
        name = str(item.get("name") or "Newznab").strip() or "Newznab"
        api_path = str(item.get("api_path") or item.get("apiPath") or "/api").strip() or "/api"
        if not api_path.startswith("/"):
            api_path = "/" + api_path
        rows.append({
            "name": name,
            "url": url,
            "api_key": api_key,
            "api_path": api_path,
            "enable_rss": _as_bool(item.get("enable_rss", item.get("enableRss")), True),
            "enable_automatic_search": _as_bool(
                item.get("enable_automatic_search", item.get("enableAutomaticSearch")), True
            ),
            "enable_interactive_search": _as_bool(
                item.get("enable_interactive_search", item.get("enableInteractiveSearch")), True
            ),
            "categories": _as_categories(item.get("categories")),
        })
    return rows


def serialize_indexers(rows: list[dict]) -> str:
    return json.dumps(rows, separators=(",", ":"))


def merge_indexers(incoming: list[dict], existing: list[dict]) -> list[dict]:
    """Keep prior api_key when the UI posts a blank secret."""
    by_name = {r["name"].lower(): r for r in existing}
    by_url = {r["url"].rstrip("/").lower(): r for r in existing}
    merged: list[dict] = []
    for row in incoming:
        prev = by_name.get(row["name"].lower()) or by_url.get(row["url"].lower())
        api_key = row.get("api_key") or ""
        if (not api_key or set(api_key) <= {"*"}) and prev:
            row = {**row, "api_key": prev["api_key"]}
        if not row.get("api_key"):
            continue
        merged.append(row)
    return merged


def newznab_payload(row: dict) -> dict:
    return {
        "enable": True,
        "enableRss": bool(row.get("enable_rss", True)),
        "enableAutomaticSearch": bool(row.get("enable_automatic_search", True)),
        "enableInteractiveSearch": bool(row.get("enable_interactive_search", True)),
        "priority": 25,
        "appProfileId": 1,
        "downloadClientId": 0,
        "redirect": True,
        "name": row["name"],
        "implementation": "Newznab",
        "configContract": "NewznabSettings",
        "protocol": "usenet",
        "tags": [],
        "fields": [
            {"name": "baseUrl", "value": row["url"]},
            {"name": "apiPath", "value": row.get("api_path") or "/api"},
            {"name": "apiKey", "value": row["api_key"]},
            {"name": "categories", "value": list(row.get("categories") or DEFAULT_CATEGORIES)},
        ],
    }


def _field_map(item: dict) -> dict[str, Any]:
    return {
        f["name"]: f.get("value")
        for f in (item.get("fields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def ensure_newznab_indexers(
    client,
    rows: list[dict],
    log: Callable[[str], None],
) -> list[str]:
    """Create/update named Newznab indexers. Returns action labels."""
    if not rows:
        return []
    existing = client.get("indexer")
    if not isinstance(existing, list):
        raise RuntimeError("prowlarr indexer list unexpected")
    by_name = {
        str(item.get("name") or "").strip().lower(): item
        for item in existing
        if str(item.get("implementation") or "") == "Newznab"
    }
    actions: list[str] = []
    for row in rows:
        payload = newznab_payload(row)
        current = by_name.get(row["name"].lower())
        if current is None:
            client.post("indexer?forceSave=true", payload)
            actions.append(f"created:{row['name']}")
            log(f"prowlarr: newznab {row['name']} (created)")
            continue
        fields = _field_map(current)
        same = (
            fields.get("baseUrl") == row["url"]
            and fields.get("apiPath") == (row.get("api_path") or "/api")
            and fields.get("apiKey") == row["api_key"]
            and list(fields.get("categories") or []) == list(row.get("categories") or [])
            and current.get("enableRss") == payload["enableRss"]
            and current.get("enableAutomaticSearch") == payload["enableAutomaticSearch"]
            and current.get("enableInteractiveSearch") == payload["enableInteractiveSearch"]
        )
        if same:
            actions.append(f"noop:{row['name']}")
            continue
        body = {**current, **payload, "id": current["id"], "fields": payload["fields"]}
        client.put(f"indexer/{current['id']}?forceSave=true", body)
        actions.append(f"updated:{row['name']}")
        log(f"prowlarr: newznab {row['name']} (updated)")
    return actions


def configure(log: Callable[[str], None]) -> None:
    """Apply env-configured Newznab indexers to a running Prowlarr."""
    from arrclient import ArrClient, http_error_detail
    from keys import resolve_key
    import tunnel_hosts

    rows = parse_indexers(os.environ.get(ENV_KEY, ""))
    if not rows:
        return
    client = ArrClient(tunnel_hosts.internal_base_for_app("prowlarr", 9696), resolve_key("prowlarr"), api="v1", timeout=120.0)
    if not client.wait(timeout=60):
        log("prowlarr: newznab skipped (no API)")
        return
    try:
        ensure_newznab_indexers(client, rows, log)
    except Exception as exc:  # noqa: BLE001 — wire continues for other recipes
        detail = http_error_detail(exc) if hasattr(exc, "response") else str(exc)
        log(f"prowlarr: newznab failed ({detail})")
