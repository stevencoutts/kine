"""Backup listing helpers."""
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


def test_backup_api_prunes_after_success_but_list_does_not():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    backup_fn = main.split('@app.post("/api/backup")', 1)[1].split("@app.post(", 1)[0]
    assert "prune_old_snapshots" in backup_fn
    list_fn = main.split('@app.get("/api/backups")', 1)[1].split("@app.", 1)[0]
    assert "prune_old_snapshots" not in list_fn
    assert '@app.get("/api/backups/{name}/file")' in main
    assert '@app.delete("/api/backups/{name}")' in main
    assert "delete_snapshot" in main


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
