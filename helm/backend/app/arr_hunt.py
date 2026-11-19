"""Search a small batch of missing Sonarr/Radarr/Lidarr items.

RSS only sees newly posted releases. Anything already on the indexer
sits in Wanted until someone clicks Search. This hunts a few of those
on a schedule, and backs off when the download queue is already deep.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from . import appkeys, catalogue, config, tunnel_hosts

QUEUE_LIMIT = 25
BATCH = 8
ARR_API = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1"}
ARR_PORTS = {"sonarr": 8989, "radarr": 7878, "lidarr": 8686}
APPS = ("sonarr", "radarr", "lidarr")
# Lidarr's wanted list can hold hundreds of albums that no indexer has. Taking
# the newest few every run would retry the same ones forever, so each run
# starts further down the list (one step of BATCH per ROTATE_SECONDS slot).
ROTATE_SECONDS = 6 * 3600
LIDARR_PAGE = 500


def should_skip(queue_total: int, limit: int = QUEUE_LIMIT) -> bool:
    return queue_total >= limit


def queued_episode_ids(records: list[dict]) -> set[int]:
    out: set[int] = set()
    for row in records:
        eid = row.get("episodeId")
        if isinstance(eid, int):
            out.add(eid)
        for episode in row.get("episodes") or []:
            epid = episode.get("id")
            if isinstance(epid, int):
                out.add(epid)
    return out


def queued_movie_ids(records: list[dict]) -> set[int]:
    out: set[int] = set()
    for row in records:
        mid = row.get("movieId")
        if isinstance(mid, int):
            out.add(mid)
    return out


def queued_album_ids(records: list[dict]) -> set[int]:
    out: set[int] = set()
    for row in records:
        aid = row.get("albumId")
        if isinstance(aid, int):
            out.add(aid)
    return out


def pick_missing_ids(
    records: list[dict],
    queued: set[int],
    *,
    limit: int = BATCH,
    require_season: bool = True,
) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for rec in records:
        if rec.get("monitored") is False:
            continue
        if require_season and rec.get("seasonNumber") == 0:
            continue
        item_id = rec.get("id")
        if not isinstance(item_id, int) or item_id in queued or item_id in seen:
            continue
        seen.add(item_id)
        out.append(item_id)
        if len(out) >= limit:
            break
    return out


def plan_sonarr(
    missing: dict,
    queue: dict,
    *,
    queue_limit: int = QUEUE_LIMIT,
    batch: int = BATCH,
) -> list[int] | None:
    if should_skip(int(queue.get("totalRecords") or 0), limit=queue_limit):
        return None
    queued = queued_episode_ids(queue.get("records") or [])
    return pick_missing_ids(missing.get("records") or [], queued, limit=batch)


def plan_radarr(
    missing: dict,
    queue: dict,
    *,
    queue_limit: int = QUEUE_LIMIT,
    batch: int = BATCH,
) -> list[int] | None:
    if should_skip(int(queue.get("totalRecords") or 0), limit=queue_limit):
        return None
    queued = queued_movie_ids(queue.get("records") or [])
    return pick_missing_ids(
        missing.get("records") or [], queued, limit=batch, require_season=False,
    )


def plan_lidarr(
    missing: dict,
    queue: dict,
    *,
    queue_limit: int = QUEUE_LIMIT,
    batch: int = BATCH,
    slot: int | None = None,
) -> list[int] | None:
    """Pick albums to search, rotating through the whole wanted list.

    ``slot`` is the number of ROTATE_SECONDS periods since the epoch, so
    consecutive runs take consecutive groups of ``batch`` albums and wrap
    round. Tests pass it explicitly.
    """
    if should_skip(int(queue.get("totalRecords") or 0), limit=queue_limit):
        return None
    queued = queued_album_ids(queue.get("records") or [])
    candidates = pick_missing_ids(
        missing.get("records") or [],
        queued,
        limit=len(missing.get("records") or []),
        require_season=False,
    )
    if not candidates:
        return []
    if slot is None:
        slot = int(time.time() // ROTATE_SECONDS)
    start = (slot * batch) % len(candidates)
    count = min(batch, len(candidates))
    return [candidates[(start + i) % len(candidates)] for i in range(count)]


def episode_search_payload(episode_ids: list[int]) -> dict:
    return {"name": "EpisodeSearch", "episodeIds": episode_ids}


def movie_search_payload(movie_ids: list[int]) -> dict:
    return {"name": "MoviesSearch", "movieIds": movie_ids}


def album_search_payload(album_ids: list[int]) -> dict:
    return {"name": "AlbumSearch", "albumIds": album_ids}


def queue_params(app: str) -> dict:
    params: dict = {"page": 1, "pageSize": 100}
    if app == "sonarr":
        params["includeUnknownSeriesItems"] = True
    elif app == "lidarr":
        params["includeUnknownArtistItems"] = True
    else:
        params["includeUnknownMovieItems"] = True
    return params


def missing_params(app: str) -> dict:
    sort_key = {"sonarr": "airDateUtc", "radarr": "year", "lidarr": "releaseDate"}[app]
    return {
        "page": 1,
        # Lidarr rotates through the whole list, so it needs all of it.
        "pageSize": LIDARR_PAGE if app == "lidarr" else 100,
        "sortKey": sort_key,
        "sortDirection": "descending",
    }


def _base(app: str) -> str:
    entry = catalogue.load().get(app, {})
    return tunnel_hosts.runtime_internal(app, entry) or tunnel_hosts.internal_base_for_app(
        app, ARR_PORTS[app]
    )


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None):
    response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json() if response.content else {}


async def _hunt_one(app: str) -> dict:
    key = appkeys.key_for(app)
    base = _base(app)
    if not key or not base:
        return {"skipped": "no api"}
    api = ARR_API[app]
    headers = {"X-Api-Key": key}
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        base_url=f"{base}/api/{api}",
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        queue = await _get_json(client, "/queue", queue_params(app))
        if not isinstance(queue, dict):
            queue = {"totalRecords": 0, "records": []}
        missing = await _get_json(client, "/wanted/missing", missing_params(app))
        if not isinstance(missing, dict):
            missing = {"records": []}
        planners = {"sonarr": plan_sonarr, "radarr": plan_radarr, "lidarr": plan_lidarr}
        ids = planners[app](missing, queue)
        if ids is None:
            return {
                "skipped": "queue",
                "queue": int(queue.get("totalRecords") or 0),
                "missing": int(missing.get("totalRecords") or 0),
            }
        if not ids:
            return {
                "searched": 0,
                "queue": int(queue.get("totalRecords") or 0),
                "missing": int(missing.get("totalRecords") or 0),
            }
        payloads = {
            "sonarr": episode_search_payload,
            "radarr": movie_search_payload,
            "lidarr": album_search_payload,
        }
        payload = payloads[app](ids)
        response = await client.post("/command", json=payload)
        response.raise_for_status()
        return {
            "searched": len(ids),
            "queue": int(queue.get("totalRecords") or 0),
            "missing": int(missing.get("totalRecords") or 0),
        }


async def hunt_missing() -> dict:
    profiles = set(config.profiles())
    result: dict = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
    }
    for app in APPS:
        if app not in profiles:
            result[app] = {"skipped": "disabled"}
            continue
        try:
            result[app] = await _hunt_one(app)
        except Exception as exc:  # noqa: BLE001 — one app must not abort the other
            result[app] = {"ok": False, "error": str(exc)}
            result["ok"] = False
    return result
