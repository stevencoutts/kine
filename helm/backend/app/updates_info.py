"""Structured image update rows for the Helm Updates page."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import catalogue, channels, config


def parse_report(text: str) -> list[dict]:
    """Parse plain-text `updates.sh check` output (legacy scheduler cache)."""
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("APP ") or line.startswith("APP\t"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        app_id, status, image = parts[0], parts[1].lower(), parts[2]
        update = status == "update"
        rows.append({
            "id": app_id,
            "image": image,
            "tag": image.rsplit(":", 1)[-1] if ":" in image else "latest",
            "local_digest": None,
            "remote_digest": None,
            "update_available": update,
            "status": "update" if update else "current",
        })
    return rows


def enrich(rows: list[dict], *, running: set[str] | None = None) -> list[dict]:
    """Join digest rows with channel tags and catalogue names."""
    env = config.read()
    cat = catalogue.load()
    dev_on = set(channels.channels())
    enabled = set(config.profiles())

    out: list[dict] = []
    for row in rows:
        app_id = row["id"]
        meta = cat.get(app_id) or {}
        tkey = channels.tag_key(app_id)
        env_tag = env.get(tkey, "").strip()
        channel = "dev" if app_id in dev_on else "prod"
        # Hidden catalogue entries are plumbing; anything compose lists that
        # is not in the catalogue (Traefik, Helm, …) is plumbing too.
        core = bool(meta.get("hidden")) if app_id in cat else True
        if running is None:
            is_running = bool(row.get("running"))
        else:
            is_running = app_id in running
        # Helm talks to Docker through dockerproxy; applying an update to
        # it from Helm stops the proxy mid-recreate and leaves it down.
        host_only = app_id == "dockerproxy"
        # Catalogue apps (including hidden metrics deps) are profile-gated.
        # Always-on plumbing (Traefik, Helm, dockerproxy, …) is not in the
        # catalogue and has no COMPOSE_PROFILES entry — treat as enabled.
        is_enabled = (app_id in enabled) if (app_id in cat) else True
        item = {
            **row,
            "name": meta.get("name", app_id),
            "channel": channel,
            "configured_tag": env_tag or row.get("tag") or "latest",
            "stable_tag": env.get(channels.stable_tag_key(app_id), "") if app_id in dev_on else "",
            "dev_supported": channels.supported(meta),
            "enabled": is_enabled,
            "running": is_running,
            "core": core,
            "host_only": host_only,
        }
        out.append(item)
    return out


def pending_ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows if r.get("update_available")]


def text_report(rows: list[dict]) -> str:
    lines = [f"{'APP':<14} {'STATUS':<12} IMAGE"]
    for row in rows:
        status = "UPDATE" if row.get("update_available") else "current"
        lines.append(f"{row['id']:<14} {status:<12} {row.get('image', '')}")
    return "\n".join(lines) + "\n"


def parse_running(ps_json: str) -> set[str]:
    running: set[str] = set()
    if not ps_json.strip():
        return set()
    for line in ps_json.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = (entry.get("Name") or entry.get("Service") or "").removeprefix("kine-")
        state = (entry.get("State") or entry.get("Status") or "").lower()
        if name and ("running" in state or state == "up"):
            running.add(name)
    return set(running)


async def fetch(compose, *, refresh: bool = False) -> dict:
    """Run digest check and return structured payload."""
    from . import scheduler

    if not refresh:
        cached = scheduler.status().get("updates")
        if cached:
            if cached.get("containers"):
                # Re-enrich so new fields (core) appear without a registry hit.
                containers = enrich(cached["containers"])
                return {
                    "ok": cached["ok"],
                    "containers": containers,
                    "pending": pending_ids(containers),
                    "checked": cached.get("checked"),
                    "report": cached.get("report", ""),
                    "cached": True,
                }
            if cached.get("report"):
                rows = parse_report(cached["report"])
                containers = enrich(rows)
                return {
                    "ok": cached["ok"],
                    "containers": containers,
                    "pending": pending_ids(containers),
                    "checked": cached.get("checked"),
                    "report": cached.get("report", ""),
                    "cached": True,
                }

    code, out = await compose.script("updates.sh", "check-json", timeout=300)
    try:
        rows = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        code, text_out = await compose.script("updates.sh", "check", timeout=300)
        rows = parse_report(text_out)
        out = text_out

    ps_code, ps_out = await compose.run("ps", "--format", "json", timeout=60)
    running = parse_running(ps_out) if ps_code == 0 else set()
    containers = enrich(rows, running=running)
    pending = pending_ids(containers)
    report = text_report(containers) if containers else (out if isinstance(out, str) else "")

    payload = {
        "ok": code == 0,
        "containers": containers,
        "pending": pending,
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report": report,
        "cached": False,
    }
    # Check Now and cold-cache fills must update the overnight file too,
    # otherwise the next page load reverts to a stale "update" badge.
    scheduler.save_updates(payload)
    return payload
