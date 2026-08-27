#!/usr/bin/env python3
"""Stamp ECM create_channel rules with Dispatcharr channel groups from kore.

Sets both create_channel.group_id and rule target_group_id so merges stay
scoped to the kore lineup groups (UK | Sky, UK | Sport, …) instead of
Default Group.

Also reassigns any kine channels whose group differs from kore.

Dry-run by default. Pass --apply to write.

  python3 scripts/ecm-rules-stamp-groups.py
  python3 scripts/ecm-rules-stamp-groups.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


EXPORT_PY = r"""
import os, django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel
out = {}
for ch in Channel.objects.select_related("channel_group"):
    if not ch.name or not ch.channel_group_id:
        continue
    out.setdefault(ch.name.casefold(), {
        "name": ch.name,
        "group": ch.channel_group.name,
    })
print(json.dumps(out))
"""


APPLY_CHANNELS_PY = r"""
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel, ChannelGroup

spec = SPEC
apply = bool(spec["apply"])
wanted = {k: v["group"] for k, v in spec["by_cf"].items()}
groups = {g.name: g for g in ChannelGroup.objects.all()}
report = {"updated": 0, "missing_group": [], "ok": 0}

for ch in Channel.objects.select_related("channel_group"):
    target = wanted.get(ch.name.casefold())
    if not target:
        continue
    cur = ch.channel_group.name if ch.channel_group_id else None
    if cur == target:
        report["ok"] += 1
        continue
    g = groups.get(target)
    if g is None:
        if apply:
            g, _ = ChannelGroup.objects.get_or_create(name=target)
            groups[target] = g
        else:
            report["missing_group"].append(target)
            continue
    report["updated"] += 1
    if apply:
        ch.channel_group = g
        ch.save(update_fields=["channel_group"])

print(json.dumps(report))
"""


GROUP_IDS_PY = r"""
import os, django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import ChannelGroup
print(json.dumps({g.name: g.id for g in ChannelGroup.objects.all()}))
"""


def stamp_rule_groups(
    rule: dict,
    *,
    group_id: int,
) -> dict | None:
    """Return PUT body fields to change, or None if already correct."""
    body: dict[str, Any] = {}
    if rule.get("target_group_id") != group_id:
        body["target_group_id"] = group_id
    actions = rule.get("actions") or []
    if not isinstance(actions, list):
        return body or None
    new_actions: list = []
    changed = False
    for action in actions:
        if not isinstance(action, dict) or action.get("type") != "create_channel":
            new_actions.append(action)
            continue
        item = dict(action)
        if item.get("group_id") != group_id:
            item["group_id"] = group_id
            changed = True
        new_actions.append(item)
    if changed:
        body["actions"] = new_actions
    return body or None


def _run_remote_py(host: str, container: str, code: str, payload: dict | None = None) -> Any:
    program = code
    if payload is not None:
        program = "import json\n" f"SPEC = json.loads({json.dumps(payload)!r})\n" + code
    proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
            "sudo", "-n", "docker", "exec", "-i",
            "-e", "PYTHONPATH=/app", "-w", "/app",
            container, "/dispatcharrpy/bin/python", "-",
        ],
        input=program.encode(),
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout or "failed").strip()[-2000:])
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            return json.loads(line)
    raise RuntimeError(f"no JSON: {stdout[-800:]!r}")


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


def resolve_group_for_rule(rule: dict, by_cf: dict[str, dict]) -> str | None:
    """Pick kore group name for a rule via create_channel name_template, else rule name."""
    for action in rule.get("actions") or []:
        if isinstance(action, dict) and action.get("type") == "create_channel":
            name = action.get("name_template")
            if isinstance(name, str) and name.strip():
                hit = by_cf.get(name.casefold())
                if hit:
                    return hit["group"]
    name = rule.get("name") or ""
    # PK: Foo → Foo; US: BAR stays as US: BAR
    candidates = [name]
    if name.startswith("PK: "):
        candidates.append(name[4:])
    for cand in candidates:
        hit = by_cf.get(cand.casefold())
        if hit:
            return hit["group"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ssh", default="stevec@10.100.100.90")
    parser.add_argument("--source-container", default="dispatcharr")
    parser.add_argument("--dest-ssh", default="stevec@10.100.100.34")
    parser.add_argument("--dest-container", default="kine-dispatcharr")
    parser.add_argument("--ecm-container", default="kine-ecm")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    print(f"exporting kore channel groups from {args.source_ssh}…", flush=True)
    by_cf = _run_remote_py(args.source_ssh, args.source_container, EXPORT_PY)
    print(f"kore channels with groups: {len(by_cf)}", flush=True)

    print(
        f"{'applying' if args.apply else 'dry-running'} channel group sync on "
        f"{args.dest_ssh}…",
        flush=True,
    )
    ch_report = _run_remote_py(
        args.dest_ssh,
        args.dest_container,
        APPLY_CHANNELS_PY,
        payload={"apply": bool(args.apply), "by_cf": by_cf},
    )
    print(f"channels: {json.dumps(ch_report)}", flush=True)

    group_ids = _run_remote_py(args.dest_ssh, args.dest_container, GROUP_IDS_PY)
    print(f"kine group ids loaded: {len(group_ids)}", flush=True)

    token = mint_token(args.dest_ssh, args.ecm_container)
    payload = dest_request(
        args.dest_ssh, args.ecm_container, token, "GET", "/api/channel-pipeline/rules"
    )
    rows = payload if isinstance(payload, list) else payload.get("rules") or []
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected rules payload: {type(payload)}")

    would = failed = skipped = 0
    for rule in rows:
        if not isinstance(rule, dict):
            continue
        gname = resolve_group_for_rule(rule, by_cf)
        if not gname:
            skipped += 1
            print(f"skip rule {rule.get('id')} {rule.get('name')!r}: no kore group")
            continue
        gid = group_ids.get(gname)
        if not isinstance(gid, int):
            skipped += 1
            print(
                f"skip rule {rule.get('id')} {rule.get('name')!r}: "
                f"kine missing group {gname!r}"
            )
            continue
        body = stamp_rule_groups(rule, group_id=gid)
        if body is None:
            continue
        would += 1
        print(
            f"{'update' if args.apply else 'would update'} rule {rule.get('id')} "
            f"{rule.get('name')!r}: group={gname!r} id={gid} fields={list(body)}"
        )
        if args.apply:
            try:
                dest_request(
                    args.dest_ssh,
                    args.ecm_container,
                    token,
                    "PUT",
                    f"/api/channel-pipeline/rules/{rule['id']}",
                    body,
                    timeout=60.0,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL rule {rule.get('id')}: {exc}", file=sys.stderr)
                failed += 1
        if args.limit and would >= args.limit:
            break

    mode = "applied" if args.apply else "dry-run"
    print(f"=== {mode}: rules_updated={would} failed={failed} skipped={skipped} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
