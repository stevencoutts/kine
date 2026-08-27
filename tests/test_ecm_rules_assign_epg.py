"""Append Jesmann assign_epg actions to ECM rules."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "ecm-rules-assign-epg.py"
    spec = importlib.util.spec_from_file_location("ecm_rules_assign_epg", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_epg_id_us_vs_uk():
    mod = _load()
    assert mod.epg_id_for_rule_name("US: HBO", uk_id=2, us_id=4) == 4
    assert mod.epg_id_for_rule_name("BBC 1", uk_id=2, us_id=4) == 2
    assert mod.epg_id_for_rule_name("PK: News One", uk_id=2, us_id=4) == 2


def test_ensure_appends_after_create():
    mod = _load()
    out = mod.ensure_assign_epg(
        [{"type": "create_channel", "name_template": "BBC 1", "if_exists": "merge", "group_id": 1}],
        epg_id=2,
    )
    assert out is not None
    assert out[-1] == {"type": "assign_epg", "epg_id": 2, "set_tvg_id": True}


def test_ensure_noop_when_present():
    mod = _load()
    actions = [
        {"type": "create_channel", "name_template": "BBC 1", "if_exists": "merge", "group_id": 1},
        {"type": "assign_epg", "epg_id": 2, "set_tvg_id": True},
    ]
    assert mod.ensure_assign_epg(actions, epg_id=2) is None


def test_ensure_updates_wrong_epg_id():
    mod = _load()
    actions = [
        {"type": "create_channel", "name_template": "US: HBO", "if_exists": "merge", "group_id": 1},
        {"type": "assign_epg", "epg_id": 2, "set_tvg_id": True},
    ]
    out = mod.ensure_assign_epg(actions, epg_id=4)
    assert out[-1]["epg_id"] == 4
