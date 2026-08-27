"""Plan helpers for kore→kine Dispatcharr channel sync."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "sync-channels-from-kore-dispatcharr.py"
    spec = importlib.util.spec_from_file_location("sync_channels_from_kore", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_plan_renumber_and_create():
    mod = _load()
    plan = mod.plan_actions(
        [
            {"name": "BBC 1", "channel_number": 101},
            {"name": "bbc 1", "channel_number": 101},  # still counts as renumber slot
            {"name": "92 News", "channel_number": 700},
        ],
        {"BBC 1", "ITV 1"},
    )
    assert "BBC 1" in plan["renumber"]
    assert "bbc 1" in plan["renumber"]
    assert plan["create"] == ["92 News"]
