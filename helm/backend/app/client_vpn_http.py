"""Laptop VPN HTTP API.

Compose is only asked to start, stop, or remove wg-access. It does not
build an image: the socket proxy rejects the build API.
"""
from __future__ import annotations

import json
import pathlib
import threading

from fastapi import Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import client_vpn, compose, config

_lock = threading.Lock()


def install(app, require_user) -> None:
    @app.get("/api/vpn/clients")
    async def client_vpn_status(user: str = Depends(require_user)):
        state = _load(create=True)
        return client_vpn.summary(state, _read_status())

    @app.put("/api/vpn/clients/settings")
    async def client_vpn_settings(request: Request, user: str = Depends(require_user)):
        body = await request.json()
        current = _load(create=True)
        try:
            raw_host = body.get("endpoint_host", None)
            updated = client_vpn.apply_settings(
                current,
                enabled=bool(body.get("enabled")),
                endpoint_host=None if raw_host is None else str(raw_host),
                host_port=body.get("host_port") or client_vpn.INTERNAL_LISTEN_PORT,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        action = client_vpn.needs_compose(current, updated)
        _save(updated)
        try:
            log = await _apply(action, updated["host_port"])
        except HTTPException:
            _save(client_vpn.failed_start_state(current, updated))
            raise
        return {**client_vpn.summary(updated, _read_status()), "log": log}

    @app.post("/api/vpn/clients/peers")
    async def client_vpn_add_peer(request: Request, user: str = Depends(require_user)):
        body = await request.json()
        state = _load(create=True)
        try:
            client_vpn.add_peer(state, str(body.get("name") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _save(state)
        return client_vpn.summary(state, _read_status())

    @app.delete("/api/vpn/clients/peers/{peer_id}")
    async def client_vpn_remove_peer(peer_id: str, user: str = Depends(require_user)):
        state = _load(create=False)
        try:
            client_vpn.remove_peer(state, peer_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        _save(state)
        return client_vpn.summary(state, _read_status())

    @app.get("/api/vpn/clients/peers/{peer_id}/conf")
    async def client_vpn_conf(peer_id: str, user: str = Depends(require_user)):
        state = _load(create=False)
        try:
            peer = client_vpn.find_peer(state, peer_id)
            text = client_vpn.client_config(state, peer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        filename = client_vpn.config_filename(peer)
        return PlainTextResponse(
            text,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _stack() -> pathlib.Path:
    return pathlib.Path(config.read().get("STACK_ROOT") or "/srv/kine")


def _load(*, create: bool) -> dict:
    path = client_vpn.state_path(_stack())
    with _lock:
        if path.is_file():
            state = client_vpn.load_state(path)
            if state is None:
                raise HTTPException(500, "Client VPN state file is unreadable")
            return state
        if not create:
            raise HTTPException(404, "Client VPN is not set up")
        env = config.read()
        state = client_vpn.fresh_state(env.get("KINE_DOMAIN") or "kine.local")
        client_vpn.save_state(path, state)
        return state


def _save(state: dict) -> None:
    with _lock:
        client_vpn.save_state(client_vpn.state_path(_stack()), state)


def _read_status() -> dict | None:
    path = client_vpn.status_path(_stack())
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


async def _apply(action: str | None, host_port: int) -> str:
    if action is None:
        return ""
    if action == "up":
        config.write({"WG_ACCESS_PORT": str(host_port)})
        code, out = await compose.run(*client_vpn.COMPOSE_UP, timeout=900)
        if code != 0:
            raise HTTPException(502, (out or "could not start the client VPN")[-800:])
        return out
    if action == "stop":
        _code, stopped = await compose.run(*client_vpn.COMPOSE_STOP, timeout=120)
        code, removed = await compose.run(*client_vpn.COMPOSE_RM, timeout=120)
        log = "\n".join(part for part in (stopped, removed) if part)
        if code != 0:
            raise HTTPException(502, log[-800:] or "could not stop the client VPN")
        return log
    raise HTTPException(500, "unknown client VPN action")
