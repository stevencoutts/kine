"""Shelling to `docker compose`.

Helm could drive the Docker API directly, but Compose already knows how
to resolve profiles, includes and dependency ordering. Reimplementing
that would be a second, subtly different opinion about what the stack
is, which is exactly the drift this design is trying to avoid.
"""
import asyncio
import os
import pathlib

REPO = pathlib.Path(os.environ.get("KINE_REPO", "/repo"))


def _env_file_keys(path: pathlib.Path) -> set[str]:
    keys: set[str] = set()
    if not path.is_file():
        return keys
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def compose_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for `docker compose` subprocesses.

    Compose prefers the process environment over the project `.env` file.
    Helm bakes values like KINE_DOMAIN at container create time; if Settings
    later changes `.env`, those stale process values would win and recreate
    Traefik Host() rules under the old domain. Drop keys present in `.env`
    so the file is the source of truth.
    """
    env = dict(base if base is not None else os.environ)
    for key in _env_file_keys(REPO / ".env"):
        env.pop(key, None)
    return env


async def run(*args: str, timeout: int = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", *args,
        cwd=REPO,
        env=compose_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


async def script(name: str, *args: str, timeout: int = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        str(REPO / "scripts" / name), *args,
        cwd=REPO,
        env=compose_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


async def stream_logs(service: str, tail: int = 200):
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "logs", "-f", f"--tail={tail}", service,
        cwd=REPO,
        env=compose_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        assert proc.stdout
        async for line in proc.stdout:
            yield line.decode(errors="replace").rstrip()
    finally:
        proc.kill()
