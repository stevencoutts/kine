#!/usr/bin/env python3
"""Rewrite ECM merge-into-existing rules to create_channel (if_exists=merge).

Dry-run by default. Pass --apply to PUT updates.

On an empty kine, imported kore rules fail with \"Channel not found for merge\"
because they only attach streams to channels that already exist. This converts:

  merge_streams + target=existing_channel + find_channel_by=name_exact
→ create_channel + name_template=<find value> + if_exists=merge + group_id

Dispatcharr rejects create without channel_group_id, so every create_channel
action also gets group_id (from the rule's target_group_id, or
--default-group-id / Default Group). Already-converted rules missing group_id
are patched the same way.

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


def ensure_create_group(action: dict, *, group_id: int) -> dict | None:
    """Add group_id to a create_channel action that lacks one."""
    if not isinstance(action, dict) or action.get("type") != "create_channel":
        return None
    existing = action.get("group_id")
    if isinstance(existing, int):
        return None
    out = dict(action)
    out["group_id"] = group_id
    return out


def convert_rule_actions(
    rule: dict,
    *,
    default_group_id: int | None = None,
) -> tuple[list[dict] | None, list[str], int | None]:
    """Return (new_actions, warnings, target_group_id) or (None, warnings, None).

    target_group_id is set when the rule should also receive that field on PUT.
    """
    actions = rule.get("actions") or []
    if not isinstance(actions, list):
        return None, ["actions is not a list"], None

    group_id = rule.get("target_group_id")
    if not isinstance(group_id, int):
        group_id = default_group_id
    need_rule_group = (
        not isinstance(rule.get("target_group_id"), int)
        and isinstance(default_group_id, int)
    )

    changed = False
    warnings: list[str] = []
    out: list[dict] = []
    for action in actions:
        converted, warn = convert_action(action, group_id=group_id)
        if warn:
            warnings.append(f"{rule.get('name')}: {warn}")
        if converted is not None:
            out.append(converted)
            changed = True
            continue
        if isinstance(group_id, int):
            patched = ensure_create_group(action, group_id=group_id)
            if patched is not None:
                out.append(patched)
                changed = True
                continue
        out.append(action)

    has_create = any(
        isinstance(a, dict) and a.get("type") == "create_channel" for a in out
    )
    rule_group: int | None = None
    if need_rule_group and has_create:
        rule_group = default_group_id
        changed = True

    return (out if changed else None), warnings, rule_group


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


def resolve_default_group_id(container: str, token: str, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    payload = dest_request(container, token, "GET", "/api/channel-groups")
    rows = payload if isinstance(payload, list) else payload.get("results") or []
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("no channel groups found; pass --default-group-id")
    for row in rows:
        if isinstance(row, dict) and row.get("name") == "Default Group" and isinstance(row.get("id"), int):
            return row["id"]
    first = rows[0]
    if isinstance(first, dict) and isinstance(first.get("id"), int):
        return first["id"]
    raise RuntimeError("could not resolve a channel group id; pass --default-group-id")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest-container", default="kine-ecm")
    parser.add_argument("--dest-token", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N updates (0=all)")
    parser.add_argument(
        "--default-group-id",
        type=int,
        default=None,
        help="Dispatcharr channel_group_id for create_channel (default: Default Group)",
    )
    args = parser.parse_args()

    token = args.dest_token or mint_token(args.dest_container)
    default_group_id = resolve_default_group_id(
        args.dest_container, token, args.default_group_id
    )
    print(f"using channel group_id={default_group_id}")

    payload = dest_request(args.dest_container, token, "GET", "/api/channel-pipeline/rules")
    rows = payload if isinstance(payload, list) else payload.get("rules") or payload.get("items") or []
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected rules payload: {type(payload)}")

    would = 0
    failed = 0
    for rule in rows:
        if not isinstance(rule, dict):
            continue
        new_actions, warnings, rule_group = convert_rule_actions(
            rule, default_group_id=default_group_id
        )
        for w in warnings:
            print(f"warn: {w}")
        if new_actions is None:
            continue
        rid = rule.get("id")
        name = rule.get("name")
        gid = next(
            (a.get("group_id") for a in new_actions if a.get("type") == "create_channel"),
            rule_group,
        )
        print(
            f"{'update' if args.apply else 'would update'} rule {rid} {name!r}: "
            f"create_channel group_id={gid}"
            + (f" target_group_id={rule_group}" if rule_group is not None else "")
        )
        would += 1
        if args.apply:
            body: dict[str, Any] = {"actions": new_actions}
            if rule_group is not None:
                body["target_group_id"] = rule_group
            try:
                dest_request(
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
    print(f"=== {mode}: converted={would} failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
