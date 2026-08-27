"""Convert ECM merge-existing actions to create_channel."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "ecm-rules-create-on-miss.py"
    spec = importlib.util.spec_from_file_location("ecm_rules_create_on_miss", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_convert_name_exact_merge_to_create():
    mod = _load()
    action, warn = mod.convert_action({
        "type": "merge_streams",
        "target": "existing_channel",
        "find_channel_by": "name_exact",
        "find_channel_value": "BBC 1",
        "remove_non_matching": True,
    })
    assert action == {
        "type": "create_channel",
        "name_template": "BBC 1",
        "if_exists": "merge",
    }
    assert warn and "remove_non_matching" in warn


def test_convert_preserves_group_id():
    mod = _load()
    action, _ = mod.convert_action(
        {
            "type": "merge_streams",
            "target": "existing_channel",
            "find_channel_by": "name_exact",
            "find_channel_value": "ITV 1",
        },
        group_id=42,
    )
    assert action["group_id"] == 42


def test_convert_rule_actions_rewrites_only_matching():
    mod = _load()
    new_actions, warnings = mod.convert_rule_actions({
        "name": "BBC 1",
        "target_group_id": None,
        "actions": [
            {
                "type": "merge_streams",
                "target": "existing_channel",
                "find_channel_by": "name_exact",
                "find_channel_value": "BBC 1",
            },
            {"type": "assign_logo", "value": "from_stream"},
        ],
    })
    assert new_actions is not None
    assert new_actions[0]["type"] == "create_channel"
    assert new_actions[1] == {"type": "assign_logo", "value": "from_stream"}
    assert warnings == []


def test_convert_anchored_name_regex():
    mod = _load()
    action, warn = mod.convert_action({
        "type": "merge_streams",
        "target": "existing_channel",
        "find_channel_by": "name_regex",
        "find_channel_value": "^5 STAR$",
        "remove_non_matching": True,
    })
    assert action == {
        "type": "create_channel",
        "name_template": "5 STAR",
        "if_exists": "merge",
    }
    assert warn and "remove_non_matching" in warn


def test_convert_rejects_complex_regex():
    mod = _load()
    action, warn = mod.convert_action({
        "type": "merge_streams",
        "target": "existing_channel",
        "find_channel_by": "name_regex",
        "find_channel_value": "^BBC ?1$",
    })
    assert action is None
    assert warn and "unsupported" in warn
