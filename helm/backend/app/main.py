"""Helm: the Media Centre admin GUI.

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

from . import auth, catalogue, compose, config

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
app = FastAPI(title="Media Centre Helm", docs_url=None, redoc_url=None)

COOKIE = "mc_session"


# ── auth ────────────────────────────────────────────────────────
def require_user(request: Request) -> str:
    user = auth.verify(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


@app.get("/api/health")
async def health():
    return {"ok": True, "configured": auth.is_configured()}


@app.get("/api/auth/verify")
async def auth_verify(request: Request):
    """Called by Traefik's forwardAuth for every app request."""
    user = auth.verify(request.cookies.get(COOKIE))
    if not user:
        return Response(status_code=401)
    return Response(status_code=200, headers={"X-Mc-User": user})


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
    auth.set_password(pw)
    updates = {
        k: str(body[v])
        for k, v in (
            ("MC_DOMAIN", "domain"),
            ("MC_TLS_MODE", "tls_mode"),
            ("MC_ACME_EMAIL", "acme_email"),
            ("MC_ACME_DNS_PROVIDER", "acme_provider"),
            ("MC_TIMEZONE", "timezone"),
        )
        if v in body
    }
    if updates:
        config.write(updates)
        await compose.script("tls-setup.sh")
    return {"ok": True}


# ── catalogue and lifecycle ─────────────────────────────────────
@app.get("/api/apps")
async def apps(user: str = Depends(require_user)):
    cat = catalogue.load()
    enabled = set(config.profiles())
    env = config.read()
    code, out = await compose.run("ps", "--format", "json")
    running = out if code == 0 else ""
    result = []
    for key, meta in cat.items():
        result.append({
            "id": key,
            "name": meta.get("name", key),
            "tier": meta.get("tier", "other"),
            "summary": meta.get("summary", ""),
            "enabled": key in enabled,
            "running": f'"{key}"' in running or f"mc-{key}" in running,
            "url": f"https://{meta['subdomain']}.{env.get('MC_DOMAIN','')}"
                   if meta.get("subdomain") else None,
            "releases": meta.get("releases"),
            "requires": meta.get("requires", []),
            "tunnelled": meta.get("tunnelled"),
            "hidden": meta.get("hidden", False),
        })
    return result


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

    code, out = await compose.run("up", "-d")
    if code != 0:
        raise HTTPException(500, out[-2000:])
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
async def updates(user: str = Depends(require_user)):
    code, out = await compose.script("updates.sh", "check", timeout=300)
    return {"ok": code == 0, "report": out}


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
    return {
        "enabled": env.get("VPN_ENABLED") == "true",
        "provider": env.get("VPN_SERVICE_PROVIDER"),
        "countries": env.get("VPN_SERVER_COUNTRIES"),
        "tunnelled": env.get("VPN_TUNNELLED_APPS", "").split(","),
        "forwarded_port": out.strip() if code == 0 else None,
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
@app.get("/api/settings")
async def get_settings(user: str = Depends(require_user)):
    env = config.read()
    public = ("MC_DOMAIN", "MC_TLS_MODE", "MC_ACME_EMAIL", "MC_ACME_DNS_PROVIDER",
              "MC_TIMEZONE", "STACK_ROOT", "DATA_ROOT", "HELM_UPDATE_CHECK_CRON")
    return {k: env.get(k, "") for k in public}


@app.post("/api/settings")
async def set_settings(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    allowed = {"MC_DOMAIN", "MC_TLS_MODE", "MC_ACME_EMAIL",
               "MC_ACME_DNS_PROVIDER", "MC_TIMEZONE", "HELM_UPDATE_CHECK_CRON"}
    config.write({k: str(v) for k, v in body.items() if k in allowed})
    if {"MC_TLS_MODE", "MC_DOMAIN", "MC_ACME_EMAIL"} & set(body):
        await compose.script("tls-setup.sh")
        await compose.run("restart", "traefik")
    return {"ok": True}


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
    return FileResponse(FRONTEND / "index.html")
