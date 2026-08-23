"""Helm: the Kine admin GUI.

Design note. Helm can create containers, which makes it root-equivalent
on the host. The socket proxy narrows what a bug in this code can reach;
it does not make a compromise of Helm survivable. That is why it sits
behind forward-auth, binds to the LAN by default, and is documented as
something you do not expose to the internet.
"""
import asyncio
import os
import pathlib

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, catalogue, channels, compose, config, launch, nfs_exports, scheduler
from .gluetun import connection_label as _connection_label
from .gluetun import parse_forwarded_port as _parse_forwarded_port
from .gluetun import parse_public_ip as _parse_public_ip
from .wireguard import empty_vpn_env as _empty_vpn_env
from .wireguard import parse_conf as _parse_wireguard_conf
from .wireguard import remove_gluetun_conf as _remove_gluetun_conf
from .wireguard import write_gluetun_conf as _write_gluetun_conf

_REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))
FRONTEND = _REPO / "helm" / "frontend"
if not (FRONTEND / "index.html").is_file():
    FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
app = FastAPI(title="Kine Helm", docs_url=None, redoc_url=None)

COOKIE = "kine_session"


async def _refresh_mdns() -> None:
    """Recreate mdns so it re-reads COMPOSE_PROFILES and advertises new names."""
    if "mdns" not in config.profiles():
        return
    await compose.run("up", "-d", "--force-recreate", "mdns", timeout=60)


@app.on_event("startup")
async def _startup() -> None:
    config.normalize()
    scheduler.start(app)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop(app)


# ── auth ────────────────────────────────────────────────────────
def require_user(request: Request) -> str:
    user = auth.verify(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


@app.get("/api/health")
async def health():
    """Unauthenticated on purpose: the container healthcheck calls it.

    It deliberately leaks nothing beyond whether setup has happened,
    which the login page needs in order to decide what to render.
    """
    return {"ok": True, "configured": auth.is_configured()}


@app.get("/api/status")
async def status(user: str = Depends(require_user)):
    env = config.read()
    disks = {}
    for label, key in (("stack", "STACK_ROOT"), ("data", "DATA_ROOT")):
        mount = "/stack" if key == "STACK_ROOT" else "/data"
        try:
            st = os.statvfs(mount)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            disks[label] = {
                "path": env.get(key, ""),
                "total_gb": round(total / 1e9, 1),
                "free_gb": round(free / 1e9, 1),
                "used_pct": round(100 * (1 - free / total), 1) if total else None,
            }
        except OSError:
            disks[label] = {"path": env.get(key, ""), "error": "not mounted"}

    code, ps = await compose.run("ps", "--format", "json", timeout=60)
    return {
        "disks": disks,
        "jobs": scheduler.status(),
        "compose_ok": code == 0,
        "raw_ps": ps if code == 0 else "",
    }


@app.get("/api/auth/verify")
async def auth_verify(request: Request):
    """Called by Traefik's forwardAuth for every app request."""
    user = auth.verify(request.cookies.get(COOKIE))
    if not user:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Kine-User": user})


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    if not auth.check(body.get("username", ""), body.get("password", "")):
        # Deliberately slow and vague: no distinction between a bad
        # username and a bad password.
        await asyncio.sleep(1.0)
        raise HTTPException(401, "invalid credentials")
    token = auth.issue(body["username"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=auth.MAX_AGE)
    return resp


@app.post("/api/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.post("/api/setup")
async def first_run(request: Request):
    """Onboarding. Only works while no admin password is set."""
    if auth.is_configured():
        raise HTTPException(409, "already configured")
    body = await request.json()
    pw = body.get("password", "")
    if len(pw) < 12:
        raise HTTPException(400, "password must be at least 12 characters")

    vpn_enabled = bool(body.get("vpn_enabled", False))
    vpn_fields = {}
    if vpn_enabled:
        try:
            vpn_fields = _parse_wireguard_conf(body.get("wireguard_conf", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if "WIREGUARD_PRIVATE_KEY" not in vpn_fields:
            raise HTTPException(
                400,
                "VPN is enabled: paste a WireGuard config that includes "
                "[Interface] PrivateKey and Address, and [Peer] PublicKey "
                "and Endpoint",
            )

    updates = {
        k: str(body[v])
        for k, v in (
            ("KINE_DOMAIN", "domain"),
            ("KINE_TLS_MODE", "tls_mode"),
            ("KINE_ACME_EMAIL", "acme_email"),
            ("KINE_ACME_DNS_PROVIDER", "acme_provider"),
            ("KINE_TIMEZONE", "timezone"),
        )
        if v in body
    }
    if updates:
        env_before = config.read()
        changed = any(
            str(updates.get(k, "")) != env_before.get(k, "") for k in updates
        )
        config.write(updates)
        if changed:
            await compose.script("tls-setup.sh", timeout=60)

    cat = catalogue.load()
    tunnelled = [k for k, v in cat.items() if "gluetun" in v.get("requires", [])]
    wanted = config.profiles()
    if vpn_enabled:
        if "gluetun" not in wanted:
            wanted.append("gluetun")
        wireguard_text = body.get("wireguard_conf", "")
        env = config.read()
        stack_root = env.get("STACK_ROOT") or "/srv/kine"
        await asyncio.to_thread(_write_gluetun_conf, wireguard_text, stack_root)
        config.write({"VPN_ENABLED": "true", **vpn_fields})
    else:
        # Nothing tunnelled-forced can run without it, so switching VPN
        # off at setup drops them too rather than leaving them enabled
        # and silently broken.
        wanted = [p for p in wanted if p != "gluetun" and p not in tunnelled]
        env = config.read()
        stack_root = env.get("STACK_ROOT") or "/srv/kine"
        await asyncio.to_thread(_remove_gluetun_conf, stack_root)
        config.write({"VPN_ENABLED": "false", **_empty_vpn_env()})
    config.set_profiles(wanted)
    await _refresh_mdns()

    if vpn_enabled:
        code, out = await compose.run(
            "up", "-d", "--force-recreate", "gluetun", timeout=90
        )
        if code != 0:
            _, logs = await compose.run("logs", "--tail", "40", "gluetun")
            detail = (logs or out or "").strip().splitlines()
            tail = " | ".join(detail[-6:]) if detail else "no gluetun logs"
            raise HTTPException(
                500,
                f"VPN could not start ({tail}). Check: docker logs kine-gluetun",
            )

    # Only lock onboarding after every requested prerequisite succeeded,
    # so a bad tunnel configuration can be corrected and submitted again.
    auth.set_password(pw)
    return {"ok": True}


# ── catalogue and lifecycle ─────────────────────────────────────
def _tier_section_enabled(tier: str, enabled: set[str]) -> bool:
    """Section is on when any visible app in the tier is enabled."""
    visible = catalogue.tier_visible_apps(tier)
    return any(app in enabled for app in visible)


@app.get("/api/apps")
async def apps(request: Request, user: str = Depends(require_user)):
    cat = catalogue.load()
    enabled = set(config.profiles())
    env = config.read()
    code, out = await compose.run("ps", "--format", "json")
    running = out if code == 0 else ""
    dev_on = set(channels.channels())
    result = []
    for key, meta in cat.items():
        result.append({
            "id": key,
            "name": meta.get("name", key),
            "tier": meta.get("tier", "other"),
            "summary": meta.get("summary", ""),
            "enabled": key in enabled,
            "default": meta.get("default", False),
            "running": f'"{key}"' in running or f"kine-{key}" in running,
            "url": launch.app_url(
                meta.get("subdomain"),
                env.get("KINE_DOMAIN", ""),
                request.url.hostname,
                env.get("KINE_LOCAL_DOMAIN", "127.0.0.1.nip.io"),
                env.get("TRAEFIK_HTTPS_PORT", "8443"),
            ),
            "releases": meta.get("releases"),
            "requires": meta.get("requires", []),
            "tunnelled": meta.get("tunnelled"),
            "hidden": meta.get("hidden", False),
            "dev_supported": channels.supported(meta),
            "dev_enabled": key in dev_on,
            "dev_tag": meta.get("dev_tag"),
        })
    tiers = {}
    for tier in catalogue.TIER_LABELS:
        visible = catalogue.tier_visible_apps(tier)
        if visible:
            tiers[tier] = {
                "label": catalogue.TIER_LABELS[tier],
                "enabled": _tier_section_enabled(tier, enabled),
                "defaults": catalogue.tier_default_apps(tier),
            }
    return {"apps": result, "tiers": tiers}


@app.post("/api/tiers/{tier}/enable")
async def enable_tier(tier: str, user: str = Depends(require_user)):
    defaults = catalogue.tier_default_apps(tier)
    if not defaults:
        raise HTTPException(404, "no default apps in this section")
    label = catalogue.TIER_LABELS.get(tier, tier.title())
    cat = catalogue.load()
    wanted = config.profiles()
    for app_id in defaults:
        wanted = catalogue.resolve_deps(app_id, cat, wanted)
        if app_id not in wanted:
            wanted.append(app_id)
    config.set_profiles(wanted)
    await _refresh_mdns()
    # Seed config.xml with derived API keys before first start so wire
    # can authenticate. Safe no-op when configs already exist.
    await compose.run("run", "--rm", "provision", "seed")
    # Do not run an unscoped `up` from inside Helm: if Compose decides
    # Helm itself needs recreation, it kills the request mid-deployment.
    code, _ = await compose.run("up", "-d", *defaults)
    if code != 0:
        raise HTTPException(500, f"Could not start {label} apps")
    await compose.run("run", "--rm", "provision", "wire")
    return {"ok": True, "enabled": defaults}


@app.post("/api/tiers/{tier}/disable")
async def disable_tier(tier: str, user: str = Depends(require_user)):
    cat = catalogue.load()
    to_remove = {
        k for k, v in cat.items()
        if v.get("tier") == tier and not v.get("mandatory") and not v.get("hidden")
    }
    wanted = config.profiles()
    for app_id in to_remove:
        if app_id not in wanted:
            continue
        dependants = [k for k, v in cat.items() if app_id in v.get("requires", [])]
        still_on = [d for d in dependants if d in wanted and d not in to_remove]
        if still_on:
            raise HTTPException(409, f"{', '.join(still_on)} depend on {app_id}")
    removed = sorted(to_remove & set(wanted))
    for app_id in to_remove:
        if app_id in wanted:
            await compose.run("stop", app_id)
            await compose.run("rm", "-f", app_id)
    wanted = catalogue.prune_orphan_gluetun(
        [p for p in wanted if p not in to_remove], cat,
    )
    config.set_profiles(wanted)
    await _refresh_mdns()
    return {"ok": True, "disabled": removed}


@app.post("/api/apps/{app_id}/enable")
async def enable(app_id: str, user: str = Depends(require_user)):
    cat = catalogue.load()
    if app_id not in cat:
        raise HTTPException(404, "unknown app")

    wanted = config.profiles()
    # Pull in anything this app cannot work without, rather than
    # starting it into a broken state and letting the user find out.
    for dep in cat[app_id].get("requires", []):
        if dep not in wanted:
            wanted.append(dep)
    if app_id not in wanted:
        wanted.append(app_id)
    config.set_profiles(wanted)
    await _refresh_mdns()

    # Seed before start so *arr apps adopt derived keys on first run.
    await compose.run("run", "--rm", "provision", "seed")
    code, out = await compose.run("up", "-d", app_id)
    if code != 0:
        raise HTTPException(500, f"Could not start {app_id}")
    # Wire it to whatever is already running.
    await compose.run("run", "--rm", "provision", "wire")
    return {"ok": True, "log": out[-2000:]}


@app.post("/api/apps/{app_id}/disable")
async def disable(app_id: str, user: str = Depends(require_user)):
    cat = catalogue.load()
    dependants = [k for k, v in cat.items() if app_id in v.get("requires", [])]
    still_on = [d for d in dependants if d in config.profiles()]
    if still_on:
        raise HTTPException(409, f"{', '.join(still_on)} depend on {app_id}")
    await compose.run("stop", app_id)
    await compose.run("rm", "-f", app_id)
    config.set_profiles([p for p in config.profiles() if p != app_id])
    await _refresh_mdns()
    return {"ok": True}


@app.post("/api/apps/{app_id}/restart")
async def restart(app_id: str, user: str = Depends(require_user)):
    if app_id == "gluetun":
        raise HTTPException(
            409,
            "Restarting the tunnel alone would sever every app inside it. "
            "Use the VPN page's restart, which cycles the whole group.",
        )
    code, out = await compose.run("restart", app_id)
    return {"ok": code == 0, "log": out[-2000:]}


@app.post("/api/apps/{app_id}/dev/enable")
async def enable_dev(app_id: str, user: str = Depends(require_user)):
    return await _set_dev_channel(app_id, enabled=True)


@app.post("/api/apps/{app_id}/dev/disable")
async def disable_dev(app_id: str, user: str = Depends(require_user)):
    return await _set_dev_channel(app_id, enabled=False)


async def _set_dev_channel(app_id: str, *, enabled: bool) -> dict:
    cat = catalogue.load()
    if app_id not in cat:
        raise HTTPException(404, "unknown app")
    meta = cat[app_id]
    if not channels.supported(meta):
        raise HTTPException(400, f"{app_id} has no development image channel")
    try:
        updates = channels.apply(app_id, meta, enabled=enabled)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Only recreate when the profile is already selected; otherwise the
    # next enable/up picks up the new tag.
    if app_id in config.profiles():
        await compose.run("pull", app_id)
        code, out = await compose.run("up", "-d", "--force-recreate", app_id)
        if code != 0:
            raise HTTPException(500, f"Could not recreate {app_id} on the new channel")
        return {"ok": True, "dev_enabled": enabled, "tag": updates.get(channels.tag_key(app_id)),
                "log": out[-2000:]}
    return {"ok": True, "dev_enabled": enabled, "tag": updates.get(channels.tag_key(app_id))}


@app.websocket("/api/apps/{app_id}/logs")
async def logs(ws: WebSocket, app_id: str):
    await ws.accept()
    if not auth.verify(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return
    try:
        async for line in compose.stream_logs(app_id):
            await ws.send_text(line)
    except Exception:
        await ws.close()


# ── updates ─────────────────────────────────────────────────────
@app.get("/api/updates")
async def updates(refresh: bool = False, user: str = Depends(require_user)):
    """Cached by default; the scheduled check refreshes it overnight.

    A digest check hits every image's registry, so doing it on every
    page load is both slow and rude to the registries.
    """
    cached = scheduler.status().get("updates")
    if cached and not refresh:
        return {"ok": cached["ok"], "report": cached["report"],
                "pending": cached["pending"], "checked": cached["checked"],
                "cached": True}
    code, out = await compose.script("updates.sh", "check", timeout=300)
    return {"ok": code == 0, "report": out, "cached": False}


@app.post("/api/updates/{app_id}")
async def apply_update(app_id: str, user: str = Depends(require_user)):
    """Snapshot, pull, recreate, and roll back if it does not come back.

    Nothing here runs on a schedule. Automatic updates are how a working
    media stack breaks overnight, usually mid-import.
    """
    code, out = await compose.script("updates.sh", "apply", app_id, timeout=1200)
    return {"ok": code == 0, "rolled_back": code != 0, "log": out}


# ── VPN ─────────────────────────────────────────────────────────
@app.get("/api/vpn")
async def vpn_status(user: str = Depends(require_user)):
    env = config.read()
    code, out = await compose.run("exec", "-T", "gluetun",
                                  "wget", "-qO-",
                                  "http://127.0.0.1:8000/v1/openvpn/portforwarded",
                                  timeout=30)
    ip_code, ip_out = await compose.run("exec", "-T", "gluetun",
                                        "wget", "-qO-",
                                        "http://127.0.0.1:8000/v1/publicip/ip",
                                        timeout=30)
    return {
        "enabled": env.get("VPN_ENABLED") == "true",
        "provider": env.get("VPN_SERVICE_PROVIDER"),
        "connection_type": _connection_label(env.get("VPN_TYPE", "")),
        "countries": env.get("VPN_SERVER_COUNTRIES"),
        "tunnelled": env.get("VPN_TUNNELLED_APPS", "").split(","),
        "forwarded_port": _parse_forwarded_port(out) if code == 0 else None,
        "public_ip": _parse_public_ip(ip_out) if ip_code == 0 else None,
    }


@app.post("/api/vpn/settings")
async def vpn_settings(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    mapping = {
        "provider": "VPN_SERVICE_PROVIDER",
        "private_key": "WIREGUARD_PRIVATE_KEY",
        "addresses": "WIREGUARD_ADDRESSES",
        "countries": "VPN_SERVER_COUNTRIES",
        "port_forwarding": "VPN_PORT_FORWARDING",
        "tunnelled": "VPN_TUNNELLED_APPS",
    }
    config.write({env: str(body[k]) for k, env in mapping.items() if k in body})
    return {"ok": True, "note": "restart the tunnel group to apply"}


@app.post("/api/vpn/restart")
async def vpn_restart(user: str = Depends(require_user)):
    env = config.read()
    group = ["gluetun", "vpn-portsync"] + [
        a for a in env.get("VPN_TUNNELLED_APPS", "").split(",") if a
    ]
    code, out = await compose.run("restart", *group, timeout=300)
    return {"ok": code == 0, "restarted": group, "log": out[-2000:]}


@app.post("/api/vpn/leaktest")
async def vpn_leaktest(user: str = Depends(require_user)):
    """The difference between believing the tunnel works and knowing."""
    code, out = await compose.script("vpn-leaktest.sh", timeout=120)
    return {"result": {0: "ok", 1: "leaking"}.get(code, "inconclusive"), "detail": out}


# ── settings, backup, provisioning ──────────────────────────────
_NFS_KEYS = ("NFS_SERVER", "NFS_TV", "NFS_MOVIES", "NFS_DOWNLOADS", "NFS_CACHE")


@app.get("/api/nfs/exports")
async def nfs_list_exports(server: str = "", user: str = Depends(require_user)):
    """Browse exports advertised by an NFS server (showmount -e).

    Does not mount anything. Prefer ``/api/nfs/browse`` for subfolders.
    """
    try:
        exports = await asyncio.to_thread(nfs_exports.list_exports, server)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"server": nfs_exports.validate_server(server), "exports": exports}


@app.get("/api/nfs/browse")
async def nfs_browse(server: str = "", path: str = "", user: str = Depends(require_user)):
    """Browse NFS exports and subfolders for the Settings path picker."""
    try:
        return await asyncio.to_thread(nfs_exports.browse, server, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/settings")
async def get_settings(user: str = Depends(require_user)):
    env = config.read()
    public = ("KINE_DOMAIN", "KINE_TLS_MODE", "KINE_ACME_EMAIL", "KINE_ACME_DNS_PROVIDER",
              "KINE_TIMEZONE", "STACK_ROOT", "DATA_ROOT", "HELM_UPDATE_CHECK_CRON",
              *_NFS_KEYS)
    return {k: env.get(k, "") for k in public}


@app.post("/api/settings")
async def set_settings(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    allowed = {"KINE_DOMAIN", "KINE_TLS_MODE", "KINE_ACME_EMAIL",
               "KINE_ACME_DNS_PROVIDER", "KINE_TIMEZONE", "HELM_UPDATE_CHECK_CRON",
               *_NFS_KEYS}
    config.write({k: str(v) for k, v in body.items() if k in allowed})
    if {"KINE_TLS_MODE", "KINE_DOMAIN", "KINE_ACME_EMAIL"} & set(body):
        await compose.script("tls-setup.sh")
        await compose.run("restart", "traefik")
    if "KINE_DOMAIN" in body:
        await _refresh_mdns()
    nfs_changed = bool(set(_NFS_KEYS) & set(body))
    return {"ok": True, "nfs_requires_host_mount": nfs_changed}


@app.post("/api/backup")
async def backup(user: str = Depends(require_user)):
    code, out = await compose.script("backup.sh", timeout=1800)
    return {"ok": code == 0, "path": out.strip().splitlines()[-1] if code == 0 else None}


@app.post("/api/provision")
async def provision(user: str = Depends(require_user)):
    code, out = await compose.run("run", "--rm", "provision", "wire", timeout=900)
    return {"ok": code == 0, "log": out}


# ── frontend ────────────────────────────────────────────────────
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/{path:path}")
async def spa(path: str):
    return FileResponse(
        FRONTEND / "index.html",
        headers={"Cache-Control": "no-cache"},
    )
