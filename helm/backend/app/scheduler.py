"""Background jobs.

Two of them, and both are deliberately conservative:

  update-check   resolves image digests and records what has moved.
                 It never pulls and never recreates anything. The
                 decision to apply an update stays with a human,
                 because an unattended update on a media stack breaks
                 things overnight, mid-import, while nobody is watching.

  backup         a scheduled config snapshot, so the rollback path the
                 updater depends on is never more than a day stale.

  seerr-wire     re-runs provision wire when Seerr's wizard has finished
                 but Sonarr/Radarr are not linked yet (enable runs wire
                 before Sign In, so the first link needs a later pass).

  dispatcharr-wire
                 auto-generates a Dispatcharr admin API key when missing,
                 then links Emby HDHomeRun and fills ECM/Teamarr env tokens.
"""
import asyncio
import contextlib
import hashlib
import json
import pathlib
import time
from datetime import datetime, timezone

import httpx
from croniter import croniter

from . import compose, config, dispatcharr_token, metrics, provision_lock, tunnel_hosts, updates_info

STATE = pathlib.Path("/stack/helm-jobs.json")
SEERR_SETTINGS = pathlib.Path("/stack/config/seerr/settings.json")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    with contextlib.suppress(OSError):
        STATE.write_text(json.dumps(data, indent=2))


def _next(expr: str) -> float:
    return croniter(expr, datetime.now(timezone.utc)).get_next(float)


def save_updates(payload: dict) -> None:
    """Persist a digest-check result (overnight job or Check Now)."""
    data = _load()
    data["updates"] = {
        "checked": payload.get("checked"),
        "ok": payload.get("ok", True),
        "pending": payload.get("pending", []),
        "report": payload.get("report", ""),
        "containers": payload.get("containers", []),
    }
    _save(data)


def mark_container_current(app_id: str) -> None:
    """After a successful apply, clear that row without re-querying registries.

    The local digest is set to the remote one we already knew about — that
    is what apply just pulled. Pending and the text report are recomputed
    so the Metrics exporter and the next cached page load stay consistent.
    """
    data = _load()
    updates = data.get("updates") or {}
    containers = list(updates.get("containers") or [])
    found = False
    for row in containers:
        if row.get("id") != app_id:
            continue
        remote = row.get("remote_digest")
        if remote and remote not in ("?", "none"):
            row["local_digest"] = remote
        row["update_available"] = False
        row["status"] = "current"
        found = True
        break
    if not found:
        return
    updates["containers"] = containers
    updates["pending"] = updates_info.pending_ids(containers)
    updates["report"] = updates_info.text_report(containers)
    data["updates"] = updates
    _save(data)


async def _update_check() -> None:
    # fetch(refresh=True) already persists via save_updates.
    await updates_info.fetch(compose, refresh=True)


async def _backup() -> None:
    code, out = await compose.script("backup.sh", timeout=1800)
    if code == 0:
        from . import backups

        await asyncio.to_thread(backups.prune_old_snapshots)
    data = _load()
    data["backup"] = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "path": out.strip().splitlines()[-1] if code == 0 and out.strip() else None,
    }
    _save(data)


def _seerr_api_key() -> str | None:
    if not SEERR_SETTINGS.is_file():
        return None
    try:
        return json.loads(SEERR_SETTINGS.read_text()).get("main", {}).get("apiKey") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


async def _seerr_needs_wire() -> bool:
    if "seerr" not in config.profiles():
        return False
    api_key = _seerr_api_key()
    if not api_key:
        return False
    headers = {"X-Api-Key": api_key}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            me = await client.get("http://seerr:5055/api/v1/auth/me", headers=headers)
            if me.status_code != 200:
                return False
            for path in ("radarr", "sonarr"):
                resp = await client.get(
                    f"http://seerr:5055/api/v1/settings/{path}",
                    headers=headers,
                )
                if resp.status_code != 200 or not resp.json():
                    return True
    except httpx.HTTPError:
        return False
    return False


async def wire_seerr_if_ready() -> None:
    if not await _seerr_needs_wire():
        return
    if provision_lock.status().get("busy"):
        return
    try:
        async with provision_lock.acquire(reason="seerr auto-wire"):
            code, out = await compose.run("run", "--rm", "provision", "wire", timeout=900)
    except provision_lock.ProvisionBusy:
        return
    data = _load()
    data["seerr_wire"] = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "log": out[-500:] if out else "",
    }
    _save(data)


async def _seerr_wire_loop() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await wire_seerr_if_ready()
        except Exception as exc:  # noqa: BLE001 — must not kill the loop
            data = _load()
            data.setdefault("errors", {})["seerr_wire"] = str(exc)
            _save(data)
        await asyncio.sleep(120)


def _dispatcharr_token() -> str | None:
    token = (config.read().get("DISPATCHARR_TOKEN") or "").strip()
    return token or None


def _dispatcharr_env_needs_token() -> bool:
    """True when ecm/teamarr are enabled but still have an empty token."""
    profiles = set(config.profiles())
    for app in ("ecm", "teamarr"):
        if app not in profiles:
            continue
        path = pathlib.Path(f"/stack/config/{app}/{app}.env")
        token = ""
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.startswith("DISPATCHARR_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
        if not token:
            return True
    return False


def _ecm_settings_needs_dispatcharr() -> bool:
    """True when ECM is enabled but settings.json has no API-key connection."""
    if "ecm" not in config.profiles():
        return False
    path = pathlib.Path("/stack/config/ecm/settings.json")
    if not path.is_file():
        return True
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    url = (data.get("url") or "").strip()
    method = (data.get("auth_method") or "").strip()
    key = (
        (data.get("dispatcharr_api_key") or data.get("api_key") or "")
    ).strip()
    return not (url and method == "api_key" and key)


async def _dispatcharr_needs_wire() -> bool:
    if "dispatcharr" not in config.profiles():
        return False
    token = _dispatcharr_token()
    if not token:
        return False
    # Token present: wire if dependents need it, or Emby may need tuner.
    if _dispatcharr_env_needs_token() or _ecm_settings_needs_dispatcharr():
        return True
    if "emby" not in config.profiles():
        return False
    emby_key = (config.read().get("EMBY_API_KEY") or "").strip()
    if not emby_key:
        return False
    # Cheap check: ask Emby if tuner already linked.
    try:
        async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
            resp = await client.get(
                "http://emby:8096/LiveTv/TunerHosts",
                headers={"X-Emby-Token": emby_key},
            )
            if resp.status_code != 200:
                return True
            hosts = resp.json() if resp.content else []
            if not isinstance(hosts, list):
                return True
            want = f"{tunnel_hosts.internal_base_for_app('dispatcharr', 9191)}/hdhr"
            for host in hosts:
                if isinstance(host, dict) and str(host.get("Url") or "").rstrip("/") == want:
                    return False
            return True
    except httpx.HTTPError:
        return True


async def wire_dispatcharr_if_ready() -> None:
    if not await _dispatcharr_needs_wire():
        return
    if provision_lock.status().get("busy"):
        return
    token = _dispatcharr_token() or ""
    # Capture before wire: fingerprint alone misses "token in .env but
    # ecm.env / settings.json still empty" after a stale provision image.
    dependents_needed = (
        _dispatcharr_env_needs_token() or _ecm_settings_needs_dispatcharr()
    )
    try:
        async with provision_lock.acquire(reason="dispatcharr auto-wire"):
            code, out = await compose.run(
                "run", "--rm",
                "-e", f"DISPATCHARR_TOKEN={token}",
                "provision", "wire",
                timeout=900,
            )
    except provision_lock.ProvisionBusy:
        return
    data = _load()
    fingerprint = data.get("dispatcharr_wire", {}).get("token_fp")
    fp = hashlib.sha256(token.encode()).hexdigest()[:16]
    data["dispatcharr_wire"] = {
        "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": code == 0,
        "log": out[-500:] if out else "",
        "token_fp": fp,
    }
    _save(data)
    if code == 0 and (fp != fingerprint or dependents_needed):
        dependents = [a for a in ("ecm", "teamarr") if a in config.profiles()]
        if dependents:
            await compose.run("up", "-d", "--force-recreate", *dependents, timeout=180)


async def _dispatcharr_wire_loop() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            await dispatcharr_token.ensure_token(write_env=True)
            await dispatcharr_token.ensure_login(write_env=True)
            await wire_dispatcharr_if_ready()
        except Exception as exc:  # noqa: BLE001
            data = _load()
            data.setdefault("errors", {})["dispatcharr_wire"] = str(exc)
            _save(data)
        await asyncio.sleep(120)


async def _loop(name: str, cron_key: str, default: str, job) -> None:
    while True:
        expr = (config.read().get(cron_key, default) or default).strip()
        try:
            delay = max(30.0, _next(expr) - time.time())
        except (ValueError, KeyError):
            # A malformed cron expression should not silently disable
            # the job or spin the loop. Fall back and say so in state.
            data = _load()
            data.setdefault("errors", {})[name] = f"invalid cron {expr!r}, using {default!r}"
            _save(data)
            delay = max(30.0, _next(default) - time.time())
        else:
            data = _load()
            if name in data.get("errors", {}):
                data["errors"].pop(name, None)
                if not data["errors"]:
                    data.pop("errors", None)
                _save(data)
        await asyncio.sleep(delay)
        try:
            await job()
        except Exception as exc:  # a failed job must not kill the loop
            data = _load()
            data.setdefault("errors", {})[name] = str(exc)
            _save(data)


def start(app) -> None:
    app.state.jobs = [
        asyncio.create_task(
            _loop("updates", "HELM_UPDATE_CHECK_CRON", "0 4 * * *", _update_check)
        ),
        asyncio.create_task(
            _loop("backup", "HELM_BACKUP_CRON", "30 3 * * *", _backup)
        ),
        asyncio.create_task(_seerr_wire_loop()),
        asyncio.create_task(_dispatcharr_wire_loop()),
        asyncio.create_task(metrics.collector_loop()),
    ]


async def stop(app) -> None:
    for task in getattr(app.state, "jobs", []):
        task.cancel()
    for task in getattr(app.state, "jobs", []):
        with contextlib.suppress(asyncio.CancelledError):
            await task


def status() -> dict:
    return _load()
