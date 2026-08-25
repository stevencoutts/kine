#!/usr/bin/env python3
"""One-shot: copy Dispatcharr M3U + EPG sources from kore to kine.

Dry-run by default. Pass --apply to create missing sources on the destination.

Example (on osiris, from the kine checkout):

  python3 scripts/sync-from-kore-dispatcharr.py \\
    --source-url http://10.100.100.90:9191 \\
    --dest-url http://gluetun:9191 \\
    --dest-token "$DISPATCHARR_TOKEN" \\
    --apply
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

KORE_TEAMARR_EPG = "http://10.100.100.90:9195/api/v1/epg/xmltv"
KINE_TEAMARR_EPG = "http://127.0.0.1:9195/api/v1/epg/xmltv"


def _request(
    base: str,
    token: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 120.0,
) -> Any:
    url = f"{base.rstrip('/')}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-API-Key": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def _unwrap_list(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("results")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _list_m3u(base: str, token: str) -> list[dict]:
    return _unwrap_list(_request(base, token, "GET", "/api/m3u/accounts/"))


def _list_epg(base: str, token: str) -> list[dict]:
    return _unwrap_list(_request(base, token, "GET", "/api/epg/sources/"))


def _export_from_kore_ssh(host: str = "kore") -> list[dict]:
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
        "sudo", "docker", "exec", "dispatcharr", "python3", "-c",
        (
            "import os,django,json; "
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE','dispatcharr.settings'); "
            "import sys; sys.path.insert(0,'/app'); django.setup(); "
            "from apps.m3u.models import M3UAccount; "
            "from apps.epg.models import EPGSource; "
            "rows=[]; "
            "rows+=[{'kind':'m3u','name':a.name,'account_type':a.account_type,"
            "'server_url':a.server_url or '','username':a.username or '',"
            "'password':a.password or '','max_streams':a.max_streams,"
            "'is_active':a.is_active} "
            "for a in M3UAccount.objects.exclude(name='custom')]; "
            "rows+=[{'kind':'epg','name':e.name,'source_type':e.source_type,"
            "'url':e.url or '','username':e.username or '',"
            "'password':e.password or '','is_active':e.is_active} "
            "for e in EPGSource.objects.all()]; "
            "print(json.dumps(rows))"
        ),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "kore export failed")
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("[") or line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("no JSON export from kore")


def _rewrite_epg_url(url: str) -> str:
    if url.strip() == KORE_TEAMARR_EPG:
        return KINE_TEAMARR_EPG
    return url


def _m3u_payload(row: dict) -> dict:
    return {
        "name": row["name"],
        "account_type": row.get("account_type") or "XC",
        "server_url": row.get("server_url") or "",
        "username": row.get("username") or "",
        "password": row.get("password") or "",
        "max_streams": int(row.get("max_streams") or 0),
        "is_active": bool(row.get("is_active", True)),
    }


def _epg_payload(row: dict) -> dict:
    return {
        "name": row["name"],
        "source_type": row.get("source_type") or "xmltv",
        "url": _rewrite_epg_url(row.get("url") or ""),
        "username": row.get("username") or "",
        "password": row.get("password") or "",
        "is_active": bool(row.get("is_active", True)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default="http://10.100.100.90:9191")
    parser.add_argument("--source-token", default="")
    parser.add_argument("--source-ssh", default="", help="SSH host to read kore DB via docker exec")
    parser.add_argument("--source-json", default="", help="JSON file exported from kore")
    parser.add_argument("--dest-url", default="http://gluetun:9191")
    parser.add_argument("--dest-token", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.source_json:
        rows = json.loads(pathlib.Path(args.source_json).read_text())
    elif args.source_ssh:
        rows = _export_from_kore_ssh(args.source_ssh)
    else:
        raise SystemExit("pass --source-json or --source-ssh")

    dest_m3u = {r.get("name"): r for r in _list_m3u(args.dest_url, args.dest_token)}
    dest_epg = {r.get("name"): r for r in _list_epg(args.dest_url, args.dest_token)}

    created = skipped = failed = 0
    for row in rows:
        kind = row.get("kind")
        name = row.get("name") or "?"
        try:
            if kind == "m3u":
                if name in dest_m3u:
                    print(f"skip m3u {name} (already exists)")
                    skipped += 1
                    continue
                payload = _m3u_payload(row)
                print(f"{'create' if args.apply else 'would create'} m3u {name} -> {payload['server_url']}")
                if args.apply:
                    _request(
                        args.dest_url, args.dest_token, "POST",
                        "/api/m3u/accounts/", payload, timeout=600.0,
                    )
                created += 1
            elif kind == "epg":
                if name in dest_epg:
                    print(f"skip epg {name} (already exists)")
                    skipped += 1
                    continue
                payload = _epg_payload(row)
                if payload["url"] != row.get("url"):
                    print(f"  rewrite Teamarr EPG URL -> {payload['url']}")
                print(f"{'create' if args.apply else 'would create'} epg {name} -> {payload['url']}")
                if args.apply:
                    _request(
                        args.dest_url, args.dest_token, "POST",
                        "/api/epg/sources/", payload, timeout=120.0,
                    )
                created += 1
            else:
                print(f"skip unknown kind {kind!r} for {name}")
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {kind} {name}: {exc}", file=sys.stderr)
            failed += 1

    mode = "applied" if args.apply else "dry-run"
    print(f"=== {mode}: created={created} skipped={skipped} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
