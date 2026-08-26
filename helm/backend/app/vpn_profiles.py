"""Helm-managed VPN profiles (WireGuard MVP)."""
from __future__ import annotations

import json
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import wireguard

PROFILES_REL = pathlib.Path("config/helm/vpn-profiles.json")
WG0_REL = pathlib.Path("config/gluetun/wireguard/wg0.conf")


def profiles_path(stack_root: str) -> pathlib.Path:
    return pathlib.Path(stack_root) / PROFILES_REL


def wg0_path(stack_root: str) -> pathlib.Path:
    return pathlib.Path(stack_root) / WG0_REL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_id(profile_id: str) -> str:
    stripped = profile_id.replace("-", "").lower()
    hex_chars = "".join(c for c in stripped if c in "0123456789abcdef")
    if len(hex_chars) >= 8:
        return hex_chars[:8]
    sanitized = "".join(c for c in profile_id.lower() if c.isalnum())
    if len(sanitized) >= 8:
        return sanitized[:8]
    return sanitized.ljust(8, "0")[:8]


def migrate_schema(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if not out.get("primary_id") and out.get("active_id"):
        out["primary_id"] = out["active_id"]
    out.pop("active_id", None)
    profiles = out.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    for profile in profiles:
        if isinstance(profile, dict) and "apps" not in profile:
            profile["apps"] = []
    out["profiles"] = profiles
    if not out.get("primary_id") and profiles:
        first = profiles[0]
        if isinstance(first, dict):
            out["primary_id"] = first.get("id")
    return out


def empty() -> dict[str, Any]:
    return {"primary_id": None, "profiles": []}


def load(stack_root: str) -> dict[str, Any]:
    path = profiles_path(stack_root)
    if not path.is_file():
        return empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    profiles = data.get("profiles")
    if not isinstance(profiles, list):
        profiles = []
    raw = {
        "primary_id": data.get("primary_id"),
        "active_id": data.get("active_id"),
        "profiles": [p for p in profiles if isinstance(p, dict) and p.get("id")],
    }
    return migrate_schema(raw)


def save(stack_root: str, data: dict[str, Any]) -> None:
    path = profiles_path(stack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def redact_conf(conf: str) -> str:
    out = []
    for line in conf.splitlines():
        if re.match(r"(?i)^\s*PrivateKey\s*=", line):
            key, _, _ = line.partition("=")
            out.append(f"{key.strip()} = ***")
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if conf.endswith("\n") else "")


def summary(data: dict[str, Any]) -> list[dict[str, Any]]:
    primary = data.get("primary_id")
    rows = []
    for p in data.get("profiles") or []:
        rows.append({
            "id": p.get("id"),
            "name": p.get("name") or "Unnamed",
            "type": p.get("type") or "wireguard",
            "updated_at": p.get("updated_at"),
            "primary": p.get("id") == primary,
            "apps": list(p.get("apps") or []),
        })
    return rows


def tunnel_service(data: dict[str, Any], app_id: str) -> str:
    primary_id = data.get("primary_id")
    for profile in data.get("profiles") or []:
        apps = profile.get("apps") or []
        if app_id in apps:
            pid = profile.get("id")
            if pid == primary_id:
                return "gluetun"
            return f"gluetun_{short_id(pid)}"
    return "gluetun"


def migrate_from_wg0(stack_root: str) -> dict[str, Any]:
    path = profiles_path(stack_root)
    if path.is_file():
        return load(stack_root)
    conf_path = wg0_path(stack_root)
    if not conf_path.is_file():
        data = empty()
        save(stack_root, data)
        return data
    conf = conf_path.read_text()
    profile_id = str(uuid.uuid4())
    data = migrate_schema({
        "primary_id": profile_id,
        "profiles": [{
            "id": profile_id,
            "name": "Default",
            "type": "wireguard",
            "conf": conf if conf.endswith("\n") else conf + "\n",
            "updated_at": _now(),
            "apps": [],
        }],
    })
    save(stack_root, data)
    return data


def add_profile(
    stack_root: str,
    name: str,
    conf: str,
    *,
    type: str = "wireguard",
) -> dict[str, Any]:
    kind = (type or "wireguard").strip().lower()
    if kind != "wireguard":
        raise ValueError("only wireguard profiles can be added in this version")
    cleaned = conf.strip()
    if not cleaned:
        raise ValueError("WireGuard config is empty")
    wireguard.parse_conf(cleaned)  # validate
    data = migrate_from_wg0(stack_root)
    profile = {
        "id": str(uuid.uuid4()),
        "name": (name or "Unnamed").strip() or "Unnamed",
        "type": "wireguard",
        "conf": cleaned + "\n",
        "updated_at": _now(),
        "apps": [],
    }
    data["profiles"].append(profile)
    if not data.get("primary_id"):
        data["primary_id"] = profile["id"]
    save(stack_root, data)
    return profile


def update_profile(
    stack_root: str,
    profile_id: str,
    *,
    name: str | None = None,
    conf: str | None = None,
) -> dict[str, Any]:
    data = migrate_from_wg0(stack_root)
    for profile in data["profiles"]:
        if profile.get("id") != profile_id:
            continue
        if name is not None:
            profile["name"] = name.strip() or profile.get("name") or "Unnamed"
        if conf is not None:
            cleaned = conf.strip()
            if not cleaned:
                raise ValueError("WireGuard config is empty")
            if (profile.get("type") or "wireguard") != "wireguard":
                raise ValueError("cannot update openvpn profile in this version")
            wireguard.parse_conf(cleaned)
            profile["conf"] = cleaned + "\n"
        profile["updated_at"] = _now()
        save(stack_root, data)
        return profile
    raise LookupError("profile not found")


def delete_profile(stack_root: str, profile_id: str) -> None:
    data = migrate_from_wg0(stack_root)
    if data.get("primary_id") == profile_id:
        raise ValueError("cannot delete the active profile; activate another first")
    before = len(data["profiles"])
    data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
    if len(data["profiles"]) == before:
        raise LookupError("profile not found")
    save(stack_root, data)


def prepare_activate(stack_root: str, profile_id: str) -> tuple[str, dict[str, str]]:
    """Validate profile, mark active, return (conf_text, vpn_env_fields)."""
    data = migrate_from_wg0(stack_root)
    profile = next((p for p in data["profiles"] if p.get("id") == profile_id), None)
    if not profile:
        raise LookupError("profile not found")
    if (profile.get("type") or "wireguard") != "wireguard":
        raise ValueError("OpenVPN profiles cannot be activated yet")
    conf = (profile.get("conf") or "").strip()
    if not conf:
        raise ValueError("profile has empty config")
    fields = wireguard.parse_conf(conf)
    if not fields or "WIREGUARD_PRIVATE_KEY" not in fields:
        raise ValueError("invalid WireGuard config")
    data["primary_id"] = profile_id
    save(stack_root, data)
    return conf + "\n", fields
