"""Stamp create_channel actions with channel_number from a name map."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "ecm-rules-stamp-channel-numbers.py"
    spec = importlib.util.spec_from_file_location("ecm_rules_stamp_channel_numbers", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_stamp_sets_channel_number():
    mod = _load()
    new, notes = mod.stamp_actions(
        [
            {
                "type": "create_channel",
                "name_template": "BBC 1",
                "if_exists": "merge",
                "group_id": 1,
            },
            {"type": "assign_epg", "epg_id": 2},
        ],
        {"bbc 1": 101.0},
    )
    assert new is not None
    assert new[0]["channel_number"] == 101
    assert new[1] == {"type": "assign_epg", "epg_id": 2}
    assert notes == []


def test_stamp_noop_when_already_set():
    mod = _load()
    new, _ = mod.stamp_actions(
        [
            {
                "type": "create_channel",
                "name_template": "BBC 1",
                "channel_number": 101,
            }
        ],
        {"bbc 1": 101.0},
    )
    assert new is None


def test_stamp_missing_kore_number():
    mod = _load()
    new, notes = mod.stamp_actions(
        [{"type": "create_channel", "name_template": "Mystery"}],
        {"bbc 1": 101.0},
    )
    assert new is None
    assert notes and "no kore number" in notes[0]
