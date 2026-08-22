"""Shelling to `docker compose`.

Helm could drive the Docker API directly, but Compose already knows how
to resolve profiles, includes and dependency ordering. Reimplementing
that would be a second, subtly different opinion about what the stack
is, which is exactly the drift this design is trying to avoid.
"""
import asyncio
import os
import pathlib

REPO = pathlib.Path(os.environ.get("MC_REPO", "/repo"))


async def run(*args: str, timeout: int = 600) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", *args,
        cwd=REPO,
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
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        assert proc.stdout
        async for line in proc.stdout:
            yield line.decode(errors="replace").rstrip()
    finally:
        proc.kill()
