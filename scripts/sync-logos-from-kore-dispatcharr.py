#!/usr/bin/env python3
"""Import Dispatcharr channel logos from kore → kine.

Dry-run by default. Pass --apply to copy files, upsert Logo rows, and
assign each kine channel the same logo as the kore channel with the same
name (casefold).

  python3 scripts/sync-logos-from-kore-dispatcharr.py
  python3 scripts/sync-logos-from-kore-dispatcharr.py --apply
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
from apps.channels.models import Channel, Logo

logos = [
    {"name": logo.name, "url": logo.url or ""}
    for logo in Logo.objects.order_by("id")
]
channels = []
for ch in Channel.objects.exclude(logo=None).select_related("logo"):
    channels.append({
        "name": ch.name,
        "logo_name": ch.logo.name,
        "logo_url": ch.logo.url or "",
    })
print(json.dumps({"logos": logos, "channels": channels}))
"""


APPLY_PY = r"""
import json, os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel, Logo

spec = SPEC
apply = bool(spec.get("apply"))
report = {
    "logos_created": 0,
    "logos_updated": 0,
    "logos_skipped": 0,
    "channels_linked": 0,
    "channels_skipped": 0,
    "channels_missing": 0,
    "warnings": [],
}

# Index existing logos by casefold name and by url (url is unique).
by_name = {}
by_url = {}
for logo in Logo.objects.all():
    key = (logo.name or "").casefold()
    if key and key not in by_name:
        by_name[key] = logo
    if logo.url and logo.url not in by_url:
        by_url[logo.url] = logo

for row in spec.get("logos") or []:
    name = row.get("name") or ""
    url = row.get("url") or ""
    if not name:
        report["logos_skipped"] += 1
        continue
    key = name.casefold()
    existing = by_name.get(key) or (by_url.get(url) if url else None)
    if existing is None:
        report["logos_created"] += 1
        if apply:
            logo = Logo.objects.create(name=name, url=url)
            by_name[key] = logo
            if url:
                by_url[url] = logo
        else:
            class L: pass
            logo = L(); logo.id = -1; logo.name = name; logo.url = url
            by_name[key] = logo
            if url:
                by_url[url] = logo
        continue
    # Ensure name index points at the resolved row
    by_name[key] = existing
    if url and (existing.url or "") != url:
        # Prefer keeping existing unique url; only fill empty url
        if not existing.url:
            report["logos_updated"] += 1
            if apply:
                existing.url = url
                existing.save(update_fields=["url"])
                by_url[url] = existing
        else:
            report["logos_skipped"] += 1
    else:
        report["logos_skipped"] += 1

# Refresh index after creates (apply only — dry-run stubs already in by_name)
if apply:
    by_name = {}
    by_url = {}
    for logo in Logo.objects.all():
        key = (logo.name or "").casefold()
        if key and key not in by_name:
            by_name[key] = logo
        if logo.url and logo.url not in by_url:
            by_url[logo.url] = logo

channels_by_cf = {}
for ch in Channel.objects.select_related("logo").all():
    key = ch.name.casefold()
    if key not in channels_by_cf:
        channels_by_cf[key] = ch

for row in spec.get("channels") or []:
    ch_name = row.get("name") or ""
    logo_name = row.get("logo_name") or ""
    logo_url = row.get("logo_url") or ""
    ch = channels_by_cf.get(ch_name.casefold())
    if ch is None:
        report["channels_missing"] += 1
        continue
    logo = by_name.get(logo_name.casefold()) if logo_name else None
    if logo is None and logo_url:
        logo = by_url.get(logo_url)
    if logo is None and logo_url:
        report["logos_created"] += 1
        if apply:
            try:
                logo = Logo.objects.create(name=logo_name or ch_name, url=logo_url)
            except Exception:
                logo = Logo.objects.filter(url=logo_url).first()
            if logo is not None:
                by_name[(logo.name or "").casefold()] = logo
                by_url[logo_url] = logo
        else:
            class L: pass
            logo = L(); logo.id = -1; logo.name = logo_name or ch_name; logo.url = logo_url
            by_name[(logo.name or "").casefold()] = logo
            by_url[logo_url] = logo
    if logo is None:
        report["warnings"].append(f"no logo for channel {ch_name!r}")
        report["channels_skipped"] += 1
        continue
    if getattr(logo, "id", None) not in (None, -1) and ch.logo_id == logo.id:
        report["channels_skipped"] += 1
        continue
    # Prefer kore local /data/logos path when both exist for this channel
    report["channels_linked"] += 1
    if apply:
        ch.logo = logo
        ch.save(update_fields=["logo"])

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


def _copy_logo_files(source_ssh: str, source_container: str, dest_ssh: str, dest_container: str) -> dict[str, Any]:
    """Stream kore /data/logos as a tarball into kine /data/logos."""
    # Producer on kore
    prod = subprocess.Popen(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", source_ssh,
            "sudo", "-n", "docker", "exec", source_container,
            "tar", "czf", "-", "-C", "/data", "logos",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Consumer on kine
    cons = subprocess.Popen(
        [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", dest_ssh,
            "sudo", "-n", "docker", "exec", "-i", dest_container,
            "tar", "xzf", "-", "-C", "/data",
        ],
        stdin=prod.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if prod.stdout is not None:
        prod.stdout.close()
    cons_out, cons_err = cons.communicate()
    prod_err = prod.stderr.read() if prod.stderr else b""
    prod.wait()
    if prod.returncode not in (0, None) and prod.returncode != 0:
        raise RuntimeError(f"kore tar failed: {prod_err.decode(errors='replace')}")
    if cons.returncode != 0:
        raise RuntimeError(
            f"kine untar failed: {(cons_err or cons_out).decode(errors='replace')}"
        )
    # Fix ownership to match existing logos dir owner if possible
    subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", dest_ssh,
            "sudo", "-n", "docker", "exec", dest_container,
            "sh", "-c",
            "owner=$(stat -c %u:%g /data/logos 2>/dev/null || echo ''); "
            "chown -R dispatch:dispatch /data/logos 2>/dev/null || "
            "chown -R 1000:1000 /data/logos 2>/dev/null || true; "
            "ls /data/logos | wc -l",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    count_proc = subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes", dest_ssh,
            "sudo", "-n", "docker", "exec", dest_container,
            "sh", "-c", "ls /data/logos | wc -l && du -sh /data/logos",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"files_listing": (count_proc.stdout or "").strip()}


def plan_logo_sync(export: dict, kine_channel_names: set[str]) -> dict[str, int]:
    """Pure helper for tests."""
    kine_cf = {n.casefold() for n in kine_channel_names}
    linkable = sum(
        1 for c in export.get("channels") or []
        if (c.get("name") or "").casefold() in kine_cf
    )
    return {
        "logos": len(export.get("logos") or []),
        "channel_links": len(export.get("channels") or []),
        "linkable_on_kine": linkable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ssh", default="stevec@10.100.100.90")
    parser.add_argument("--source-container", default="dispatcharr")
    parser.add_argument("--dest-ssh", default="stevec@10.100.100.34")
    parser.add_argument("--dest-container", default="kine-dispatcharr")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-files", action="store_true", help="Only DB rows, no /data/logos copy")
    args = parser.parse_args()

    print(f"exporting logos from {args.source_ssh}…", flush=True)
    export = _run_remote_py(args.source_ssh, args.source_container, EXPORT_PY)
    print(
        f"kore logos={len(export.get('logos') or [])} "
        f"channel_links={len(export.get('channels') or [])}",
        flush=True,
    )

    names_py = r"""
import os, django, json
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
django.setup()
from apps.channels.models import Channel
print(json.dumps(list(Channel.objects.values_list("name", flat=True))))
"""
    kine_names = _run_remote_py(args.dest_ssh, args.dest_container, names_py)
    plan = plan_logo_sync(export, set(kine_names))
    print(f"plan {plan} apply={args.apply}", flush=True)

    if args.apply and not args.skip_files:
        print("copying /data/logos …", flush=True)
        files = _copy_logo_files(
            args.source_ssh, args.source_container,
            args.dest_ssh, args.dest_container,
        )
        print(f"  {files.get('files_listing')}", flush=True)

    print(f"{'applying' if args.apply else 'dry-running'} Logo rows + channel links…", flush=True)
    report = _run_remote_py(
        args.dest_ssh,
        args.dest_container,
        APPLY_PY,
        payload={"apply": bool(args.apply), **export},
    )
    print(json.dumps(report, indent=2))
    for w in (report.get("warnings") or [])[:20]:
        print(f"warn: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
