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
