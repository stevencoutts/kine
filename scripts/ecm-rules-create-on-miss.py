#!/usr/bin/env python3
"""Rewrite ECM merge-into-existing rules to create_channel (if_exists=merge).

Dry-run by default. Pass --apply to PUT updates.

On an empty kine, imported kore rules fail with \"Channel not found for merge\"
because they only attach streams to channels that already exist. This converts:

  merge_streams + target=existing_channel + find_channel_by=name_exact
→ create_channel + name_template=<find value> + if_exists=merge

Example (on osiris):

  python3 scripts/ecm-rules-create-on-miss.py --dest-container kine-ecm
  python3 scripts/ecm-rules-create-on-miss.py --dest-container kine-ecm --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


def _name_from_find(find_by: str | None, value: str) -> str | None:
    """Map find_channel_* to a create_channel name_template, or None if unsupported."""
    text = value.strip()
    if not text:
        return None
    if find_by == "name_exact":
        return text
    if find_by == "name_regex":
        # Only ^literal$ (no other regex metacharacters) → literal channel name.
        if text.startswith("^") and text.endswith("$"):
            inner = text[1:-1]
            if inner and not any(ch in inner for ch in r".*+?[](){}|\\"):
                return inner
        return None
    return None


def convert_action(action: dict, *, group_id: int | None = None) -> tuple[dict | None, str | None]:
    """Return (new_action, warning) or (None, None) if unchanged."""
    if not isinstance(action, dict):
        return None, None
    if action.get("type") != "merge_streams":
        return None, None
    if action.get("target") != "existing_channel":
        return None, None
    raw = action.get("find_channel_value")
    if not isinstance(raw, str) or not raw.strip():
        return None, "missing find_channel_value"
    find_by = action.get("find_channel_by")
    name = _name_from_find(find_by if isinstance(find_by, str) else None, raw)
    if name is None:
        return None, f"unsupported find_channel_by={find_by!r} value={raw!r}"
    out: dict[str, Any] = {
        "type": "create_channel",
        "name_template": name,
        "if_exists": "merge",
    }
    if group_id is not None:
        out["group_id"] = group_id
    warn = None
    if action.get("remove_non_matching"):
        warn = "dropped remove_non_matching (not supported on create_channel)"
    return out, warn


def convert_rule_actions(rule: dict) -> tuple[list[dict] | None, list[str]]:
    """Return (new_actions, warnings) or (None, warnings) if no changes."""
    actions = rule.get("actions") or []
    if not isinstance(actions, list):
        return None, ["actions is not a list"]
    group_id = rule.get("target_group_id")
    if not isinstance(group_id, int):
        group_id = None
    changed = False
    warnings: list[str] = []
    out: list[dict] = []
    for action in actions:
        converted, warn = convert_action(action, group_id=group_id)
        if warn:
            warnings.append(f"{rule.get('name')}: {warn}")
        if converted is None:
            out.append(action)
        else:
            out.append(converted)
            changed = True
    return (out if changed else None), warnings


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest-container", default="kine-ecm")
    parser.add_argument("--dest-token", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N updates (0=all)")
    args = parser.parse_args()

    token = args.dest_token or mint_token(args.dest_container)
    payload = dest_request(args.dest_container, token, "GET", "/api/channel-pipeline/rules")
    rows = payload if isinstance(payload, list) else payload.get("rules") or payload.get("items") or []
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected rules payload: {type(payload)}")

    would = 0
    failed = 0
    for rule in rows:
        if not isinstance(rule, dict):
            continue
        new_actions, warnings = convert_rule_actions(rule)
        for w in warnings:
            print(f"warn: {w}")
        if new_actions is None:
            continue
        rid = rule.get("id")
        name = rule.get("name")
        print(
            f"{'update' if args.apply else 'would update'} rule {rid} {name!r}: "
            f"merge_streams(existing) -> create_channel({new_actions[0].get('name_template')!r}, if_exists=merge)"
            if len(new_actions) == 1 and new_actions[0].get("type") == "create_channel"
            else f"{'update' if args.apply else 'would update'} rule {rid} {name!r}"
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
    print(f"=== {mode}: converted={would} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
