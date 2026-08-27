#!/usr/bin/env python3
"""Deduplicate kine Dispatcharr channels after ECM spawned name-colliding copies.

For each casefold name with multiple channels:
  - keeper = channel whose number matches kore (else lowest id)
  - move streams from losers onto keeper
  - delete losers
Then renumber keepers to match kore.

Dry-run by default. Pass --apply to write.

  python3 scripts/dedupe-channels-from-kore.py
  python3 scripts/dedupe-channels-from-kore.py --apply
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
for name, num in Channel.objects.values_list("name", "channel_number"):
    if not name or num is None:
        continue
    out.setdefault(name.casefold(), float(num))
print(json.dumps(out))
"""


APPLY_PY = r"""
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from collections import defaultdict
from apps.channels.models import Channel, ChannelStream

spec = SPEC  # injected
apply = bool(spec.get("apply"))
numbers = {k: float(v) for k, v in spec["numbers"].items()}
report = {
    "dup_groups": 0,
    "deleted": 0,
    "streams_moved": 0,
    "renumbered": 0,
    "kept": 0,
    "final_count": 0,
    "warnings": [],
}

groups = defaultdict(list)
for ch in Channel.objects.all().order_by("id"):
    groups[ch.name.casefold()].append(ch)

for key, chs in groups.items():
    target = numbers.get(key)
    if len(chs) == 1:
        keeper = chs[0]
    else:
        report["dup_groups"] += 1
        if target is not None:
            matches = [
                c for c in chs if float(c.channel_number or -1) == float(target)
            ]
            keeper = matches[0] if matches else chs[0]
        else:
            keeper = chs[0]
            report["warnings"].append(f"no kore number for {chs[0].name!r}")
        for loser in chs:
            if loser.id == keeper.id:
                continue
            for link in ChannelStream.objects.filter(channel=loser).order_by("order", "id"):
                exists = ChannelStream.objects.filter(
                    channel=keeper, stream_id=link.stream_id
                ).exists()
                if not exists:
                    if apply:
                        max_order = (
                            ChannelStream.objects.filter(channel=keeper)
                            .order_by("-order")
                            .values_list("order", flat=True)
                            .first()
                        )
                        ChannelStream.objects.create(
                            channel=keeper,
                            stream_id=link.stream_id,
                            order=(max_order + 1) if max_order is not None else 0,
                        )
                    report["streams_moved"] += 1
            if apply:
                loser.delete()
            report["deleted"] += 1
        report["kept"] += 1

    if target is not None and float(keeper.channel_number or -1) != float(target):
        report["renumbered"] += 1
        if apply:
            keeper.channel_number = target
            keeper.save(update_fields=["channel_number"])

report["final_count"] = Channel.objects.count() if apply else (
    Channel.objects.count() - report["deleted"]
)
print(json.dumps(report))
"""


def _run_remote_py(
    host: str,
    container: str,
    code: str,
    payload: dict | None = None,
) -> Any:
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
            "sudo", "-n", "docker", "exec", "-i",
            "-e", "PYTHONPATH=/app", "-w", "/app",
            container,
            "/dispatcharrpy/bin/python", "-",
        ],
        input=program.encode(),
        capture_output=True,
        check=False,
    )
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout or "ssh/docker failed").strip()[-2000:])
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("[") or line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"no JSON in output: {stdout[-800:]!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ssh", default="stevec@10.100.100.90")
    parser.add_argument("--source-container", default="dispatcharr")
    parser.add_argument("--dest-ssh", default="stevec@10.100.100.34")
    parser.add_argument("--dest-container", default="kine-dispatcharr")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    print(f"exporting kore numbers from {args.source_ssh}…", flush=True)
    numbers = _run_remote_py(args.source_ssh, args.source_container, EXPORT_PY)
    print(f"kore numbers: {len(numbers)}", flush=True)

    print(
        f"{'applying' if args.apply else 'dry-running'} dedupe on {args.dest_ssh}…",
        flush=True,
    )
    report = _run_remote_py(
        args.dest_ssh,
        args.dest_container,
        APPLY_PY,
        payload={"apply": bool(args.apply), "numbers": numbers},
    )
    print(json.dumps(report, indent=2))
    for w in (report.get("warnings") or [])[:20]:
        print(f"warn: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
