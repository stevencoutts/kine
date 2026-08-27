#!/usr/bin/env python3
"""Rebuild kine Dispatcharr channel numbers (and missing channels) from kore.

Dry-run by default. Pass --apply to write.

For each kore channel (by channel_number):
  - match kine by casefold name → set channel_number + channel_group
  - if missing → create channel, attach streams matched by exact name (kore order)
  - set tvg_id; set epg_data when a kine EPGData row shares that tvg_id

Example:

  python3 scripts/sync-channels-from-kore-dispatcharr.py
  python3 scripts/sync-channels-from-kore-dispatcharr.py --apply
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
from apps.channels.models import Channel, ChannelStream
rows = []
for ch in Channel.objects.select_related("channel_group", "epg_data").order_by("channel_number", "id"):
    stream_ids = list(
        ChannelStream.objects.filter(channel=ch).order_by("order", "id").values_list("stream_id", flat=True)
    )
    streams = list(ch.streams.filter(id__in=stream_ids).values("id", "name"))
    by_id = {s["id"]: s["name"] for s in streams}
    ordered = [by_id[i] for i in stream_ids if i in by_id]
    epg = ch.epg_data
    rows.append({
        "name": ch.name,
        "channel_number": float(ch.channel_number) if ch.channel_number is not None else None,
        "group": ch.channel_group.name if ch.channel_group_id else None,
        "tvg_id": ch.tvg_id or "",
        "epg_tvg_id": (epg.tvg_id if epg else "") or "",
        "streams": ordered,
    })
print(json.dumps(rows))
"""


APPLY_PY = r"""
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel, ChannelGroup, ChannelStream, Stream
from apps.epg.models import EPGData

spec = SPEC  # injected by the runner
apply = bool(spec.get("apply"))
rows = spec["rows"]
report = {"renumbered": 0, "created": 0, "streams_linked": 0, "epg_set": 0, "skipped": 0, "warnings": []}

by_cf = {}
for ch in Channel.objects.select_related("channel_group").all():
    key = ch.name.casefold()
    if key in by_cf:
        report["warnings"].append(f"duplicate kine name casefold={key!r}")
        continue
    by_cf[key] = ch

stream_by_name = {}
for sid, name in Stream.objects.values_list("id", "name"):
    if name not in stream_by_name:
        stream_by_name[name] = sid

epg_by_tvg = {}
for eid, tvg in EPGData.objects.values_list("id", "tvg_id"):
    if tvg and tvg not in epg_by_tvg:
        epg_by_tvg[tvg] = eid

group_cache = {g.name: g for g in ChannelGroup.objects.all()}

def ensure_group(name):
    if not name:
        name = "Default Group"
    g = group_cache.get(name)
    if g is not None:
        return g
    if not apply:
        class G: pass
        g = G(); g.id = None; g.name = name
        group_cache[name] = g
        return g
    g, _ = ChannelGroup.objects.get_or_create(name=name)
    group_cache[name] = g
    return g

for row in rows:
    name = row.get("name") or ""
    if not name:
        report["skipped"] += 1
        continue
    key = name.casefold()
    number = row.get("channel_number")
    group = ensure_group(row.get("group"))
    tvg_id = row.get("tvg_id") or row.get("epg_tvg_id") or ""
    epg_id = epg_by_tvg.get(tvg_id) or epg_by_tvg.get(row.get("epg_tvg_id") or "")
    stream_names = row.get("streams") or []

    ch = by_cf.get(key)
    if ch is None:
        report["created"] += 1
        if apply:
            ch = Channel(name=name, channel_number=number, channel_group=group)
            if tvg_id:
                ch.tvg_id = tvg_id
            if epg_id:
                ch.epg_data_id = epg_id
                report["epg_set"] += 1
            ch.save()
            by_cf[key] = ch
            order = 0
            for sname in stream_names:
                sid = stream_by_name.get(sname)
                if sid is None:
                    continue
                _, created_link = ChannelStream.objects.get_or_create(
                    channel=ch, stream_id=sid, defaults={"order": order}
                )
                if created_link:
                    report["streams_linked"] += 1
                order += 1
        else:
            for sname in stream_names:
                if sname in stream_by_name:
                    report["streams_linked"] += 1
            if epg_id:
                report["epg_set"] += 1
        continue

    gname = ch.channel_group.name if ch.channel_group_id else None
    changed = False
    if number is not None and float(ch.channel_number or -1) != float(number):
        changed = True
    if getattr(group, "name", None) != gname:
        changed = True
    if changed:
        report["renumbered"] += 1
        if apply:
            ch.channel_number = number
            if getattr(group, "id", None) is not None:
                ch.channel_group = group
            if tvg_id and not ch.tvg_id:
                ch.tvg_id = tvg_id
            if epg_id and not ch.epg_data_id:
                ch.epg_data_id = epg_id
                report["epg_set"] += 1
            ch.save()
    else:
        report["skipped"] += 1

print(json.dumps(report))
"""

NAME_DUMP_PY = r"""
import os, django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel
print(json.dumps(list(Channel.objects.values_list("name", flat=True))))
"""


def _run_remote_py(
    host: str,
    container: str,
    code: str,
    payload: dict | None = None,
) -> Any:
    """Pipe a Python program into `docker exec -i … python3 -` over SSH."""
    program = code
    if payload is not None:
        program = (
            "import json\n"
            f"SPEC = json.loads({json.dumps(payload)!r})\n"
            + code
        )
    proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
            "sudo", "-n", "docker", "exec", "-i", container,
            "python3", "-",
        ],
        input=program.encode(),
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout or "ssh/docker failed").strip())
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("[") or line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"no JSON in output: {stdout[-800:]!r}")


def plan_actions(
    kore_rows: list[dict],
    kine_names: set[str],
) -> dict[str, list[str]]:
    """Pure helper for tests: classify kore names vs kine casefold set."""
    kine_cf = {n.casefold() for n in kine_names}
    renumber: list[str] = []
    create: list[str] = []
    for row in kore_rows:
        name = row.get("name") or ""
        if not name:
            continue
        if name.casefold() in kine_cf:
            renumber.append(name)
        else:
            create.append(name)
    return {"renumber": renumber, "create": create}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ssh", default="stevec@10.100.100.90")
    parser.add_argument("--source-container", default="dispatcharr")
    parser.add_argument("--dest-ssh", default="stevec@10.100.100.34")
    parser.add_argument("--dest-container", default="kine-dispatcharr")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Only first N kore channels (0=all)")
    args = parser.parse_args()

    print(f"exporting channels from {args.source_ssh}…", flush=True)
    rows = _run_remote_py(args.source_ssh, args.source_container, EXPORT_PY)
    if not isinstance(rows, list):
        raise RuntimeError("unexpected export payload")
    if args.limit:
        rows = rows[: args.limit]
    print(f"kore channels: {len(rows)}", flush=True)

    kine_names = _run_remote_py(args.dest_ssh, args.dest_container, NAME_DUMP_PY)
    plan = plan_actions(rows, set(kine_names))
    print(
        f"plan renumber={len(plan['renumber'])} create={len(plan['create'])} "
        f"(apply={args.apply})",
        flush=True,
    )
    if plan["create"][:5]:
        print(f"  create sample: {plan['create'][:5]}", flush=True)

    print(f"{'applying' if args.apply else 'dry-running'} on {args.dest_ssh}…", flush=True)
    report = _run_remote_py(
        args.dest_ssh,
        args.dest_container,
        APPLY_PY,
        payload={"apply": bool(args.apply), "rows": rows},
    )
    print(json.dumps(report, indent=2))
    for w in (report.get("warnings") or [])[:20]:
        print(f"warn: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
