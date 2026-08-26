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
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import acme_env, appkeys, auth, backups, catalogue, channels, compose, config, dispatcharr_sources, dispatcharr_token, downloads, ecm_setup, embed_proxy, launch, library_rescan, media_servers, metrics, nfs_exports, nzbget_news, profile_reconcile, prowlarr_newznab, promquery, provision_lock, scheduler, teamarr_setup, tunnel_heal, tunnel_hosts, updates_info, vpn_profiles, vpn_routing, watching
import sys
from .gluetun import connection_label as _connection_label
from .gluetun import parse_forwarded_port as _parse_forwarded_port
from .gluetun import parse_public_ip as _parse_public_ip
from .wireguard import empty_vpn_env as _empty_vpn_env
from .wireguard import parse_conf as _parse_wireguard_conf
from .wireguard import remove_gluetun_conf as _remove_gluetun_conf
from .wireguard import write_gluetun_conf as _write_gluetun_conf
from .wireguard import write_secondary_conf as _write_secondary_conf

_REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))
FRONTEND = _REPO / "helm" / "frontend"
if not (FRONTEND / "index.html").is_file():
    FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
app = FastAPI(title="Kine Helm", docs_url=None, redoc_url=None)


async def _queue_library_sync(changed_keys: set[str] | None = None) -> dict:
    """Import and rescan in the background; large libraries can take minutes."""

    async def _run() -> None:
        try:
            await asyncio.to_thread(library_rescan.after_nfs_mount, changed_keys)
        except Exception as exc:
            print(f"library sync failed: {exc}", flush=True)

    asyncio.create_task(_run())
    return {"ok": True, "queued": True, "results": []}

COOKIE = "kine_session"


async def _provision(*args: str, reason: str = "wire", timeout: int = 900) -> tuple[int, str]:
    try:
        async with provision_lock.acquire(reason=reason):
            return await compose.run("run", "--rm", "provision", *args, timeout=timeout)
    except provision_lock.ProvisionBusy as exc:
        raise HTTPException(409, exc.detail) from exc


async def _sync_recyclarr() -> None:
    """Push TRaSH Guide profiles into Sonarr/Radarr after wire or tier changes."""
    if "recyclarr" not in config.profiles():
        return
    code, out = await compose.run(
        "exec", "-T", "recyclarr", "recyclarr", "sync", timeout=300,
    )
    if code != 0:
        print(f"recyclarr sync failed: {out[-500:]}", flush=True)


async def _refresh_mdns() -> None:
    """Recreate mdns so it re-reads COMPOSE_PROFILES and advertises new names."""
    if "mdns" not in config.profiles():
        return
    await compose.run("up", "-d", "--force-recreate", "mdns", timeout=60)


_MEDIA_VOLUME_APPS = (
    "sonarr", "radarr", "prowlarr", "transmission", "bazarr",
    "nzbget", "unpackerr", "emby", "tdarr", "dispatcharr",
)
_NFS_KEYS = ("NFS_SERVER", "NFS_MEDIA", "NFS_TV", "NFS_MOVIES", "NFS_DOWNLOADS", "NFS_CACHE")
_NFS_EXPORT_KEYS = _NFS_KEYS[1:]
_MEDIA_SERVER_KEYS = (
    "PLEX_HOST", "PLEX_PORT", "PLEX_TOKEN", "PLEX_USE_SSL",
    "PLEX_TV_MAP_FROM", "PLEX_TV_MAP_TO",
    "PLEX_MOVIES_MAP_FROM", "PLEX_MOVIES_MAP_TO",
    "EMBY_HOST", "EMBY_PORT", "EMBY_API_KEY", "EMBY_USE_SSL",
    "EMBY_TV_MAP_FROM", "EMBY_TV_MAP_TO",
    "EMBY_MOVIES_MAP_FROM", "EMBY_MOVIES_MAP_TO",
)
_LIVE_TV_KEYS = ("DISPATCHARR_TOKEN",)
_SUBTITLE_KEYS = ("OPENSUBTITLES_USERNAME", "OPENSUBTITLES_PASSWORD")
NZBGET_NEWS_KEY = "NZBGET_NEWS_SERVERS"
PROWLARR_NEWZNAB_KEY = "PROWLARR_NEWZNAB_INDEXERS"


def _store_nzbget_servers(servers) -> list[dict]:
    recipe = nzbget_news.recipe()
    incoming = servers if isinstance(servers, list) else []
    # Blank password in the form must not wipe an existing credential for
    # the same host (browser password managers often leave the field empty).
    existing = {
        str(s.get("host") or "").strip().lower(): s
        for s in recipe.parse_servers(config.read().get(NZBGET_NEWS_KEY, ""))
        if s.get("host")
    }
    merged = []
    for row in incoming:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        host = str(item.get("host") or "").strip().lower()
        prev = existing.get(host) or {}
        if not str(item.get("password") or "") and prev.get("password"):
            item["password"] = prev["password"]
        if not str(item.get("username") or "") and prev.get("username"):
            item["username"] = prev["username"]
        merged.append(item)
    parsed = recipe.parse_servers(
        recipe.serialize_servers(merged)
    )
    config.write({NZBGET_NEWS_KEY: recipe.serialize_servers(parsed)})
    return parsed


def _store_prowlarr_newznab(indexers) -> list[dict]:
    recipe = prowlarr_newznab.recipe()
    incoming = indexers if isinstance(indexers, list) else []
    existing = recipe.parse_indexers(config.read().get(PROWLARR_NEWZNAB_KEY, ""))
    merged = recipe.merge_indexers(incoming, existing)
    parsed = recipe.parse_indexers(recipe.serialize_indexers(merged))
    config.write({PROWLARR_NEWZNAB_KEY: recipe.serialize_indexers(parsed)})
    return parsed


def _apply_prowlarr_newznab(rows: list[dict] | None = None) -> dict:
    """Upsert Newznab indexers in Prowlarr from Settings / .env."""
    recipe = prowlarr_newznab.recipe()
    rows = rows if rows is not None else recipe.parse_indexers(
        config.read().get(PROWLARR_NEWZNAB_KEY, "")
    )
    if "prowlarr" not in config.profiles():
        return {"ok": True, "indexers": len(rows), "deferred": True}
    if not rows:
        return {"ok": True, "indexers": 0}
    key = appkeys.arr_key("prowlarr")
    if not key:
        return {"ok": False, "indexers": len(rows), "error": "prowlarr API key missing"}
    provision = str(_REPO / "provision")
    if provision not in sys.path:
        sys.path.insert(0, provision)
    from arrclient import ArrClient, http_error_detail

    client = ArrClient(tunnel_hosts.internal_base_for_app("prowlarr", 9696), key, api="v1", timeout=120.0)
    if not client.wait(timeout=30):
        return {"ok": False, "indexers": len(rows), "error": "prowlarr unreachable"}
    logs: list[str] = []
    try:
        actions = recipe.ensure_newznab_indexers(client, rows, logs.append)
    except Exception as exc:  # noqa: BLE001
        detail = http_error_detail(exc) if hasattr(exc, "response") else str(exc)
        return {"ok": False, "indexers": len(rows), "error": detail, "log": logs}
    return {"ok": True, "indexers": len(rows), "actions": actions, "log": logs}


def _apply_nzbget_conf(servers: list[dict] | None = None) -> None:
    """Patch nzbget.conf on disk when the file already exists."""
    recipe = nzbget_news.recipe()
    stack = pathlib.Path(os.environ.get("KINE_ROOT", "/stack"))
    conf = stack / "config" / "nzbget" / "nzbget.conf"
    scripts = stack / "config" / "nzbget" / "scripts"
    recipe.install_extensions(scripts, log=lambda *_: None)
    if conf.is_file():
        if servers is not None:
            recipe.apply_servers(conf, servers)
        recipe.apply_runtime_defaults(conf)
        recipe.apply_categories(conf)
        recipe.apply_extensions(conf)



def _nfs_configured(env: dict | None = None) -> bool:
    env = env or config.read()
    if not env.get("NFS_SERVER", "").strip():
        return False
    return any(env.get(k, "").strip() for k in _NFS_EXPORT_KEYS)


async def _ensure_nfs_mounted() -> dict | None:
    """Mount NFS on the host when configured. Returns None if NFS is not set up."""
    if not _nfs_configured():
        return None
    await _stop_media_volume_apps()
    return await asyncio.to_thread(nfs_exports.apply_mounts_via_agent)


def _nfs_mount_error(mount: dict | None) -> str | None:
    if mount is None or mount.get("ok"):
        return None
    return (mount.get("log") or "NFS mount failed")[-500:]


async def _stop_media_volume_apps() -> None:
    """Stop apps that bind DATA_ROOT so host NFS mounts are not busy."""
    profiles = set(config.profiles())
    wanted = [a for a in _MEDIA_VOLUME_APPS if a in profiles]
    if wanted:
        await compose.run("stop", *wanted, timeout=120)


def _tunnelled_profiles(profiles: set[str] | None = None) -> list[str]:
    """Enabled apps that share gluetun's network namespace."""
    env = config.read()
    active = profiles if profiles is not None else set(config.profiles())
    return [
        a.strip()
        for a in env.get("VPN_TUNNELLED_APPS", "").split(",")
        if a.strip() and a.strip() in active
    ]


async def _start_app(app_id: str, profiles: list[str]) -> tuple[int, str]:
    """Start an app; tunnelled apps recreate the whole gluetun group.

    ``compose up -d <tunnelled>`` can restart gluetun (depends_on health),
    which orphans every other ``network_mode: service:gluetun`` container.
    Bring the whole group up together so they share the new namespace.
    """
    tunnelled = _tunnelled_profiles(set(profiles))
    if app_id == "gluetun" or app_id in tunnelled:
        group = ["gluetun", *tunnelled]
        # Preserve order, drop duplicates (gluetun first).
        seen: set[str] = set()
        ordered = [s for s in group if not (s in seen or seen.add(s))]
        return await compose.run(
            "up", "-d", "--force-recreate", *ordered, timeout=300,
        )
    return await compose.run("up", "-d", app_id)


async def _recreate_media_volume_apps() -> None:
    """Recreate apps that bind DATA_ROOT so they see host NFS mounts.

    Docker captures the bind source at container create time. Mounting NFS on
    the host afterwards leaves running containers on the old local directories.
    """
    profiles = set(config.profiles())
    wanted = [a for a in _MEDIA_VOLUME_APPS if a in profiles]
    if not wanted:
        return
    env = config.read()
    tunnelled = {
        a.strip()
        for a in env.get("VPN_TUNNELLED_APPS", "").split(",")
        if a.strip()
    } & profiles
    media_tunnelled = [a for a in wanted if a in tunnelled]
    standalone = [a for a in wanted if a not in tunnelled]
    if media_tunnelled and "gluetun" in profiles:
        all_tunnelled = [
            a.strip()
            for a in env.get("VPN_TUNNELLED_APPS", "").split(",")
            if a.strip() and a.strip() in profiles
        ]
        await compose.run(
            "up", "-d", "--force-recreate", "gluetun", *all_tunnelled, timeout=300,
        )
    if standalone:
        await compose.run("up", "-d", "--force-recreate", *standalone, timeout=300)


async def _apply_domain_routing() -> None:
    """Recreate services whose Traefik Host() rules embed KINE_DOMAIN.

    Docker bakes compose labels at container creation; restarting Traefik
    alone leaves gluetun (and others) still advertising the old domain.
    """
    profiles = set(config.profiles())
    env = config.read()
    routed = ["traefik", "helm"]
    if "gluetun" in profiles:
        routed.append("gluetun")
    for app in ("emby", "seerr", "tdarr", "dispatcharr", "ecm", "teamarr"):
        if app in profiles:
            routed.append(app)
    await compose.run("up", "-d", "--force-recreate", *routed, timeout=300)
    if "gluetun" not in profiles:
        return
    tunnelled = [a for a in env.get("VPN_TUNNELLED_APPS", "").split(",") if a in profiles]
    if tunnelled:
        await compose.run(
            "up", "-d", "--force-recreate", "gluetun", *tunnelled, timeout=300
        )


async def reconcile_disabled_running() -> list[str]:
    """Stop catalogue containers left up after their profile was removed.

    Compose starts a *named* service even when its profile is off, so tunnel
    group recreates and Update All can resurrect disabled apps. Sweep them.
    """
    cat = catalogue.load()
    enabled = set(config.profiles())
    code, out = await compose.run("ps", "--format", "json")
    running = profile_reconcile.running_service_names(out if code == 0 else "")
    stray = profile_reconcile.disabled_running_services(
        catalogue_ids=cat.keys(),
        enabled=enabled,
        running=running,
    )
    for app_id in stray:
        await compose.run("stop", app_id)
        await compose.run("rm", "-f", app_id)
    return stray


@app.on_event("startup")
async def _startup() -> None:
    config.normalize()
    scheduler.start(app)
    # Fire-and-forget: do not delay healthchecks on a slow compose ps.
    asyncio.create_task(reconcile_disabled_running())


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop(app)


# ── auth ────────────────────────────────────────────────────────
def require_user(request: Request) -> str:
    user = auth.verify(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "not authenticated")
    return user


# Same-origin app embeds (/view/sonarr/ …) — after require_user exists.
embed_proxy.mount(app, require_user)


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

    def _disk(display_path: str, candidates: list[str]) -> dict:
        for mount in candidates:
            if not mount:
                continue
            try:
                st = os.statvfs(mount)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            return {
                "path": display_path or mount,
                "total_gb": round(total / 1e9, 1),
                "free_gb": round(free / 1e9, 1),
                "used_pct": round(100 * (1 - free / total), 1) if total else None,
            }
        return {"path": display_path, "error": "not mounted"}

    stack_root = (env.get("STACK_ROOT") or "").rstrip("/")
    data_root = (env.get("DATA_ROOT") or "").rstrip("/")
    media_mount = promquery.nfs_media_mountpoint(env)
    disks["stack"] = _disk(stack_root, ["/stack", stack_root])
    # Prefer the NFS media share (downloads usually live under it).
    disks["data"] = _disk(
        data_root or media_mount,
        [media_mount, f"{data_root}/media" if data_root else "", data_root, "/data/media", "/data"],
    )

    code, ps = await compose.run("ps", "--format", "json", timeout=60)
    return {
        "disks": disks,
        "jobs": scheduler.status(),
        "provision": provision_lock.status(),
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
    username = body.get("username", "")
    password = body.get("password", "")
    if not auth.check(username, password):
        # Deliberately slow and vague: no distinction between a bad
        # username and a bad password.
        await asyncio.sleep(1.0)
        raise HTTPException(401, "invalid credentials")
    # ECM only stores a hash of its own admin; create it the first time we
    # see the live Helm password (first-run or a later login after enabling).
    ecm = await asyncio.to_thread(ecm_setup.ensure_admin, username, password)
    token = auth.issue(username)
    resp = JSONResponse({"ok": True, "ecm_setup": ecm})
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
    admin_user = config.read().get("HELM_ADMIN_USER", auth.DEFAULT_ADMIN_USER)
    ecm = await asyncio.to_thread(ecm_setup.ensure_admin, admin_user, pw)
    return {"ok": True, "ecm_setup": ecm}


# ── catalogue and lifecycle ─────────────────────────────────────
def _tier_section_enabled(tier: str, enabled: set[str]) -> bool:
    """Section is on when any visible app in the tier is enabled."""
    visible = catalogue.tier_visible_apps(tier)
    return any(app in enabled for app in visible)


@app.get("/api/apps")
async def apps(request: Request, user: str = Depends(require_user)):
    # Self-heal before reporting status so Disabled never shows a green dot.
    await reconcile_disabled_running()
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
            "embed": embed_proxy.embeddable(meta),
            "embed_url": (
                embed_proxy.embed_prefix(key) + "/"
                if embed_proxy.embeddable(meta) and key in enabled
                else None
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


@app.get("/api/watching")
async def watching_now(user: str = Depends(require_user)):
    """Active Plex and Emby sessions using Settings credentials."""
    return await watching.snapshot()


@app.get("/api/watching/art/{server}")
async def watching_art(
    server: str,
    path: str | None = None,
    item_id: str | None = None,
    tag: str | None = None,
    image: str | None = None,
    user: str = Depends(require_user),
):
    """Proxy poster/logo bytes from Plex or Emby for Watching cards."""
    try:
        body, content_type = await watching.fetch_art(
            server, path=path, item_id=item_id, tag=tag, image=image
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/api/media-servers")
async def media_servers_now(user: str = Depends(require_user)):
    """Reachability of Settings-configured Plex and Emby for the Media card."""
    return await media_servers.snapshot()


@app.get("/api/downloads")
async def downloads_now(user: str = Depends(require_user)):
    """Transmission and NZBGet queue summary for the Apps overview."""
    return await downloads.snapshot()


@app.post("/api/tiers/{tier}/enable")
async def enable_tier(tier: str, request: Request, user: str = Depends(require_user)):
    defaults = catalogue.tier_default_apps(tier)
    if not defaults:
        raise HTTPException(404, "no default apps in this section")
    label = catalogue.TIER_LABELS.get(tier, tier.title())
    body = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001
        body = {}

    teamarr_leagues = None
    if "teamarr" in defaults and "leagues" in body:
        recipe = teamarr_setup.recipe()
        try:
            teamarr_leagues = recipe.assign_channel_starts(body.get("leagues") or [])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        stack = pathlib.Path(config.read().get("STACK_ROOT") or "/srv/kine")
        recipe.STACK = stack
        recipe.save_leagues(teamarr_leagues)

    cat = catalogue.load()
    wanted = config.profiles()
    for app_id in defaults:
        wanted = catalogue.resolve_deps(app_id, cat, wanted)
        if app_id not in wanted:
            wanted.append(app_id)
    config.set_profiles(wanted)
    await _refresh_mdns()
    try:
        async with provision_lock.acquire(reason=f"enable tier {tier}"):
            await compose.run("run", "--rm", "provision", "seed")
            # NFS remount stops every DATA_ROOT app. Only do that when this
            # tier actually starts one — enabling Metrics used to stop Sonarr
            # et al. and never bring them back, then hang in wire.
            needs_media_nfs = bool(set(defaults) & set(_MEDIA_VOLUME_APPS))
            if needs_media_nfs:
                mount = await _ensure_nfs_mounted()
                if err := _nfs_mount_error(mount):
                    raise HTTPException(500, f"NFS mount failed: {err}")
            code, _ = await compose.run("up", "-d", *defaults)
            if code != 0:
                raise HTTPException(500, f"Could not start {label} apps")
            if needs_media_nfs:
                await _recreate_media_volume_apps()
            await compose.run("run", "--rm", "provision", "wire")
    except provision_lock.ProvisionBusy as exc:
        raise HTTPException(409, exc.detail) from exc
    if "teamarr" in defaults:
        leagues = teamarr_leagues
        env = config.read()
        login_user, login_password = await dispatcharr_token.ensure_login()

        def _apply() -> None:
            recipe = teamarr_setup.recipe()
            recipe.STACK = pathlib.Path(env.get("STACK_ROOT") or "/srv/kine")
            fresh = config.read()
            recipe.configure(
                leagues,
                lambda _m: None,
                dispatcharr_token=(fresh.get("DISPATCHARR_TOKEN") or "").strip(),
                dispatcharr_username=(
                    login_user
                    or (fresh.get("DISPATCHARR_LOGIN_USER") or "").strip()
                    or "kine"
                ),
                dispatcharr_password=(
                    login_password
                    or (fresh.get("DISPATCHARR_LOGIN_PASSWORD") or "").strip()
                ),
            )

        asyncio.create_task(asyncio.to_thread(_apply))
    await _sync_recyclarr()
    if _nfs_configured() and set(defaults) & {"sonarr", "radarr"}:
        await _queue_library_sync({"NFS_MEDIA", "NFS_TV", "NFS_MOVIES"})
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
    kept = [p for p in wanted if p not in to_remove]
    pruned = catalogue.prune_orphan_deps(kept, cat)
    # Stop both the visible apps and any hidden deps that became orphans
    # (e.g. Prometheus after Grafana is gone).
    stop_ids = sorted(set(wanted) - set(pruned))
    for app_id in stop_ids:
        await compose.run("stop", app_id)
        await compose.run("rm", "-f", app_id)
    config.set_profiles(pruned)
    await _refresh_mdns()
    return {"ok": True, "disabled": removed, "stopped": stop_ids}


@app.post("/api/apps/{app_id}/enable")
async def enable(app_id: str, request: Request, user: str = Depends(require_user)):
    cat = catalogue.load()
    if app_id not in cat:
        raise HTTPException(404, "unknown app")

    body = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001 — empty POST body is the usual enable click
        body = {}

    if app_id == "nzbget" and "news_servers" in body:
        _store_nzbget_servers(body.get("news_servers") or [])

    teamarr_leagues = None
    if app_id == "teamarr" and "leagues" in body:
        recipe = teamarr_setup.recipe()
        try:
            teamarr_leagues = recipe.assign_channel_starts(body.get("leagues") or [])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Point recipe STACK at the live stack root for leagues.json.
        stack = pathlib.Path(config.read().get("STACK_ROOT") or "/srv/kine")
        recipe.STACK = stack
        recipe.save_leagues(teamarr_leagues)

    wanted = config.profiles()
    # Pull in anything this app cannot work without, rather than
    # starting it into a broken state and letting the user find out.
    wanted = catalogue.resolve_deps(app_id, cat, list(wanted))
    if app_id not in wanted:
        wanted.append(app_id)
    config.set_profiles(wanted)
    await _refresh_mdns()

    try:
        async with provision_lock.acquire(reason=f"enable {app_id}"):
            await compose.run("run", "--rm", "provision", "seed")
            # Remount stops every media app; bring the whole set back after
            # start so peers (Sonarr while enabling NZBGet, etc.) are not left down.
            remounted = False
            if app_id in _MEDIA_VOLUME_APPS:
                mount = await _ensure_nfs_mounted()
                if err := _nfs_mount_error(mount):
                    raise HTTPException(500, f"NFS mount failed: {err}")
                remounted = mount is not None
            code, out = await _start_app(app_id, wanted)
            if code != 0:
                raise HTTPException(500, f"Could not start {app_id}")
            if remounted:
                await _recreate_media_volume_apps()
            await compose.run("run", "--rm", "provision", "wire")
    except provision_lock.ProvisionBusy as exc:
        raise HTTPException(409, exc.detail) from exc
    if app_id == "nzbget":
        # Conf edits need a process restart; restart alone keeps the
        # shared gluetun namespace (unlike compose up of a single peer).
        await asyncio.to_thread(_apply_nzbget_conf)
        await compose.run("restart", "nzbget")
    if app_id == "teamarr":
        # Apply leagues/numbering in the background so the Apps UI can
        # refresh as soon as the container is up (configure waits on /health).
        leagues = teamarr_leagues
        env = config.read()
        login_user, login_password = await dispatcharr_token.ensure_login()

        def _apply() -> None:
            recipe = teamarr_setup.recipe()
            recipe.STACK = pathlib.Path(env.get("STACK_ROOT") or "/srv/kine")
            fresh = config.read()
            recipe.configure(
                leagues,
                lambda _m: None,
                dispatcharr_token=(fresh.get("DISPATCHARR_TOKEN") or "").strip(),
                dispatcharr_username=(
                    login_user
                    or (fresh.get("DISPATCHARR_LOGIN_USER") or "").strip()
                    or "kine"
                ),
                dispatcharr_password=(
                    login_password
                    or (fresh.get("DISPATCHARR_LOGIN_PASSWORD") or "").strip()
                ),
            )

        asyncio.create_task(asyncio.to_thread(_apply))
    await _sync_recyclarr()
    return {"ok": True, "log": out[-2000:]}


@app.post("/api/apps/{app_id}/disable")
async def disable(app_id: str, user: str = Depends(require_user)):
    cat = catalogue.load()
    dependants = [k for k, v in cat.items() if app_id in v.get("requires", [])]
    still_on = [d for d in dependants if d in config.profiles()]
    if still_on:
        raise HTTPException(409, f"{', '.join(still_on)} depend on {app_id}")
    before = config.profiles()
    kept = [p for p in before if p != app_id]
    pruned = catalogue.prune_orphan_deps(kept, cat)
    stop_ids = sorted(set(before) - set(pruned))
    for sid in stop_ids:
        await compose.run("stop", sid)
        await compose.run("rm", "-f", sid)
    config.set_profiles(pruned)
    await _refresh_mdns()
    return {"ok": True, "stopped": stop_ids}


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


# ── metrics ─────────────────────────────────────────────────────
@app.get("/api/stats/cards")
async def stats_cards(user: str = Depends(require_user)):
    """Sparkline series for the Apps page. Empty when metrics are off."""
    return await promquery.card_series()


@app.get("/api/stats/overview")
async def stats_overview(user: str = Depends(require_user)):
    """Hero numbers for the Stats glass cards. Empty-ish when metrics are off."""
    return await promquery.overview()


@app.get("/api/metrics")
async def prometheus_metrics():
    """Scraped by Prometheus, which cannot log in.

    Deliberately has no auth dependency: no Traefik router points here,
    so it is reachable only from inside the stack. It renders a cache,
    so a wedged app cannot stall the scrape.
    """
    return Response(
        content=metrics.export(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ── updates ─────────────────────────────────────────────────────
@app.get("/api/updates")
async def updates(refresh: bool = False, user: str = Depends(require_user)):
    """Cached by default; the scheduled check refreshes it overnight.

    A digest check hits every image's registry, so doing it on every
    page load is both slow and rude to the registries.
    """
    return await updates_info.fetch(compose, refresh=refresh)


# Helm reaches Docker only through dockerproxy. Recreating that container
# from here stops the proxy mid-apply and leaves it (and often Traefik)
# down. Update it on the host instead.
HOST_ONLY_UPDATES = {
    "dockerproxy": (
        "dockerproxy cannot be updated from Helm — recreating it cuts "
        "Helm's Docker access mid-apply. On the host run:\n"
        "  cd ~/Docker/kine && sudo docker compose pull dockerproxy && "
        "sudo docker compose up -d dockerproxy"
    ),
}


@app.post("/api/updates/{app_id}")
async def apply_update(app_id: str, user: str = Depends(require_user)):
    """Snapshot, pull, recreate, and roll back if it does not come back.

    Nothing here runs on a schedule. Automatic updates are how a working
    media stack breaks overnight, usually mid-import.
    """
    if app_id in HOST_ONLY_UPDATES:
        raise HTTPException(status_code=409, detail=HOST_ONLY_UPDATES[app_id])
    # `compose up -d <svc>` starts a named service even when its profile is
    # off, which is how a disabled Grafana came back after Update All.
    # Always-on plumbing (Traefik, Helm, …) is not catalogue/profile-gated.
    cat = catalogue.load()
    meta = cat.get(app_id) or {}
    is_core = bool(meta.get("hidden")) if app_id in cat else True
    if is_core:
        raise HTTPException(
            status_code=409,
            detail=f"{app_id} is a core container — update it on the host, "
                   "not from Helm",
        )
    if app_id in cat and app_id not in config.profiles():
        raise HTTPException(
            status_code=409,
            detail=f"{app_id} is disabled — enable it before updating, "
                   "or Update All will skip it",
        )
    code, out = await compose.script("updates.sh", "apply", app_id, timeout=1200)
    if code == 0:
        # Clear the badge from the overnight cache immediately so the page
        # does not still say "update" until the next full registry check.
        scheduler.mark_container_current(app_id)
        # updates.sh also heals; run again here so a partial script log
        # still cannot leave Sonarr/Radarr on a dead Gluetun namespace.
        heal = await asyncio.to_thread(tunnel_heal.heal_orphans)
        if heal.get("healed"):
            out = (out or "") + f"\n[tunnel-heal] recreated {', '.join(heal['healed'])}"
        stray = await reconcile_disabled_running()
        if stray:
            out = (out or "") + f"\n[profile-reconcile] stopped {', '.join(stray)}"
    return {"ok": code == 0, "rolled_back": code != 0, "log": out}


# ── VPN ─────────────────────────────────────────────────────────
def _vpn_stack_root() -> str:
    env = config.read()
    return env.get("STACK_ROOT") or os.environ.get("KINE_ROOT", "/stack")


def _vpn_enabled_tunnel_apps(env: dict | None = None) -> set[str]:
    """Catalogue tunnel apps listed in VPN_TUNNELLED_APPS (for override labels)."""
    e = env if env is not None else config.read()
    return {
        a.strip()
        for a in e.get("VPN_TUNNELLED_APPS", "").split(",")
        if a.strip() and a.strip() in vpn_routing.APP_PORTS
    }


def _vpn_ensure_routing_fs(data: dict, env: dict | None = None) -> None:
    """Regenerate wg0 confs + compose/vpn-routing.override.yml (no compose recreate)."""
    e = env if env is not None else config.read()
    vpn_routing.apply_filesystem(
        _vpn_stack_root(),
        _REPO,
        data,
        _vpn_enabled_tunnel_apps(e),
        kine_domain=e.get("KINE_DOMAIN") or "",
        kine_local_domain=e.get("KINE_LOCAL_DOMAIN") or "127.0.0.1.nip.io",
    )


def _vpn_tunnel_group(env: dict | None = None) -> list[str]:
    """Tunnel services to restart/recreate — only apps the user has enabled."""
    _ = env  # callers pass freshly-read env; profiles come from config
    return ["gluetun", "vpn-portsync", *_tunnelled_profiles()]


async def _vpn_recreate_tunnel(env: dict) -> tuple[int, str, list[str]]:
    group = _vpn_tunnel_group(env)
    code, out = await compose.run("up", "-d", "--force-recreate", *group, timeout=300)
    return code, out, group


async def apply_vpn_routing(
    data: dict | None = None,
    *,
    recreate: bool = True,
) -> tuple[int, str, list[str]]:
    """Write confs + override; optionally force-recreate each tunnel group.

    Task 6 routes (set_apps / set_primary / rematerialize) should call this.
    GET /api/vpn uses filesystem ensure only (recreate=False via helper).
    """
    env = config.read()
    stack = _vpn_stack_root()
    store = data
    if store is None:
        store = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    await asyncio.to_thread(_vpn_ensure_routing_fs, store, env)
    if not recreate:
        return 0, "", []

    enabled = _vpn_enabled_tunnel_apps(env)
    active = set(_tunnelled_profiles())
    peers_enabled = enabled & active if active else enabled

    services = ["gluetun"] + [svc for _, svc in vpn_routing.running_secondaries(store)]
    logs: list[str] = []
    recreated: list[str] = []
    last_code = 0
    for svc in services:
        peers = vpn_routing.peers_for(store, svc, peers_enabled)
        group = [svc]
        if svc == "gluetun":
            group.append("vpn-portsync")
        group.extend(peers)
        seen: set[str] = set()
        ordered = [s for s in group if not (s in seen or seen.add(s))]
        code, out = await compose.run(
            "up", "-d", "--force-recreate", *ordered, timeout=300,
        )
        if code != 0:
            last_code = code
        logs.append(out)
        recreated.extend(ordered)
    return last_code, "\n".join(logs), recreated


def _vpn_profile_public(profile: dict, *, primary_id: str | None = None) -> dict:
    return {
        "id": profile.get("id"),
        "name": profile.get("name") or "Unnamed",
        "type": profile.get("type") or "wireguard",
        "updated_at": profile.get("updated_at"),
        "primary": profile.get("id") == primary_id if primary_id is not None else False,
        "apps": list(profile.get("apps") or []),
        "conf": vpn_profiles.redact_conf(profile.get("conf") or ""),
    }


def _vpn_forced_assignable() -> set[str]:
    """Catalogue tunnelled:forced apps that are currently enabled."""
    cat = catalogue.load()
    enabled = set(config.profiles())
    return {
        app
        for app, meta in cat.items()
        if isinstance(meta, dict)
        and meta.get("tunnelled") == "forced"
        and app in enabled
    }


def _vpn_assignable_apps() -> list[dict[str, str]]:
    cat = catalogue.load()
    return [
        {"id": app, "name": (cat.get(app) or {}).get("name") or app}
        for app in sorted(_vpn_forced_assignable())
    ]


def _vpn_running_services(store: dict) -> list[str]:
    """Compose service names for primary + secondaries that own apps."""
    services = ["gluetun"]
    services.extend(svc for _, svc in vpn_routing.running_secondaries(store))
    return services


async def _vpn_probe_tunnel(service: str) -> dict:
    """Best-effort control-server probe for one Gluetun service."""
    code, out = await compose.run(
        "exec", "-T", service,
        "wget", "-qO-",
        "http://127.0.0.1:8000/v1/openvpn/portforwarded",
        timeout=30,
    )
    ip_code, ip_out = await compose.run(
        "exec", "-T", service,
        "wget", "-qO-",
        "http://127.0.0.1:8000/v1/publicip/ip",
        timeout=30,
    )
    public_ip = _parse_public_ip(ip_out) if ip_code == 0 else None
    forwarded_port = _parse_forwarded_port(out) if code == 0 else None
    return {
        "service": service,
        "public_ip": public_ip,
        "forwarded_port": forwarded_port,
        "enabled": public_ip is not None or forwarded_port is not None or code == 0 or ip_code == 0,
    }


async def _vpn_resolve_tunnel(request: Request, tunnel: str | None = None) -> str:
    if tunnel and tunnel.strip():
        return tunnel.strip()
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        for key in ("service", "tunnel"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return "gluetun"


async def _vpn_restart_group(service: str, store: dict | None = None) -> tuple[int, str, list[str]]:
    env = config.read()
    stack = _vpn_stack_root()
    data = store
    if data is None:
        data = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    enabled = _vpn_enabled_tunnel_apps(env)
    active = set(_tunnelled_profiles())
    peers_enabled = enabled & active if active else enabled
    peers = vpn_routing.peers_for(data, service, peers_enabled)
    group = [service]
    if service == "gluetun":
        group.append("vpn-portsync")
    group.extend(peers)
    seen: set[str] = set()
    ordered = [s for s in group if not (s in seen or seen.add(s))]
    code, out = await compose.run("restart", *ordered, timeout=300)
    return code, out, ordered


@app.get("/api/vpn")
async def vpn_status(user: str = Depends(require_user)):
    env = config.read()
    stack = _vpn_stack_root()
    store = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    # Ensure Traefik labels exist after Task 4 stripped static gluetun routers.
    await asyncio.to_thread(_vpn_ensure_routing_fs, store, env)

    vpn_on = env.get("VPN_ENABLED") == "true"
    tunnels: list[dict] = []
    if vpn_on:
        for service in _vpn_running_services(store):
            tunnels.append(await _vpn_probe_tunnel(service))
    else:
        tunnels.append({
            "service": "gluetun",
            "public_ip": None,
            "forwarded_port": None,
            "enabled": False,
        })

    by_service = {t["service"]: t for t in tunnels}
    profiles = vpn_profiles.summary(store)
    for row in profiles:
        if row.get("primary"):
            svc = "gluetun"
        elif row.get("apps"):
            svc = f"gluetun_{vpn_profiles.short_id(row['id'])}"
        else:
            row["tunnel"] = None
            continue
        row["tunnel"] = by_service.get(svc) or {
            "service": svc,
            "public_ip": None,
            "forwarded_port": None,
            "enabled": False,
        }

    primary_tunnel = by_service.get("gluetun") or {}
    return {
        "enabled": vpn_on,
        "provider": env.get("VPN_SERVICE_PROVIDER"),
        "connection_type": _connection_label(env.get("VPN_TYPE", "")),
        "countries": env.get("VPN_SERVER_COUNTRIES"),
        "tunnelled": [a for a in env.get("VPN_TUNNELLED_APPS", "").split(",") if a],
        "forwarded_port": primary_tunnel.get("forwarded_port"),
        "public_ip": primary_tunnel.get("public_ip"),
        "profiles": profiles,
        "assignable_apps": _vpn_assignable_apps(),
        "tunnels": tunnels,
        "tunnels_running": sum(1 for t in tunnels if t.get("enabled")),
        "note": (
            "Per-tunnel public_ip/forwarded_port come from each Gluetun "
            "control server when reachable; otherwise enabled reflects probe failure."
        ),
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


@app.get("/api/vpn/profiles/{profile_id}")
async def vpn_profile_get(profile_id: str, user: str = Depends(require_user)):
    stack = _vpn_stack_root()
    store = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    profile = next((p for p in store["profiles"] if p.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(404, "profile not found")
    return {
        "ok": True,
        "profile": {
            "id": profile.get("id"),
            "name": profile.get("name") or "Unnamed",
            "type": profile.get("type") or "wireguard",
            "updated_at": profile.get("updated_at"),
            "primary": profile.get("id") == store.get("primary_id"),
            "apps": list(profile.get("apps") or []),
            "conf": profile.get("conf") or "",
        },
    }


@app.post("/api/vpn/profiles")
async def vpn_profile_add(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    stack = _vpn_stack_root()
    try:
        profile = await asyncio.to_thread(
            vpn_profiles.add_profile,
            stack,
            body.get("name") or "Unnamed",
            body.get("conf") or "",
            type=body.get("type") or "wireguard",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = await asyncio.to_thread(vpn_profiles.load, stack)
    return {
        "ok": True,
        "profile": _vpn_profile_public(profile, primary_id=store.get("primary_id")),
    }


@app.put("/api/vpn/profiles/{profile_id}")
async def vpn_profile_update(profile_id: str, request: Request, user: str = Depends(require_user)):
    body = await request.json()
    stack = _vpn_stack_root()
    kwargs = {}
    if "name" in body:
        kwargs["name"] = body.get("name")
    if "conf" in body:
        kwargs["conf"] = body.get("conf")
    try:
        profile = await asyncio.to_thread(
            vpn_profiles.update_profile, stack, profile_id, **kwargs
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = await asyncio.to_thread(vpn_profiles.load, stack)
    return {
        "ok": True,
        "profile": _vpn_profile_public(profile, primary_id=store.get("primary_id")),
    }


@app.delete("/api/vpn/profiles/{profile_id}")
async def vpn_profile_delete(profile_id: str, user: str = Depends(require_user)):
    stack = _vpn_stack_root()
    try:
        await asyncio.to_thread(vpn_profiles.delete_profile, stack, profile_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/vpn/profiles/{profile_id}/primary")
async def vpn_profile_set_primary(profile_id: str, user: str = Depends(require_user)):
    stack = _vpn_stack_root()
    try:
        store = await asyncio.to_thread(vpn_profiles.set_primary, stack, profile_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    code, out, group = await apply_vpn_routing(store)
    return {"ok": code == 0, "recreated": group, "log": out[-2000:]}


@app.put("/api/vpn/profiles/{profile_id}/apps")
async def vpn_profile_set_apps(
    profile_id: str, request: Request, user: str = Depends(require_user),
):
    body = await request.json()
    apps = body.get("apps") if isinstance(body, dict) else None
    if not isinstance(apps, list):
        raise HTTPException(400, "body must include apps: list[str]")
    stack = _vpn_stack_root()
    forced = _vpn_forced_assignable()
    try:
        store = await asyncio.to_thread(
            vpn_profiles.set_profile_apps, stack, profile_id, apps, forced=forced,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    code, out, group = await apply_vpn_routing(store)
    return {"ok": code == 0, "recreated": group, "log": out[-2000:]}


@app.post("/api/vpn/profiles/{profile_id}/activate")
async def vpn_profile_activate(profile_id: str, user: str = Depends(require_user)):
    """Rematerialize a profile's WireGuard conf into its tunnel slot, then apply routing."""
    stack = _vpn_stack_root()
    store = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    profile = next((p for p in store["profiles"] if p.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(404, "profile not found")
    if (profile.get("type") or "wireguard") != "wireguard":
        raise HTTPException(400, "OpenVPN profiles cannot be rematerialized yet")
    conf = (profile.get("conf") or "").strip()
    if not conf:
        raise HTTPException(400, "profile has empty config")
    try:
        fields = _parse_wireguard_conf(conf)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not fields or "WIREGUARD_PRIVATE_KEY" not in fields:
        raise HTTPException(400, "invalid WireGuard config")

    # Rematerializing always leaves VPN_ENABLED on so the tunnel group can start.
    if profile_id == store.get("primary_id"):
        await asyncio.to_thread(_write_gluetun_conf, conf + "\n", stack)
        config.write({"VPN_ENABLED": "true", **fields})
    else:
        config.write({"VPN_ENABLED": "true"})
        await asyncio.to_thread(
            _write_secondary_conf, stack, vpn_profiles.short_id(profile_id), conf + "\n",
        )

    code, out, group = await apply_vpn_routing(store)
    return {"ok": code == 0, "recreated": group, "log": out[-2000:]}


@app.post("/api/vpn/disable")
async def vpn_disable(user: str = Depends(require_user)):
    stack = _vpn_stack_root()
    # Keep primary_id for the next enable; only clear materialization.
    store = await asyncio.to_thread(vpn_profiles.migrate_from_wg0, stack)
    await asyncio.to_thread(_remove_gluetun_conf, stack)
    config.write({"VPN_ENABLED": "false", **_empty_vpn_env()})
    code, out, group = await apply_vpn_routing(store)
    return {"ok": code == 0, "recreated": group, "log": out[-2000:]}


@app.post("/api/vpn/restart")
async def vpn_restart(
    request: Request,
    tunnel: str | None = None,
    user: str = Depends(require_user),
):
    service = await _vpn_resolve_tunnel(request, tunnel)
    code, out, group = await _vpn_restart_group(service)
    return {"ok": code == 0, "restarted": group, "log": out[-2000:]}


@app.post("/api/vpn/leaktest")
async def vpn_leaktest(
    request: Request,
    tunnel: str | None = None,
    user: str = Depends(require_user),
):
    """The difference between believing the tunnel works and knowing."""
    service = await _vpn_resolve_tunnel(request, tunnel)
    container = vpn_routing.container_name_for_tunnel_service(service)
    code, out = await compose.script("vpn-leaktest.sh", container, timeout=120)
    return {"result": {0: "ok", 1: "leaking"}.get(code, "inconclusive"), "detail": out}


# ── settings, backup, provisioning ──────────────────────────────


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
    host = nfs_exports.validate_server(server)
    return {
        "server": host,
        "exports": exports,
        "labels": {path: nfs_exports.export_label(path) for path in exports},
        "suggestions": nfs_exports.suggest_assignments(exports),
    }


@app.get("/api/nfs/browse")
async def nfs_browse(server: str = "", path: str = "", user: str = Depends(require_user)):
    """Browse NFS exports and subfolders for the Settings path picker."""
    try:
        return await asyncio.to_thread(nfs_exports.browse, server, path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/nfs/apply")
async def nfs_apply_mounts(user: str = Depends(require_user)):
    try:
        result = await _ensure_nfs_mounted()
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    if result is None:
        return {"ok": True, "log": "No NFS exports configured"}
    if result.get("ok"):
        await _recreate_media_volume_apps()
        result["rescan"] = await _queue_library_sync()
    return result


@app.get("/api/teamarr/leagues")
async def teamarr_leagues(user: str = Depends(require_user)):
    """Last Teamarr league picks (or product defaults) for the enable dialog."""
    recipe = teamarr_setup.recipe()
    stack = pathlib.Path(config.read().get("STACK_ROOT") or "/srv/kine")
    recipe.STACK = stack
    data = recipe.load_leagues()
    return {
        "ok": True,
        "soccer_mode": data.get("soccer_mode") or "manual",
        "leagues": data.get("leagues") or [],
        "catalog": [
            {"id": r["id"], "name": r["name"], "channel_start": r["channel_start"]}
            for r in recipe.DEFAULT_LEAGUES
        ],
        "updated_at": data.get("updated_at"),
    }


@app.get("/api/settings")
async def get_settings(user: str = Depends(require_user)):
    if dispatcharr_sources.enabled() and not dispatcharr_sources.configured():
        await dispatcharr_token.ensure_token(write_env=True)
    env = config.read()
    public = ("KINE_DOMAIN", "KINE_TLS_MODE", "KINE_ACME_EMAIL", "KINE_ACME_DNS_PROVIDER",
              "KINE_TIMEZONE", "STACK_ROOT", "DATA_ROOT", "HELM_UPDATE_CHECK_CRON",
              *_NFS_KEYS, *_MEDIA_SERVER_KEYS, *_LIVE_TV_KEYS, *_SUBTITLE_KEYS)
    out = {k: env.get(k, "") for k in public}
    out["EMBY_BUNDLED"] = "emby" in config.profiles()
    domain = env.get("KINE_DOMAIN", "").strip()
    if out["EMBY_BUNDLED"] and domain:
        out["EMBY_DEFAULT_HOST"] = f"emby.{domain}"
    else:
        out["EMBY_DEFAULT_HOST"] = ""
    out["nzbget_news_servers"] = nzbget_news.recipe().parse_servers(
        env.get(NZBGET_NEWS_KEY, "")
    )
    out["nzbget_enabled"] = "nzbget" in config.profiles()
    out["prowlarr_newznab_indexers"] = [
        {**row, "api_key": "********" if row.get("api_key") else ""}
        for row in prowlarr_newznab.recipe().parse_indexers(
            env.get(PROWLARR_NEWZNAB_KEY, "")
        )
    ]
    out["prowlarr_enabled"] = "prowlarr" in config.profiles()
    out["dispatcharr_enabled"] = dispatcharr_sources.enabled()
    out["dispatcharr_configured"] = dispatcharr_sources.configured()
    cloudns = acme_env.read_cloudns()
    out["CLOUDNS_AUTH_ID"] = cloudns["auth_id"]
    out["CLOUDNS_AUTH_PASSWORD_SET"] = cloudns["password_set"]
    return out


@app.post("/api/settings")
async def set_settings(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    allowed = {"KINE_DOMAIN", "KINE_TLS_MODE", "KINE_ACME_EMAIL",
               "KINE_ACME_DNS_PROVIDER", "KINE_TIMEZONE", "HELM_UPDATE_CHECK_CRON",
               *_NFS_KEYS, *_MEDIA_SERVER_KEYS, *_LIVE_TV_KEYS, *_SUBTITLE_KEYS}
    before = config.read()
    incoming = {k: str(v) for k, v in body.items() if k in allowed}
    config.write(incoming)

    domain_changed = (
        "KINE_DOMAIN" in incoming
        and incoming["KINE_DOMAIN"].strip() != (before.get("KINE_DOMAIN") or "").strip()
    )
    tls_keys = {"KINE_TLS_MODE", "KINE_ACME_EMAIL", "KINE_ACME_DNS_PROVIDER", "KINE_DOMAIN"}
    tls_changed = any(
        k in incoming and incoming[k].strip() != (before.get(k) or "").strip()
        for k in tls_keys
    )
    if tls_changed:
        await compose.script("tls-setup.sh")

    cloudns_changed = False
    if "CLOUDNS_AUTH_ID" in body or "CLOUDNS_AUTH_PASSWORD" in body:
        cloudns_changed = acme_env.write_cloudns(
            auth_id=str(body.get("CLOUDNS_AUTH_ID", "")),
            password=str(body.get("CLOUDNS_AUTH_PASSWORD", "")),
        )

    if domain_changed:
        # Recreating helm from inside a helm request kills this handler (137).
        # Only do it when the domain value actually changed.
        await _apply_domain_routing()
        await _refresh_mdns()
    elif tls_changed or cloudns_changed:
        # Recreate so acme.env / entrypoint ACME flags are picked up.
        await compose.run("up", "-d", "--force-recreate", "traefik")
    nfs_changed = bool(set(_NFS_KEYS) & set(body))
    nfs_mount = None
    if nfs_changed:
        try:
            nfs_mount = await _ensure_nfs_mounted()
        except RuntimeError as exc:
            nfs_mount = {"ok": False, "log": str(exc)}
        if nfs_mount and nfs_mount.get("ok"):
            await _recreate_media_volume_apps()
            changed = set(body) & set(_NFS_KEYS)
            nfs_mount["rescan"] = await _queue_library_sync(changed)
    media_wire = None
    if (
        set(_MEDIA_SERVER_KEYS) & set(body)
        or set(_SUBTITLE_KEYS) & set(body)
        or set(_LIVE_TV_KEYS) & set(body)
    ):
        code, out = await _provision("wire", reason="settings", timeout=900)
        log = out[-2000:] if out else ""
        media_wire = {
            "ok": code == 0 and "wiring failed" not in log.lower()
            and "notification plex failed" not in log.lower()
            and "notification emby failed" not in log.lower(),
            "log": log,
        }
        if "DISPATCHARR_TOKEN" in body:
            dependents = [a for a in ("ecm", "teamarr") if a in config.profiles()]
            if dependents:
                await compose.run(
                    "up", "-d", "--force-recreate", *dependents, timeout=180
                )
    nzbget_apply = None
    if "nzbget_news_servers" in body:
        servers = _store_nzbget_servers(body.get("nzbget_news_servers") or [])
        if "nzbget" in config.profiles():
            await asyncio.to_thread(_apply_nzbget_conf, servers)
            await compose.run("restart", "nzbget")
            nzbget_apply = {"ok": True, "servers": len(servers)}
        else:
            nzbget_apply = {"ok": True, "servers": len(servers), "deferred": True}
    newznab_apply = None
    if "prowlarr_newznab_indexers" in body:
        rows = _store_prowlarr_newznab(body.get("prowlarr_newznab_indexers") or [])
        newznab_apply = await asyncio.to_thread(_apply_prowlarr_newznab, rows)
    return {
        "ok": True,
        "nfs_mount": nfs_mount,
        "media_wire": media_wire,
        "nzbget_apply": nzbget_apply,
        "prowlarr_newznab_apply": newznab_apply,
    }


@app.get("/api/backups")
async def backups_list(user: str = Depends(require_user)):
    await asyncio.to_thread(backups.prune_old_snapshots)
    rows = await asyncio.to_thread(backups.list_snapshots)
    return {"ok": True, "snapshots": rows, "busy": provision_lock.status().get("busy")}


@app.post("/api/backup")
async def backup(user: str = Depends(require_user)):
    if provision_lock.status().get("busy"):
        raise HTTPException(409, "Another stack operation is in progress")
    try:
        async with provision_lock.acquire(reason="backup"):
            code, out = await compose.script("backup.sh", timeout=1800)
    except provision_lock.ProvisionBusy as exc:
        raise HTTPException(409, exc.detail) from exc
    path = out.strip().splitlines()[-1] if code == 0 and out.strip() else None
    if code == 0 and path:
        await asyncio.to_thread(backups.prune_old_snapshots)
        data = scheduler._load()
        data["backup"] = {
            "ran": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ok": True,
            "path": path,
        }
        scheduler._save(data)
    return {"ok": code == 0, "path": path, "log": out[-2000:] if out else ""}


@app.post("/api/backups/restore")
async def backups_restore(request: Request, user: str = Depends(require_user)):
    """Restore a local snapshot: app configs + .env (enabled apps) then up -d."""
    body = await request.json()
    name = str(body.get("name") or "").strip()
    try:
        path = await asyncio.to_thread(backups.resolve, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if provision_lock.status().get("busy"):
        raise HTTPException(409, "Another stack operation is in progress")
    try:
        async with provision_lock.acquire(reason="restore"):
            code, out = await compose.script(
                "restore.sh", str(path), timeout=1800,
            )
    except provision_lock.ProvisionBusy as exc:
        raise HTTPException(409, exc.detail) from exc
    return {
        "ok": code == 0,
        "name": name,
        "log": out[-4000:] if out else "",
    }


def _dispatcharr_http_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "token" in msg.lower():
        return HTTPException(409, msg)
    return HTTPException(400, msg)


@app.get("/api/dispatcharr/m3u")
async def dispatcharr_m3u_list(user: str = Depends(require_user)):
    await dispatcharr_sources.ensure_ready()
    try:
        rows = await asyncio.to_thread(dispatcharr_sources.list_m3u)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True, "accounts": rows}


@app.post("/api/dispatcharr/m3u")
async def dispatcharr_m3u_create(request: Request, user: str = Depends(require_user)):
    await dispatcharr_sources.ensure_ready()
    body = await request.json()
    try:
        row = await asyncio.to_thread(dispatcharr_sources.create_m3u, body)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True, "account": row}


@app.delete("/api/dispatcharr/m3u/{account_id}")
async def dispatcharr_m3u_delete(account_id: int, user: str = Depends(require_user)):
    try:
        await asyncio.to_thread(dispatcharr_sources.delete_m3u, account_id)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True}


@app.post("/api/dispatcharr/m3u/{account_id}/refresh")
async def dispatcharr_m3u_refresh(account_id: int, user: str = Depends(require_user)):
    try:
        await asyncio.to_thread(dispatcharr_sources.refresh_m3u, account_id)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True}


@app.get("/api/dispatcharr/epg")
async def dispatcharr_epg_list(user: str = Depends(require_user)):
    await dispatcharr_sources.ensure_ready()
    try:
        rows = await asyncio.to_thread(dispatcharr_sources.list_epg)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True, "sources": rows}


@app.post("/api/dispatcharr/epg")
async def dispatcharr_epg_create(request: Request, user: str = Depends(require_user)):
    await dispatcharr_sources.ensure_ready()
    body = await request.json()
    try:
        row = await asyncio.to_thread(dispatcharr_sources.create_epg, body)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True, "source": row}


@app.delete("/api/dispatcharr/epg/{source_id}")
async def dispatcharr_epg_delete(source_id: int, user: str = Depends(require_user)):
    try:
        await asyncio.to_thread(dispatcharr_sources.delete_epg, source_id)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True}


@app.post("/api/dispatcharr/epg/{source_id}/refresh")
async def dispatcharr_epg_refresh(source_id: int, user: str = Depends(require_user)):
    try:
        await asyncio.to_thread(dispatcharr_sources.refresh_epg, source_id)
    except ValueError as exc:
        raise _dispatcharr_http_error(exc) from exc
    return {"ok": True}


@app.post("/api/provision")
async def provision(user: str = Depends(require_user)):
    code, out = await _provision("wire", reason="manual wire")
    await _sync_recyclarr()
    return {"ok": code == 0, "log": out}


# ── frontend ────────────────────────────────────────────────────
app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/{path:path}")
async def spa(path: str):
    # Never serve the Helm shell for /view/* — that is the app embed proxy.
    if path == "view" or path.startswith("view/"):
        raise HTTPException(404, "Not found")
    return FileResponse(
        FRONTEND / "index.html",
        headers={"Cache-Control": "no-cache"},
    )
