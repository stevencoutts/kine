"""Read/write ClouDNS credentials in Traefik's acme.env."""
from __future__ import annotations

import os
import pathlib
import re
import tempfile

_AUTH_ID = "CLOUDNS_AUTH_ID"
_AUTH_PASSWORD = "CLOUDNS_AUTH_PASSWORD"
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def acme_env_path(stack_root: str | pathlib.Path | None = None) -> pathlib.Path:
    root = pathlib.Path(
        stack_root
        or os.environ.get("KINE_ROOT")
        or "/stack"
    )
    return root / "config" / "traefik" / "acme.env"


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def read_cloudns(path: pathlib.Path | None = None) -> dict[str, object]:
    """Return auth_id and whether a password is stored (never the password)."""
    target = path or acme_env_path()
    if not target.is_file():
        return {"auth_id": "", "password_set": False}
    try:
        data = _parse(target.read_text())
    except OSError:
        return {"auth_id": "", "password_set": False}
    return {
        "auth_id": (data.get(_AUTH_ID) or "").strip(),
        "password_set": bool((data.get(_AUTH_PASSWORD) or "").strip()),
    }


def write_cloudns(
    path: pathlib.Path | None = None,
    *,
    auth_id: str = "",
    password: str = "",
) -> bool:
    """Upsert ClouDNS keys. Blank password leaves the existing value.

    Returns True when the file content changed.
    """
    target = path or acme_env_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    existing_text = ""
    if target.is_file():
        try:
            existing_text = target.read_text()
        except OSError:
            existing_text = ""
    parsed = _parse(existing_text)

    new_id = (auth_id or "").strip()
    new_pw = (password or "").strip()
    old_id = (parsed.get(_AUTH_ID) or "").strip()
    old_pw = (parsed.get(_AUTH_PASSWORD) or "").strip()

    final_id = new_id
    final_pw = new_pw if new_pw else old_pw

    if final_id == old_id and final_pw == old_pw and target.is_file():
        return False

    lines: list[str] = []
    seen = set()
    for raw in existing_text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in {_AUTH_ID, _AUTH_PASSWORD}:
                if key not in seen:
                    if key == _AUTH_ID:
                        lines.append(f"{_AUTH_ID}={final_id}")
                    else:
                        lines.append(f"{_AUTH_PASSWORD}={final_pw}")
                    seen.add(key)
                continue
        lines.append(raw)

    if _AUTH_ID not in seen:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{_AUTH_ID}={final_id}")
        seen.add(_AUTH_ID)
    if _AUTH_PASSWORD not in seen:
        lines.append(f"{_AUTH_PASSWORD}={final_pw}")

    body = "\n".join(lines).rstrip() + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".acme.env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return True
