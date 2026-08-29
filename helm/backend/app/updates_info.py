"""Structured image update rows for the Helm Updates page."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from . import catalogue, channels, config

_APPLY_STEP = re.compile(r"^(\d+)[a-z]?/(\d+)\s+(.*)$")
_check: dict | None = None
_apply: dict[str, dict] = {}


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
            "tier": meta.get("tier") or "",
        }
        out.append(item)
    return out


def catalogue_apps(rows: list[dict]) -> list[dict]:
    """Helm's Updates page is for catalogue apps, not stack plumbing."""
    return [r for r in rows if not r.get("core")]


def pending_ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows if r.get("update_available")]


def text_report(rows: list[dict]) -> str:
    lines = [f"{'APP':<14} {'STATUS':<12} IMAGE"]
    for row in rows:
        status = "UPDATE" if row.get("update_available") else "current"
        lines.append(f"{row['id']:<14} {status:<12} {row.get('image', '')}")
    return "\n".join(lines) + "\n"


def parse_check_output(text: str) -> list[dict]:
    """Accept NDJSON progress+result lines, or a legacy JSON array."""
    text = (text or "").strip()
    if not text:
        return []
    rows = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            rows = obj
        elif isinstance(obj, dict) and obj.get("type") == "result":
            rows = obj.get("rows") or []
    if rows is not None:
        return rows
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict) and obj.get("type") == "result":
        return obj.get("rows") or []
    return []


def parse_apply_line(line: str) -> dict | None:
    """Parse `1/4 Snapshotting...` / `3c/4 Healing...` from updates.sh apply."""
    m = _APPLY_STEP.match((line or "").strip())
    if not m:
        return None
    step, steps = int(m.group(1)), int(m.group(2))
    message = m.group(3)
    pct = 90 if steps and step >= steps else (
        round(100 * step / steps) if steps else 0)
    return {"step": step, "steps": steps, "pct": pct, "message": message}


def progress() -> dict:
    apps = {k: dict(v) for k, v in _apply.items()}
    check = dict(_check) if _check else None
    busy = bool(check or apps)
    if check:
        kind = "check"
        primary = check
    elif apps:
        kind = "apply"
        app_id, job = next(iter(apps.items()))
        primary = {**job, "id": app_id, "kind": "apply", "busy": True}
    else:
        kind = ""
        primary = {
            "busy": False, "kind": "", "current": 0, "total": 0,
            "id": "", "message": "", "pct": 0,
        }
    return {
        "busy": busy,
        "kind": kind,
        "apps": apps,
        "check": check,
        "current": primary.get("current", 0),
        "total": primary.get("total", 0),
        "id": primary.get("id", ""),
        "message": primary.get("message", ""),
        "pct": primary.get("pct", 0),
    }


def clear_progress(app_id: str | None = None) -> None:
    global _check
    if app_id:
        _apply.pop(app_id, None)
        return
    _check = None
    _apply.clear()


def clear_check_progress() -> None:
    global _check
    _check = None


def set_check_progress(*, current: int, total: int, app_id: str) -> None:
    global _check
    pct = round(100 * current / total) if total else 0
    _check = {
        "busy": True,
        "kind": "check",
        "current": current,
        "total": total,
        "id": app_id,
        "message": f"Checking {app_id}…" if app_id else "Checking registries…",
        "pct": pct,
    }


def set_apply_progress(*, app_id: str, step: int, steps: int, message: str) -> None:
    pct = 90 if steps and step >= steps else (
        round(100 * step / steps) if steps else 0)
    _apply[app_id] = {
        "busy": True,
        "kind": "apply",
        "current": step,
        "total": steps,
        "id": app_id,
        "message": message,
        "pct": pct,
        "step": step,
        "steps": steps,
    }


def _note_check_line(line: str) -> None:
    try:
        obj = json.loads((line or "").strip())
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict) and obj.get("type") == "progress":
        set_check_progress(
            current=int(obj.get("current") or 0),
            total=int(obj.get("total") or 0),
            app_id=str(obj.get("id") or ""),
        )


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
                containers = catalogue_apps(enrich(cached["containers"]))
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
                containers = catalogue_apps(enrich(rows))
                return {
                    "ok": cached["ok"],
                    "containers": containers,
                    "pending": pending_ids(containers),
                    "checked": cached.get("checked"),
                    "report": cached.get("report", ""),
                    "cached": True,
                }

    set_check_progress(current=0, total=0, app_id="")
    try:
        run_script = getattr(compose, "script_with_callback", None)
        if run_script is not None:
            code, out = await run_script(
                "updates.sh", "check-json", timeout=300, on_line=_note_check_line)
        else:
            code, out = await compose.script("updates.sh", "check-json", timeout=300)
        rows = parse_check_output(out)
        if not rows:
            try:
                parsed = json.loads(out) if (out or "").strip() else []
                rows = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                rows = []
        if not rows:
            code, text_out = await compose.script("updates.sh", "check", timeout=300)
            rows = parse_report(text_out)
            out = text_out

        ps_code, ps_out = await compose.run("ps", "--format", "json", timeout=60)
        running = parse_running(ps_out) if ps_code == 0 else set()
        containers = catalogue_apps(enrich(rows, running=running))
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
    finally:
        clear_check_progress()
