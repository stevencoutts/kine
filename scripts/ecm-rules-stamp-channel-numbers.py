#!/usr/bin/env python3
"""Stamp ECM create_channel rules with channel_number from kore Dispatcharr.

Dry-run by default. Pass --apply to PUT updates.

Also sets allow_manual_channel_merge=True so create_channel if_exists=merge
adopts hand-built/synced channels instead of spawning auto-numbered duplicates.

Run from a host that can SSH to kore + osiris:

  python3 scripts/ecm-rules-stamp-channel-numbers.py
  python3 scripts/ecm-rules-stamp-channel-numbers.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


EXPORT_CHANNELS_PY = r"""
import os, django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel
out = {}
for name, num in Channel.objects.values_list("name", "channel_number"):
    if not name or num is None:
        continue
    out.setdefault(name.casefold(), float(num))
print(json.dumps(out))
"""


def stamp_actions(
    actions: list,
    numbers_by_cf: dict[str, float],
) -> tuple[list | None, list[str]]:
    """Return (new_actions, notes) or (None, notes) if create_channel numbers unchanged."""
    if not isinstance(actions, list):
        return None, ["actions not a list"]
    out: list = []
    changed = False
    notes: list[str] = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "create_channel":
            out.append(action)
            continue
        item = dict(action)
        name = item.get("name_template")
        if not isinstance(name, str) or not name.strip():
            out.append(item)
            notes.append("create_channel missing name_template")
            continue
        hit = numbers_by_cf.get(name.casefold())
        if hit is None:
            notes.append(f"no kore number for {name!r}")
            out.append(item)
            continue
        number: int | float = int(hit) if float(hit).is_integer() else float(hit)
        if item.get("channel_number") != number:
            item["channel_number"] = number
            changed = True
        out.append(item)
    return (out if changed else None), notes


def _run_remote_py(host: str, container: str, code: str, *, python: str) -> Any:
    proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
            "sudo", "-n", "docker", "exec", "-i",
            "-e", "PYTHONPATH=/app", "-w", "/app",
            container, python, "-",
        ],
        input=code.encode(),
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout or "ssh/docker failed").strip()[-2000:])
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            return json.loads(line)
    raise RuntimeError(f"no JSON in output: {stdout[-800:]!r}")


def mint_token(ssh: str, container: str) -> str:
    code = (
        "import sqlite3\n"
        "from auth.tokens import create_access_token\n"
        "c=sqlite3.connect('/config/journal.db')\n"
        "u=c.execute('select id,username from users where is_admin=1 "
        "order by id limit 1').fetchone()\n"
        "assert u, 'no admin user'\n"
        "print(create_access_token(u[0], u[1]))\n"
    )
    proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", ssh,
            "sudo", "-n", "docker", "exec", "-i", container, "python3", "-",
        ],
        input=code.encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout).decode(errors="replace").strip() or "mint failed"
        )
    token = proc.stdout.decode().strip().splitlines()[-1].strip()
    if len(token) < 40:
        raise RuntimeError(f"unexpected token: {token!r}")
    return token


def dest_request(
    ssh: str,
    container: str,
    token: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 120.0,
) -> Any:
    payload = {
        "method": method,
        "path": path,
        "token": token,
        "body": body,
        "timeout": timeout,
    }
    script = (
        "import json, sys, urllib.request, urllib.error\n"
        f"spec = json.loads({json.dumps(payload)!r})\n"
        "url = 'http://127.0.0.1:6100' + spec['path']\n"
        "data = None\n"
        "headers = {'Authorization': 'Bearer ' + spec['token'], 'Accept': 'application/json'}\n"
        "if spec.get('body') is not None:\n"
        "    data = json.dumps(spec['body']).encode()\n"
        "    headers['Content-Type'] = 'application/json'\n"
        "req = urllib.request.Request(url, data=data, method=spec['method'], headers=headers)\n"
        "try:\n"
        "    with urllib.request.urlopen(req, timeout=float(spec.get('timeout') or 120)) as resp:\n"
        "        raw = resp.read()\n"
        "        sys.stdout.write(raw.decode() if raw else '{}')\n"
        "except urllib.error.HTTPError as exc:\n"
        "    detail = exc.read().decode('utf-8', 'replace')[:800]\n"
        "    print(json.dumps({'_error': f\"{spec['method']} {url} -> {exc.code}: {detail}\"}))\n"
        "    raise SystemExit(1)\n"
    )
    proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", ssh,
            "sudo", "-n", "docker", "exec", "-i", container, "python3", "-",
        ],
        input=script.encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stdout or proc.stderr).decode(errors="replace").strip()
        try:
            parsed = json.loads(err)
            raise RuntimeError(parsed.get("_error") or err)
        except json.JSONDecodeError:
            raise RuntimeError(err or "request failed") from None
    return json.loads(proc.stdout.decode().strip() or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ssh", default="stevec@10.100.100.90")
    parser.add_argument("--source-container", default="dispatcharr")
    parser.add_argument("--dest-ssh", default="stevec@10.100.100.34")
    parser.add_argument("--dest-container", default="kine-ecm")
    parser.add_argument("--dest-token", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    print(f"exporting channel numbers from {args.source_ssh}…", flush=True)
    numbers = _run_remote_py(
        args.source_ssh,
        args.source_container,
        EXPORT_CHANNELS_PY,
        python="/dispatcharrpy/bin/python",
    )
    print(f"kore channel numbers: {len(numbers)}", flush=True)

    token = args.dest_token or mint_token(args.dest_ssh, args.dest_container)
    payload = dest_request(
        args.dest_ssh, args.dest_container, token, "GET", "/api/channel-pipeline/rules"
    )
    rows = (
        payload
        if isinstance(payload, list)
        else payload.get("rules") or payload.get("items") or []
    )
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected rules payload: {type(payload)}")

    would = failed = 0
    for rule in rows:
        if not isinstance(rule, dict):
            continue
        new_actions, notes = stamp_actions(rule.get("actions") or [], numbers)
        need_manual = not bool(rule.get("allow_manual_channel_merge"))
        if new_actions is None and not need_manual:
            continue
        rid = rule.get("id")
        name = rule.get("name") or ""
        sample = [
            a.get("channel_number")
            for a in (new_actions or rule.get("actions") or [])
            if isinstance(a, dict) and a.get("type") == "create_channel"
        ]
        print(
            f"{'update' if args.apply else 'would update'} rule {rid} {name!r}: "
            f"channel_number={sample[:1]} allow_manual={need_manual} "
            f"notes={notes[:1]}"
        )
        would += 1
        if args.apply:
            body: dict[str, Any] = {}
            if new_actions is not None:
                body["actions"] = new_actions
            if need_manual:
                body["allow_manual_channel_merge"] = True
            try:
                dest_request(
                    args.dest_ssh,
                    args.dest_container,
                    token,
                    "PUT",
                    f"/api/channel-pipeline/rules/{rid}",
                    body,
                    timeout=60.0,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL rule {rid} {name}: {exc}", file=sys.stderr)
                failed += 1
        if args.limit and would >= args.limit:
            break

    mode = "applied" if args.apply else "dry-run"
    print(f"=== {mode}: updated={would} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
