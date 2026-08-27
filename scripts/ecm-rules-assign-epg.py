#!/usr/bin/env python3
"""Append assign_epg (Jesmann) to ECM create_channel pipeline rules.

Dry-run by default. Pass --apply to PUT updates.

  US:* rules → Jesmann - US
  everything else → Jesmann - UK

Example:

  python3 scripts/ecm-rules-assign-epg.py --dest-container kine-ecm
  python3 scripts/ecm-rules-assign-epg.py --dest-container kine-ecm --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def epg_id_for_rule_name(name: str, *, uk_id: int, us_id: int) -> int:
    if (name or "").startswith("US:"):
        return us_id
    return uk_id


def ensure_assign_epg(
    actions: list,
    *,
    epg_id: int,
    set_tvg_id: bool = True,
) -> list[dict] | None:
    """Return new actions with assign_epg, or None if already correct."""
    if not isinstance(actions, list):
        return None
    out: list[dict] = []
    saw = False
    changed = False
    for action in actions:
        if not isinstance(action, dict):
            out.append(action)
            continue
        if action.get("type") != "assign_epg":
            out.append(action)
            continue
        saw = True
        desired = {"type": "assign_epg", "epg_id": epg_id, "set_tvg_id": set_tvg_id}
        if action.get("epg_id") == epg_id and bool(action.get("set_tvg_id", False)) == set_tvg_id:
            out.append(action)
        else:
            out.append(desired)
            changed = True
    if not saw:
        if not any(isinstance(a, dict) and a.get("type") == "create_channel" for a in out):
            return None
        out.append({"type": "assign_epg", "epg_id": epg_id, "set_tvg_id": set_tvg_id})
        changed = True
    return out if changed else None


def mint_token(container: str) -> str:
    cmd = [
        "sudo", "-n", "docker", "exec", container, "python3", "-c",
        (
            "import sqlite3; from auth.tokens import create_access_token; "
            "c=sqlite3.connect('/config/journal.db'); "
            "u=c.execute('select id,username from users where is_admin=1 "
            "order by id limit 1').fetchone(); "
            "assert u, 'no admin user'; "
            "print(create_access_token(u[0], u[1]))"
        ),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "mint failed")
    token = proc.stdout.strip().splitlines()[-1].strip()
    if len(token) < 40:
        raise RuntimeError(f"unexpected token: {token!r}")
    return token


def dest_request(
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
    script = r"""
import json, sys, urllib.request, urllib.error
spec = json.loads(sys.stdin.read())
url = "http://127.0.0.1:6100" + spec["path"]
data = None
headers = {"Authorization": "Bearer " + spec["token"], "Accept": "application/json"}
if spec.get("body") is not None:
    data = json.dumps(spec["body"]).encode()
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(url, data=data, method=spec["method"], headers=headers)
try:
    with urllib.request.urlopen(req, timeout=float(spec.get("timeout") or 120)) as resp:
        raw = resp.read()
        if not raw:
            print("{}")
        else:
            sys.stdout.write(raw.decode())
except urllib.error.HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")[:800]
    print(json.dumps({"_error": f"{spec['method']} {url} -> {exc.code}: {detail}"}))
    sys.exit(1)
"""
    proc = subprocess.run(
        ["sudo", "-n", "docker", "exec", "-i", container, "python3", "-c", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stdout.strip() or proc.stderr.strip()
        try:
            parsed = json.loads(err)
            raise RuntimeError(parsed.get("_error") or err)
        except json.JSONDecodeError:
            raise RuntimeError(err or "request failed") from None
    return json.loads(proc.stdout.strip() or "{}")


def resolve_jesmann_ids(container: str, token: str) -> tuple[int, int]:
    payload = dest_request(container, token, "GET", "/api/epg/sources")
    rows = payload if isinstance(payload, list) else payload.get("results") or []
    uk = us = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip().lower()
        rid = row.get("id")
        if not isinstance(rid, int):
            continue
        if name == "jesmann - uk":
            uk = rid
        elif name == "jesmann - us":
            us = rid
    if uk is None or us is None:
        raise RuntimeError(
            f"need Jesmann - UK and Jesmann - US sources (found uk={uk} us={us})"
        )
    return uk, us


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest-container", default="kine-ecm")
    parser.add_argument("--dest-token", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--uk-epg-id", type=int, default=None)
    parser.add_argument("--us-epg-id", type=int, default=None)
    args = parser.parse_args()

    token = args.dest_token or mint_token(args.dest_container)
    if args.uk_epg_id is not None and args.us_epg_id is not None:
        uk_id, us_id = args.uk_epg_id, args.us_epg_id
    else:
        uk_id, us_id = resolve_jesmann_ids(args.dest_container, token)
    print(f"using Jesmann UK={uk_id} US={us_id}")

    payload = dest_request(args.dest_container, token, "GET", "/api/channel-pipeline/rules")
    rows = payload if isinstance(payload, list) else payload.get("rules") or payload.get("items") or []
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected rules payload: {type(payload)}")

    would = failed = 0
    for rule in rows:
        if not isinstance(rule, dict):
            continue
        name = rule.get("name") or ""
        epg_id = epg_id_for_rule_name(name, uk_id=uk_id, us_id=us_id)
        new_actions = ensure_assign_epg(rule.get("actions") or [], epg_id=epg_id)
        if new_actions is None:
            continue
        rid = rule.get("id")
        print(
            f"{'update' if args.apply else 'would update'} rule {rid} {name!r}: "
            f"assign_epg epg_id={epg_id}"
        )
        would += 1
        if args.apply:
            try:
                dest_request(
                    args.dest_container,
                    token,
                    "PUT",
                    f"/api/channel-pipeline/rules/{rid}",
                    {"actions": new_actions},
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
