#!/usr/bin/env python3
"""One-shot: copy Althub Newznab settings and monitored flags from kore *arr to kine.

Dry-run by default. Pass --apply to write.

From osiris (LAN to kore, gluetun IP to kine *arr):

  python3 scripts/sync-from-kore-arr.py \\
    --source-sonarr http://10.100.100.90:8989 --source-sonarr-key "$KORE_SONARR_KEY" \\
    --source-radarr http://10.100.100.90:7878 --source-radarr-key "$KORE_RADARR_KEY" \\
    --dest-sonarr http://"$GLUETUN_IP":8989 --dest-sonarr-key "$KINE_SONARR_KEY" \\
    --dest-radarr http://"$GLUETUN_IP":7878 --dest-radarr-key "$KINE_RADARR_KEY"

  python3 scripts/sync-from-kore-arr.py ... --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


ALTHUB_NAME = "althub"


class ArrApi:
    def __init__(self, base: str, key: str, timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base}/api/v3/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-Api-Key": self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body)

    def put(self, path: str, body: dict) -> Any:
        return self._request("PUT", path, body)


def _field_map(item: dict) -> dict[str, Any]:
    return {
        f["name"]: f.get("value")
        for f in (item.get("fields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def find_althub(indexers: list[dict]) -> dict | None:
    for item in indexers:
        name = str(item.get("name") or "").strip().lower()
        fields = _field_map(item)
        base = str(fields.get("baseUrl") or "").lower()
        if name == ALTHUB_NAME or "althub" in base:
            return item
    return None


def _is_redacted_secret(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return True
    stripped = value.strip()
    return set(stripped) <= {"*"} or stripped.lower() in {"********", "redacted"}


def indexer_payload_from_source(source: dict, api_key: str | None = None) -> dict:
    """Build a create/update payload without source ids."""
    fields = []
    for field in source.get("fields") or []:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        value = field.get("value")
        if field["name"] == "apiKey" and (_is_redacted_secret(value) or api_key):
            if not api_key:
                raise RuntimeError(
                    "Althub API key is redacted by the *arr API; pass --althub-api-key"
                )
            value = api_key
        fields.append({"name": field["name"], "value": value})
    return {
        "enable": source.get("enable", True),
        "enableRss": source.get("enableRss", True),
        "enableAutomaticSearch": source.get("enableAutomaticSearch", True),
        "enableInteractiveSearch": source.get("enableInteractiveSearch", True),
        "priority": source.get("priority", 25),
        "name": source.get("name") or "Althub",
        "implementation": source.get("implementation") or "Newznab",
        "configContract": source.get("configContract") or "NewznabSettings",
        "protocol": source.get("protocol") or "usenet",
        "tags": source.get("tags") or [],
        "fields": fields,
    }


def sync_althub(src: ArrApi, dst: ArrApi, *, apply: bool, label: str, api_key: str | None) -> dict:
    source_list = src.get("indexer")
    dest_list = dst.get("indexer")
    if not isinstance(source_list, list) or not isinstance(dest_list, list):
        raise RuntimeError(f"{label}: unexpected indexer payload")
    source = find_althub(source_list)
    if not source:
        return {"label": label, "action": "skip", "reason": "no Althub on source"}
    dest = find_althub(dest_list)
    payload = indexer_payload_from_source(source, api_key=api_key)
    src_fields = _field_map(source)
    dst_fields = _field_map(dest) if dest else {}
    dest_key = dst_fields.get("apiKey")
    same_key = bool(dest) and api_key and not _is_redacted_secret(dest_key) and dest_key == api_key and (
        src_fields.get("baseUrl") == dst_fields.get("baseUrl")
    )
    if dest and same_key:
        # Still refresh categories / search flags if they differ.
        need = (
            src_fields.get("categories") != dst_fields.get("categories")
            or source.get("enableRss") != dest.get("enableRss")
            or source.get("enableAutomaticSearch") != dest.get("enableAutomaticSearch")
            or source.get("enableInteractiveSearch") != dest.get("enableInteractiveSearch")
            or source.get("priority") != dest.get("priority")
        )
        if not need:
            return {"label": label, "action": "noop", "name": payload["name"]}
        action = "update"
    elif dest:
        action = "update"
    else:
        action = "create"

    if apply:
        if action == "create":
            # forceSave skips the live Newznab probe (VPN egress can fail it).
            dst.post("indexer?forceSave=true", payload)
        else:
            body = {**dest, **payload, "id": dest["id"], "fields": payload["fields"]}
            dst.put(f"indexer/{dest['id']}?forceSave=true", body)
    return {
        "label": label,
        "action": action,
        "name": payload["name"],
        "baseUrl": src_fields.get("baseUrl"),
        "categories": src_fields.get("categories"),
    }


def sync_monitored(
    src: ArrApi,
    dst: ArrApi,
    *,
    resource: str,
    id_field: str,
    apply: bool,
    label: str,
) -> dict:
    source_items = src.get(resource)
    dest_items = dst.get(resource)
    if not isinstance(source_items, list) or not isinstance(dest_items, list):
        raise RuntimeError(f"{label}: unexpected {resource} payload")

    by_id: dict[Any, dict] = {}
    for item in source_items:
        key = item.get(id_field)
        if key:
            by_id[key] = item

    would_monitor = []
    would_unmonitor = []
    matched = 0
    missing_on_dest = 0
    missing_id = 0

    for dest in dest_items:
        key = dest.get(id_field)
        if not key:
            missing_id += 1
            continue
        source = by_id.get(key)
        if not source:
            missing_on_dest += 1  # on dest but not source — leave alone
            continue
        matched += 1
        src_mon = bool(source.get("monitored"))
        dst_mon = bool(dest.get("monitored"))
        if src_mon == dst_mon:
            continue
        title = source.get("title") or source.get("sortTitle") or str(key)
        entry = {"id": dest["id"], "key": key, "title": title, "monitored": src_mon}
        if src_mon:
            would_monitor.append(entry)
        else:
            would_unmonitor.append(entry)
        if apply:
            body = {**dest, "monitored": src_mon}
            dst.put(f"{resource}/{dest['id']}", body)

    dest_ids = {d.get(id_field) for d in dest_items if d.get(id_field)}
    only_on_source = sum(1 for k in by_id if k not in dest_ids)
    return {
        "label": label,
        "matched": matched,
        "would_monitor": len(would_monitor),
        "would_unmonitor": len(would_unmonitor),
        "only_on_source": only_on_source,
        "only_on_dest": missing_on_dest,
        "dest_missing_id": missing_id,
        "samples_monitor": [e["title"] for e in would_monitor[:8]],
        "samples_unmonitor": [e["title"] for e in would_unmonitor[:8]],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    p.add_argument("--althub-api-key", default="",
                   help="Real Althub key; *arr GET redacts indexer secrets")
    p.add_argument("--skip-althub", action="store_true")
    p.add_argument("--skip-monitored", action="store_true")
    for side in ("source", "dest"):
        for app in ("sonarr", "radarr"):
            p.add_argument(f"--{side}-{app}", required=True)
            p.add_argument(f"--{side}-{app}-key", required=True)
    args = p.parse_args(argv)

    src_sonarr = ArrApi(args.source_sonarr, args.source_sonarr_key)
    src_radarr = ArrApi(args.source_radarr, args.source_radarr_key)
    dst_sonarr = ArrApi(args.dest_sonarr, args.dest_sonarr_key)
    dst_radarr = ArrApi(args.dest_radarr, args.dest_radarr_key)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== {mode} kore → kine ===")

    results: list[dict] = []
    if not args.skip_althub:
        results.append(sync_althub(src_sonarr, dst_sonarr, apply=args.apply, label="sonarr-althub", api_key=args.althub_api_key or None))
        results.append(sync_althub(src_radarr, dst_radarr, apply=args.apply, label="radarr-althub", api_key=args.althub_api_key or None))
    if not args.skip_monitored:
        results.append(
            sync_monitored(
                src_sonarr, dst_sonarr,
                resource="series", id_field="tvdbId",
                apply=args.apply, label="sonarr-monitored",
            )
        )
        results.append(
            sync_monitored(
                src_radarr, dst_radarr,
                resource="movie", id_field="tmdbId",
                apply=args.apply, label="radarr-monitored",
            )
        )

    for row in results:
        print(json.dumps(row, sort_keys=True))
    if not args.apply:
        print("Dry-run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
