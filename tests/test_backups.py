"""Backup listing helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import backups  # noqa: E402


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


def test_backup_script_keeps_three_snapshots():
    script = (ROOT / "scripts" / "backup.sh").read_text()
    assert "tail -n +4" in script
    assert "Keep the last 3" in script or "keep 3" in script.lower()


def test_backup_api_prunes_after_success():
    main = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
    backup_fn = main.split('@app.post("/api/backup")', 1)[1].split("@app.post(", 1)[0]
    assert "prune_old_snapshots" in backup_fn
    list_fn = main.split('@app.get("/api/backups")', 1)[1].split("@app.post(", 1)[0]
    assert "prune_old_snapshots" in list_fn
