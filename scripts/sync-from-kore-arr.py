#!/usr/bin/env python3
"""One-shot: copy Althub Newznab settings, monitored flags, and missing library
items from kore *arr to kine.

Dry-run by default. Pass --apply to write.

From osiris (LAN to kore, gluetun IP to kine *arr):

  python3 scripts/sync-from-kore-arr.py \\
    --source-sonarr http://10.100.100.90:8989 --source-sonarr-key "$KORE_SONARR_KEY" \\
    --source-radarr http://10.100.100.90:7878 --source-radarr-key "$KORE_RADARR_KEY" \\
    --dest-sonarr http://"$GLUETUN_IP":8989 --dest-sonarr-key "$KINE_SONARR_KEY" \\
    --dest-radarr http://"$GLUETUN_IP":7878 --dest-radarr-key "$KINE_RADARR_KEY"

  python3 scripts/sync-from-kore-arr.py ... --apply
  python3 scripts/sync-from-kore-arr.py ... --add-missing --apply
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
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


def _profile_name_map(src: ArrApi, dst: ArrApi) -> tuple[dict[int, int], int]:
    """Map source qualityProfileId → dest id by name; fallback = most-used dest id."""
    src_profiles = src.get("qualityprofile")
    dst_profiles = dst.get("qualityprofile")
    if not isinstance(src_profiles, list) or not isinstance(dst_profiles, list):
        raise RuntimeError("unexpected qualityprofile payload")
    dst_by_name = {p["name"]: int(p["id"]) for p in dst_profiles if p.get("name") and p.get("id")}
    mapping = {
        int(p["id"]): dst_by_name[p["name"]]
        for p in src_profiles
        if p.get("id") is not None and p.get("name") in dst_by_name
    }
    fallback = int(dst_profiles[0]["id"]) if dst_profiles else 1
    return mapping, fallback


def _most_common_profile(items: list[dict], fallback: int) -> int:
    counts = Counter(int(i["qualityProfileId"]) for i in items if i.get("qualityProfileId"))
    return counts.most_common(1)[0][0] if counts else fallback


def _root_path(dst: ArrApi) -> str:
    roots = dst.get("rootfolder")
    if not isinstance(roots, list) or not roots:
        raise RuntimeError("no root folder configured on dest")
    path = roots[0].get("path")
    if not path:
        raise RuntimeError("dest root folder missing path")
    return str(path)


def sync_missing_movies(src: ArrApi, dst: ArrApi, *, apply: bool, search: bool) -> dict:
    """Add kore monitored+!hasFile movies that are absent from kine."""
    source_items = src.get("movie")
    dest_items = dst.get("movie")
    if not isinstance(source_items, list) or not isinstance(dest_items, list):
        raise RuntimeError("radarr: unexpected movie payload")

    dest_ids = {m.get("tmdbId") for m in dest_items if m.get("tmdbId")}
    to_add = [
        m
        for m in source_items
        if m.get("monitored")
        and not m.get("hasFile")
        and m.get("tmdbId")
        and m["tmdbId"] not in dest_ids
    ]

    mapping, fb = _profile_name_map(src, dst)
    fallback = _most_common_profile(dest_items, fb)
    root = _root_path(dst)
    added: list[str] = []
    errors: list[str] = []

    for movie in to_add:
        tmdb_id = int(movie["tmdbId"])
        title = movie.get("title") or str(tmdb_id)
        src_qp = int(movie.get("qualityProfileId") or 0)
        qp = mapping.get(src_qp, fallback)
        payload = {
            "title": movie.get("title"),
            "tmdbId": tmdb_id,
            "year": movie.get("year"),
            "qualityProfileId": qp,
            "rootFolderPath": root,
            "monitored": True,
            "minimumAvailability": movie.get("minimumAvailability") or "released",
            "tags": movie.get("tags") or [],
            "addOptions": {"searchForMovie": search},
        }
        # Prefer full lookup metadata so Radarr gets images/path slug.
        try:
            looked = dst.get(f"movie/lookup/tmdb?tmdbId={tmdb_id}")
            if isinstance(looked, dict) and looked.get("tmdbId"):
                payload = {
                    **looked,
                    "qualityProfileId": qp,
                    "rootFolderPath": root,
                    "monitored": True,
                    "minimumAvailability": movie.get("minimumAvailability")
                    or looked.get("minimumAvailability")
                    or "released",
                    "tags": movie.get("tags") or [],
                    "addOptions": {"searchForMovie": search},
                }
                for drop in ("id", "path", "folderName", "movieFile", "hasFile", "sizeOnDisk"):
                    payload.pop(drop, None)
        except RuntimeError as exc:
            errors.append(f"{title}: lookup failed ({exc})")
            continue

        if apply:
            try:
                dst.post("movie", payload)
            except RuntimeError as exc:
                errors.append(f"{title}: {exc}")
                continue
        added.append(title)

    return {
        "label": "radarr-add-missing",
        "candidates": len(to_add),
        "added": len(added),
        "root": root,
        "search": search,
        "samples": added[:12],
        "errors": errors[:8],
    }


def sync_missing_series(src: ArrApi, dst: ArrApi, *, apply: bool, search: bool) -> dict:
    """Add kore series that are absent from kine (by tvdbId)."""
    source_items = src.get("series")
    dest_items = dst.get("series")
    if not isinstance(source_items, list) or not isinstance(dest_items, list):
        raise RuntimeError("sonarr: unexpected series payload")

    dest_ids = {s.get("tvdbId") for s in dest_items if s.get("tvdbId")}
    to_add = [s for s in source_items if s.get("tvdbId") and s["tvdbId"] not in dest_ids]

    mapping, fb = _profile_name_map(src, dst)
    fallback = _most_common_profile(dest_items, fb)
    root = _root_path(dst)

    # Language profile (Sonarr v3); optional on v4+.
    lang_id = None
    try:
        langs = dst.get("languageprofile")
        if isinstance(langs, list) and langs:
            lang_id = langs[0].get("id")
    except RuntimeError:
        lang_id = None

    added: list[str] = []
    errors: list[str] = []

    for series in to_add:
        tvdb_id = int(series["tvdbId"])
        title = series.get("title") or str(tvdb_id)
        src_qp = int(series.get("qualityProfileId") or 0)
        qp = mapping.get(src_qp, fallback)
        src_seasons = {
            int(sea["seasonNumber"]): bool(sea.get("monitored"))
            for sea in (series.get("seasons") or [])
            if sea.get("seasonNumber") is not None
        }
        term = urllib.parse.quote(f"tvdb:{tvdb_id}")
        try:
            looked_list = dst.get(f"series/lookup?term={term}")
        except RuntimeError as exc:
            errors.append(f"{title}: lookup failed ({exc})")
            continue
        if not isinstance(looked_list, list) or not looked_list:
            errors.append(f"{title}: lookup empty")
            continue
        looked = next((x for x in looked_list if x.get("tvdbId") == tvdb_id), looked_list[0])
        seasons = []
        for sea in looked.get("seasons") or []:
            sn = sea.get("seasonNumber")
            if sn is None:
                continue
            monitored = src_seasons.get(int(sn), bool(sea.get("monitored")))
            seasons.append({**sea, "monitored": monitored})

        payload = {
            **looked,
            "qualityProfileId": qp,
            "rootFolderPath": root,
            "monitored": bool(series.get("monitored", True)),
            "seasonFolder": bool(series.get("seasonFolder", True)),
            "seriesType": series.get("seriesType") or looked.get("seriesType") or "standard",
            "seasons": seasons,
            "tags": series.get("tags") or [],
            "addOptions": {
                "monitor": "all" if series.get("monitored", True) else "none",
                "searchForMissingEpisodes": search,
                "searchForCutoffUnmetEpisodes": False,
            },
        }
        if lang_id is not None:
            payload["languageProfileId"] = lang_id
        if series.get("monitorNewItems"):
            payload["monitorNewItems"] = series["monitorNewItems"]
        for drop in ("id", "path", "statistics", "previousAiring", "nextAiring"):
            payload.pop(drop, None)

        if apply:
            try:
                try:
                    dst.post("series", payload)
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "titleslug" not in msg and "title slug" not in msg:
                        raise
                    # Sonarr derives slug from title; same-name different-TVDB needs a
                    # distinct title (slug-only overrides are ignored).
                    year = series.get("year") or looked.get("year")
                    if not year:
                        raise
                    payload["title"] = f"{looked.get('title') or title} ({year})"
                    payload["titleSlug"] = f"{looked.get('titleSlug') or 'series'}-{year}"
                    try:
                        dst.post("series", payload)
                    except RuntimeError:
                        # Dest may already own the bare slug under another TVDB id.
                        errors.append(
                            f"{title}: title slug conflict with existing series "
                            f"(tvdb {tvdb_id}); add manually after renaming the other"
                        )
                        continue
            except RuntimeError as exc:
                errors.append(f"{title}: {exc}")
                continue
        added.append(title)

    return {
        "label": "sonarr-add-missing",
        "candidates": len(to_add),
        "added": len(added),
        "root": root,
        "search": search,
        "samples": added[:12],
        "errors": errors[:8],
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
    p.add_argument(
        "--add-missing",
        action="store_true",
        help="Add kore missing movies + series absent from kine",
    )
    p.add_argument(
        "--search",
        action="store_true",
        help="With --add-missing, trigger search on add (default: no search)",
    )
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
    if not args.skip_althub and not args.add_missing:
        results.append(sync_althub(src_sonarr, dst_sonarr, apply=args.apply, label="sonarr-althub", api_key=args.althub_api_key or None))
        results.append(sync_althub(src_radarr, dst_radarr, apply=args.apply, label="radarr-althub", api_key=args.althub_api_key or None))
    if not args.skip_monitored and not args.add_missing:
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
    if args.add_missing:
        results.append(
            sync_missing_movies(src_radarr, dst_radarr, apply=args.apply, search=args.search)
        )
        results.append(
            sync_missing_series(src_sonarr, dst_sonarr, apply=args.apply, search=args.search)
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
