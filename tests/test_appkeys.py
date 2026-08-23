"""Key resolution prefers what is on disk over what is derived."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import appkeys  # noqa: E402


def test_arr_key_reads_the_key_on_disk(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "sonarr"
    cfg.mkdir(parents=True)
    (cfg / "config.xml").write_text(
        '<?xml version="1.0"?><Config><ApiKey>ondisk123</ApiKey></Config>'
    )
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    assert appkeys.arr_key("sonarr") == "ondisk123"


def test_arr_key_derives_from_secret_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    monkeypatch.setenv("KINE_SECRET", "s3cret")
    key = appkeys.arr_key("sonarr")
    assert key and len(key) == 32


def test_bazarr_key_reads_nested_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "bazarr" / "config"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("auth:\n  apikey: bzr456\n")
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    assert appkeys.bazarr_key() == "bzr456"


def test_key_for_dispatches_by_app(tmp_path, monkeypatch):
    monkeypatch.setattr(appkeys, "STACK", tmp_path)
    monkeypatch.setenv("KINE_SECRET", "s3cret")
    assert appkeys.key_for("radarr") == appkeys.arr_key("radarr")
