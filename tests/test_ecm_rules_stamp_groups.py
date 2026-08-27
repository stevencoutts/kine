"""Stamp ECM rules with channel group ids from a kore name→group map."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "ecm-rules-stamp-groups.py"
    spec = importlib.util.spec_from_file_location("ecm_rules_stamp_groups", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_stamp_rule_groups_updates_both():
    mod = _load()
    body = mod.stamp_rule_groups(
        {
            "target_group_id": 1,
            "actions": [
                {
                    "type": "create_channel",
                    "name_template": "BBC 1",
                    "group_id": 1,
                    "channel_number": 101,
                },
                {"type": "assign_epg", "epg_id": 2},
            ],
        },
        group_id=6,
    )
    assert body == {
        "target_group_id": 6,
        "actions": [
            {
                "type": "create_channel",
                "name_template": "BBC 1",
                "group_id": 6,
                "channel_number": 101,
            },
            {"type": "assign_epg", "epg_id": 2},
        ],
    }


def test_stamp_rule_groups_noop():
    mod = _load()
    body = mod.stamp_rule_groups(
        {
            "target_group_id": 6,
            "actions": [{"type": "create_channel", "group_id": 6}],
        },
        group_id=6,
    )
    assert body is None


def test_resolve_group_for_rule():
    mod = _load()
    by_cf = {
        "bbc 1": {"name": "BBC 1", "group": "UK | Sky"},
        "hum sitaray": {"name": "Hum Sitaray", "group": "ZZ | PAK | General"},
    }
    assert (
        mod.resolve_group_for_rule(
            {
                "name": "BBC 1",
                "actions": [{"type": "create_channel", "name_template": "BBC 1"}],
            },
            by_cf,
        )
        == "UK | Sky"
    )
    assert (
        mod.resolve_group_for_rule(
            {
                "name": "PK: Hum Sitaray",
                "actions": [{"type": "create_channel", "name_template": "Hum Sitaray"}],
            },
            by_cf,
        )
        == "ZZ | PAK | General"
    )
