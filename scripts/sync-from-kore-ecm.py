#!/usr/bin/env python3
"""One-shot: copy ECM auto-creation rules + task schedules from kore to kine.

Dry-run by default. Pass --apply to write.

Auth uses a short-lived JWT minted inside each ECM container (admin user),
so no password is required when Docker access is available.

Example (on osiris, from the kine checkout):

  python3 scripts/sync-from-kore-ecm.py
  python3 scripts/sync-from-kore-ecm.py --apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_SOURCE_URL = "http://10.100.100.90:6100"
DEFAULT_SOURCE_SSH = "kore"
DEFAULT_SOURCE_CONTAINER = "enhancedchannelmanager-ecm-1"
DEFAULT_DEST_CONTAINER = "kine-ecm"

# Schedule parameter keys that hold kore-local IDs and must not be copied.
_STRIP_PARAM_KEYS = frozenset({"channel_groups", "channel_group_ids", "group_ids"})

_SCHEDULE_CREATE_KEYS = (
    "name",
    "enabled",
    "schedule_type",
    "interval_seconds",
    "schedule_time",
    "timezone",
    "days_of_week",
    "day_of_month",
    "parameters",
)


def _request(
    base: str,
    token: str,
    method: str,
    path: str,
    body: dict | str | None = None,
    *,
    timeout: float = 120.0,
    accept: str = "application/json",
) -> Any:
    url = f"{base.rstrip('/')}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
    }
    if body is not None:
        if isinstance(body, str):
            data = body.encode()
            headers["Content-Type"] = "application/json"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            if not raw:
                return {}
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype:
                return json.loads(raw)
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def mint_token_via_ssh(host: str, container: str) -> str:
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
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
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "source mint failed")
    token = proc.stdout.strip().splitlines()[-1].strip()
    if len(token) < 40:
        raise RuntimeError(f"unexpected source token: {token!r}")
    return token


def mint_token_via_docker(container: str) -> str:
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
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "dest mint failed")
    token = proc.stdout.strip().splitlines()[-1].strip()
    if len(token) < 40:
        raise RuntimeError(f"unexpected dest token: {token!r}")
    return token


def dest_request(
    container: str,
    token: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 300.0,
) -> Any:
    """Call ECM on localhost inside the dest container network namespace."""
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
        ctype = (resp.headers.get("content-type") or "").lower()
        if not raw:
            print("{}")
        elif "json" in ctype:
            sys.stdout.write(raw.decode())
        else:
            print(json.dumps({"_text": raw.decode("utf-8", "replace")}))
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
            raise RuntimeError(err or "dest request failed") from None
    raw = proc.stdout.strip() or "{}"
    return json.loads(raw)


def count_rules_in_yaml(yaml_text: str) -> int:
    """Count top-level rule entries without requiring PyYAML."""
    return sum(1 for line in yaml_text.splitlines() if line.startswith("- name:"))


def schedule_create_payload(row: dict) -> tuple[dict, list[str]]:
    """Build TaskScheduleCreate body; return (payload, warnings)."""
    warnings: list[str] = []
    out: dict[str, Any] = {}
    for key in _SCHEDULE_CREATE_KEYS:
        if key not in row:
            continue
        value = row[key]
        if key == "parameters" and isinstance(value, dict):
            cleaned = dict(value)
            for bad in list(cleaned):
                if bad in _STRIP_PARAM_KEYS:
                    warnings.append(f"stripped parameters.{bad}")
                    cleaned.pop(bad, None)
            out[key] = cleaned
        else:
            out[key] = value
    if "schedule_type" not in out:
        raise ValueError("schedule missing schedule_type")
    # API create enum does not include cron/manual
    if out["schedule_type"] not in {"interval", "daily", "weekly", "biweekly", "monthly"}:
        raise ValueError(f"unsupported schedule_type {out['schedule_type']!r}")
    return out, warnings


def _tasks_by_id(payload: Any) -> dict[str, dict]:
    rows = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}
    return {
        str(r["task_id"]): r
        for r in rows
        if isinstance(r, dict) and r.get("task_id")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--source-ssh", default=DEFAULT_SOURCE_SSH)
    parser.add_argument("--source-container", default=DEFAULT_SOURCE_CONTAINER)
    parser.add_argument("--source-token", default="", help="Bearer JWT; skips SSH mint")
    parser.add_argument("--dest-container", default=DEFAULT_DEST_CONTAINER)
    parser.add_argument("--dest-token", default="", help="Bearer JWT; skips docker mint")
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--schedules-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    do_rules = not args.schedules_only
    do_schedules = not args.rules_only

    source_token = args.source_token or mint_token_via_ssh(args.source_ssh, args.source_container)
    dest_token = args.dest_token or mint_token_via_docker(args.dest_container)

    failed = 0

    if do_rules:
        yaml_text = _request(
            args.source_url,
            source_token,
            "GET",
            "/api/channel-pipeline/export/yaml",
            accept="text/yaml, application/json",
            timeout=180.0,
        )
        if not isinstance(yaml_text, str):
            raise RuntimeError(f"expected YAML text, got {type(yaml_text)}")
        n_rules = count_rules_in_yaml(yaml_text)
        print(f"rules: exported {n_rules} from kore ({len(yaml_text)} bytes)")
        if args.apply:
            result = dest_request(
                args.dest_container,
                dest_token,
                "POST",
                "/api/channel-pipeline/import/yaml",
                {"yaml_content": yaml_text, "overwrite": True},
                timeout=600.0,
            )
            print(f"rules: imported -> {result if not isinstance(result, dict) or len(json.dumps(result)) < 500 else {k: result.get(k) for k in list(result)[:12]}}")
        else:
            print("rules: would import with overwrite=true")

    if do_schedules:
        source_tasks = _tasks_by_id(
            _request(args.source_url, source_token, "GET", "/api/tasks", timeout=60.0)
        )
        dest_tasks = _tasks_by_id(
            dest_request(args.dest_container, dest_token, "GET", "/api/tasks", timeout=60.0)
        )
        print(f"schedules: kore tasks={len(source_tasks)} kine tasks={len(dest_tasks)}")

        for task_id, src in sorted(source_tasks.items()):
            schedules = src.get("schedules") or []
            if task_id not in dest_tasks:
                print(f"skip schedules for {task_id} (missing on kine)")
                continue
            dest_existing = dest_tasks[task_id].get("schedules") or []
            print(
                f"{'replace' if args.apply else 'would replace'} {task_id}: "
                f"kine {len(dest_existing)} -> kore {len(schedules)}"
            )
            if not args.apply:
                for row in schedules:
                    try:
                        payload, warnings = schedule_create_payload(row)
                    except ValueError as exc:
                        print(f"  FAIL build {task_id}: {exc}")
                        failed += 1
                        continue
                    extra = f" ({', '.join(warnings)})" if warnings else ""
                    print(
                        f"  + {payload.get('name') or '(unnamed)'} "
                        f"{payload['schedule_type']} {payload.get('schedule_time') or payload.get('interval_seconds')}{extra}"
                    )
                continue

            # Delete existing dest schedules, then recreate from kore.
            for existing in dest_existing:
                sid = existing.get("id")
                if sid is None:
                    continue
                try:
                    dest_request(
                        args.dest_container,
                        dest_token,
                        "DELETE",
                        f"/api/tasks/{task_id}/schedules/{sid}",
                        timeout=60.0,
                    )
                    print(f"  deleted schedule id={sid}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL delete {task_id}/{sid}: {exc}", file=sys.stderr)
                    failed += 1

            for row in schedules:
                try:
                    payload, warnings = schedule_create_payload(row)
                    for w in warnings:
                        print(f"  warn {task_id}: {w}")
                    dest_request(
                        args.dest_container,
                        dest_token,
                        "POST",
                        f"/api/tasks/{task_id}/schedules",
                        payload,
                        timeout=60.0,
                    )
                    print(f"  created {payload.get('name') or '(unnamed)'}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL create {task_id}: {exc}", file=sys.stderr)
                    failed += 1

    mode = "applied" if args.apply else "dry-run"
    print(f"=== {mode}: failed={failed} ===")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
