"""Backup listing helpers."""
import os
import pathlib
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import backups  # noqa: E402


def _prepare_stack(tmp_path: pathlib.Path) -> pathlib.Path:
    stack = tmp_path / "stack"
    (stack / "config" / "sonarr").mkdir(parents=True)
    (stack / "config" / "radarr").mkdir(parents=True)
    (stack / "config" / "sonarr" / "config.xml").write_text("sonarr-cfg")
    (stack / "config" / "radarr" / "config.xml").write_text("radarr-cfg")
    (stack / "backups").mkdir(parents=True)
    # A scheduled tarball so the prune glob matches; otherwise `ls` of the
    # empty pattern fails the script before we can inspect the new snapshot.
    (stack / "backups" / "kine-20200101-000000.tar.gz").write_bytes(b"old")
    (tmp_path / ".env").write_text(f"STACK_ROOT={stack}\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / "catalogue.yml").write_text("apps: []\n")
    (tmp_path / "compose").mkdir()
    (tmp_path / "compose" / "acq.sonarr.yml").write_text("x: 1\n")
    return stack


def _run_backup(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "backup.sh"), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )


def _tarball_names(path: pathlib.Path) -> set[str]:
    with tarfile.open(path) as tf:
        return set(tf.getnames())


def test_validate_name_accepts_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    assert backups.validate_name("kine-20260825-093000.tar.gz") == "kine-20260825-093000.tar.gz"


def test_validate_name_rejects_path_traversal():
    try:
        backups.validate_name("../kine-20260825-093000.tar.gz")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_validate_name_rejects_bad_pattern():
    try:
        backups.validate_name("evil.tar.gz")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_validate_name_accepts_per_app_update_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    assert backups.validate_name("kine-20260829-141000-bazarr.tar.gz") == (
        "kine-20260829-141000-bazarr.tar.gz"
    )


def test_list_snapshots_includes_update_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "kine-20260829-010000.tar.gz").write_bytes(b"sched")
    (bdir / "kine-20260829-141000-bazarr.tar.gz").write_bytes(b"upd")
    rows = {r["name"]: r for r in backups.list_snapshots()}
    assert rows["kine-20260829-010000.tar.gz"]["kind"] == "scheduled"
    assert rows["kine-20260829-141000-bazarr.tar.gz"]["kind"] == "update"
    assert rows["kine-20260829-141000-bazarr.tar.gz"]["app"] == "bazarr"


def test_list_snapshots_newest_first(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    older = bdir / "kine-20260824-111846.tar.gz"
    newer = bdir / "kine-20260825-093000.tar.gz"
    older.write_bytes(b"a" * 10)
    newer.write_bytes(b"b" * 20)
    rows = backups.list_snapshots()
    assert [r["name"] for r in rows] == [
        "kine-20260825-093000.tar.gz",
        "kine-20260824-111846.tar.gz",
    ]
    assert rows[0]["size_bytes"] == 20


def test_resolve_requires_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    (tmp_path / "backups").mkdir()
    try:
        backups.resolve("kine-20260825-093000.tar.gz")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


def test_resolve_returns_path(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    name = "kine-20260825-093000.tar.gz"
    (bdir / name).write_bytes(b"x")
    assert backups.resolve(name).name == name


def test_prune_old_snapshots_keeps_three_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    names = [
        "kine-20260820-010000.tar.gz",
        "kine-20260821-010000.tar.gz",
        "kine-20260822-010000.tar.gz",
        "kine-20260823-010000.tar.gz",
        "kine-20260824-010000.tar.gz",
    ]
    for name in names:
        (bdir / name).write_bytes(b"x")
    removed = backups.prune_old_snapshots(keep=3)
    assert set(removed) == {
        "kine-20260820-010000.tar.gz",
        "kine-20260821-010000.tar.gz",
    }
    remaining = sorted(p.name for p in bdir.glob("kine-*.tar.gz"))
    assert remaining == [
        "kine-20260822-010000.tar.gz",
        "kine-20260823-010000.tar.gz",
        "kine-20260824-010000.tar.gz",
    ]
    assert [r["name"] for r in backups.list_snapshots()] == [
        "kine-20260824-010000.tar.gz",
        "kine-20260823-010000.tar.gz",
        "kine-20260822-010000.tar.gz",
    ]


def test_prune_old_snapshots_keeps_one_update_per_app(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    names = [
        "kine-20260901-010000-bazarr.tar.gz",
        "kine-20260902-010000-bazarr.tar.gz",
        "kine-20260903-010000-sonarr.tar.gz",
        "kine-20260904-010000-sonarr.tar.gz",
        "kine-20260905-010000-sonarr.tar.gz",
        "kine-20260906-010000.tar.gz",
    ]
    for name in names:
        (bdir / name).write_bytes(b"x")
    removed = backups.prune_old_snapshots(keep=3)
    assert set(removed) == {
        "kine-20260901-010000-bazarr.tar.gz",
        "kine-20260903-010000-sonarr.tar.gz",
        "kine-20260904-010000-sonarr.tar.gz",
    }
    remaining = sorted(p.name for p in bdir.glob("kine-*.tar.gz"))
    assert remaining == [
        "kine-20260902-010000-bazarr.tar.gz",
        "kine-20260905-010000-sonarr.tar.gz",
        "kine-20260906-010000.tar.gz",
    ]


def test_prune_old_snapshots_spares_update_tarballs(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    scheduled = [
        "kine-20260820-010000.tar.gz",
        "kine-20260821-010000.tar.gz",
        "kine-20260822-010000.tar.gz",
        "kine-20260823-010000.tar.gz",
    ]
    update = "kine-20260819-010000-bazarr.tar.gz"
    for name in scheduled + [update]:
        (bdir / name).write_bytes(b"x")
    removed = backups.prune_old_snapshots(keep=3)
    assert update not in removed
    assert (bdir / update).is_file()
    remaining_scheduled = sorted(
        p.name for p in bdir.glob("kine-*.tar.gz") if "-bazarr" not in p.name
    )
    assert remaining_scheduled == [
        "kine-20260821-010000.tar.gz",
        "kine-20260822-010000.tar.gz",
        "kine-20260823-010000.tar.gz",
    ]


def test_delete_snapshots_unlinks_each_named_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    names = [
        "kine-20260908-141000-bazarr.tar.gz",
        "kine-20260908-150000.tar.gz",
    ]
    for name in names:
        (bdir / name).write_bytes(b"x")
    (bdir / "kine-20260908-160000-sonarr.tar.gz").write_bytes(b"keep")
    deleted = backups.delete_snapshots(names)
    assert deleted == names
    assert not (bdir / names[0]).exists()
    assert not (bdir / names[1]).exists()
    assert (bdir / "kine-20260908-160000-sonarr.tar.gz").is_file()


def test_delete_snapshot_unlinks_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backups.config, "read", lambda: {"STACK_ROOT": str(tmp_path)})
    bdir = tmp_path / "backups"
    bdir.mkdir()
    name = "kine-20260829-141000-bazarr.tar.gz"
    (bdir / name).write_bytes(b"x")
    backups.delete_snapshot(name)
    assert not (bdir / name).exists()


def test_backup_script_keeps_three_snapshots():
    script = (ROOT / "scripts" / "backup.sh").read_text()
    assert "tail -n +4" in script
    assert "Keep the last 3" in script or "keep 3" in script.lower()
    # Per-app update snapshots must not match the prune glob.
    assert "kine-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].tar.gz" in script


def test_backup_api_prunes_after_success_and_list():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    backup_fn = main.split('@app.post("/api/backup")', 1)[1].split("@app.post(", 1)[0]
    assert "prune_old_snapshots" in backup_fn
    list_fn = main.split('@app.get("/api/backups")', 1)[1].split("@app.", 1)[0]
    assert "prune_old_snapshots" in list_fn
    apply_fn = main.split('@app.post("/api/updates/{app_id}")', 1)[1].split("@app.", 1)[0]
    assert "prune_old_snapshots" in apply_fn
    assert '@app.get("/api/backups/{name}/file")' in main
    assert '@app.delete("/api/backups/{name}")' in main
    assert '@app.post("/api/backups/delete")' in main
    assert "delete_snapshots" in main
    assert "delete_snapshot" in main
    restore_fn = main.split('@app.post("/api/backups/restore")', 1)[1].split("@app.", 1)[0]
    assert "snapshot_app" in restore_fn


def test_per_app_backup_prunes_older_snaps_of_that_app(tmp_path):
    _prepare_stack(tmp_path)
    bdir = tmp_path / "stack" / "backups"
    (bdir / "kine-20200101-000000-sonarr.tar.gz").write_bytes(b"old1")
    (bdir / "kine-20200102-000000-sonarr.tar.gz").write_bytes(b"old2")
    (bdir / "kine-20200103-000000-radarr.tar.gz").write_bytes(b"other")
    result = _run_backup(tmp_path, "sonarr")
    assert result.returncode == 0, result.stderr
    sonarr = sorted(p.name for p in bdir.glob("kine-*-sonarr.tar.gz"))
    assert len(sonarr) == 1
    assert sonarr[0].endswith("-sonarr.tar.gz")
    assert sonarr[0] != "kine-20200101-000000-sonarr.tar.gz"
    assert (bdir / "kine-20200103-000000-radarr.tar.gz").is_file()


def test_per_app_backup_only_includes_that_app_config(tmp_path):
    """Update snapshots must not pack every app's config — rollback only
    restores config/<app>, and the full tree is what made apply look hung."""
    _prepare_stack(tmp_path)
    result = _run_backup(tmp_path, "sonarr")
    assert result.returncode == 0, result.stderr
    snap = pathlib.Path(result.stdout.strip().splitlines()[-1])
    assert snap.name.endswith("-sonarr.tar.gz")
    names = _tarball_names(snap)
    assert "config/sonarr/config.xml" in names
    assert "config/radarr/config.xml" not in names
    assert ".env" not in names
    assert "docker-compose.yml" not in names


def test_scheduled_backup_still_includes_full_config(tmp_path):
    _prepare_stack(tmp_path)
    result = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    snap = pathlib.Path(result.stdout.strip().splitlines()[-1])
    names = _tarball_names(snap)
    assert "config/sonarr/config.xml" in names
    assert "config/radarr/config.xml" in names
    assert ".env" in names


def test_per_app_backup_rejects_path_traversal(tmp_path):
    _prepare_stack(tmp_path)
    result = _run_backup(tmp_path, "../etc")
    assert result.returncode != 0
    assert "invalid" in result.stderr.lower()
    assert not (tmp_path / "stack" / "etc.tar.gz").exists()


def test_per_app_backup_succeeds_when_config_dir_missing(tmp_path):
    """Sidecars like ecm-mcp share another app's config (or have none).
    GNU tar exits 2 if config/<app> is missing, which aborted updates."""
    _prepare_stack(tmp_path)
    result = _run_backup(tmp_path, "ecm-mcp")
    assert result.returncode == 0, result.stderr
    # BSD tar exits 1 (treated as a warning); GNU tar exits 2 (fatal).
    # Either way, a missing sidecar dir must not be passed to tar.
    assert "Cannot stat" not in result.stderr
    snap = pathlib.Path(result.stdout.strip().splitlines()[-1])
    assert snap.is_file()
    assert snap.name.endswith("-ecm-mcp.tar.gz")
    names = _tarball_names(snap)
    assert "config/sonarr/config.xml" not in names
    assert "config/radarr/config.xml" not in names
    assert not any(n == "config/ecm-mcp" or n.startswith("config/ecm-mcp/") for n in names)


def _run_restore(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    fake_bin = cwd / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / "restore.sh"), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
    )


def test_per_app_restore_skips_extract_when_snapshot_has_no_config(tmp_path):
    """A sidecar update snapshot is empty; rollback must not fail tar extract."""
    stack = _prepare_stack(tmp_path)
    snap_result = _run_backup(tmp_path, "ecm-mcp")
    assert snap_result.returncode == 0, snap_result.stderr
    snap = pathlib.Path(snap_result.stdout.strip().splitlines()[-1])
    before = (stack / "config" / "sonarr" / "config.xml").read_text()
    result = _run_restore(tmp_path, str(snap), "ecm-mcp")
    assert result.returncode == 0, result.stderr
    assert (stack / "config" / "sonarr" / "config.xml").read_text() == before
    assert not (stack / "config" / "ecm-mcp").exists()


def test_per_app_restore_extracts_that_app_config_only(tmp_path):
    stack = _prepare_stack(tmp_path)
    snap_result = _run_backup(tmp_path, "sonarr")
    assert snap_result.returncode == 0, snap_result.stderr
    snap = pathlib.Path(snap_result.stdout.strip().splitlines()[-1])
    (stack / "config" / "sonarr" / "config.xml").write_text("changed")
    (stack / "config" / "radarr" / "config.xml").write_text("radarr-changed")
    result = _run_restore(tmp_path, str(snap), "sonarr")
    assert result.returncode == 0, result.stderr
    assert (stack / "config" / "sonarr" / "config.xml").read_text() == "sonarr-cfg"
    assert (stack / "config" / "radarr" / "config.xml").read_text() == "radarr-changed"
