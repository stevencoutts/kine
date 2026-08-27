"""Plan helpers for kore→kine Dispatcharr logo sync."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "sync-logos-from-kore-dispatcharr.py"
    spec = importlib.util.spec_from_file_location("sync_logos_from_kore", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_plan_logo_sync_counts_linkable():
    mod = _load()
    plan = mod.plan_logo_sync(
        {
            "logos": [{"name": "a", "url": "/data/logos/a.png"}],
            "channels": [
                {"name": "BBC 1", "logo_name": "a", "logo_url": "/data/logos/a.png"},
                {"name": "Missing", "logo_name": "a", "logo_url": "/data/logos/a.png"},
            ],
        },
        {"BBC 1", "ITV 1"},
    )
    assert plan["logos"] == 1
    assert plan["channel_links"] == 2
    assert plan["linkable_on_kine"] == 1
