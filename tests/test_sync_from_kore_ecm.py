"""Helpers for kore → kine ECM sync."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "sync-from-kore-ecm.py"
    spec = importlib.util.spec_from_file_location("sync_from_kore_ecm", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_count_rules_in_yaml():
    mod = _load()
    text = "version: 1\nrules:\n- name: BBC 1\n  enabled: true\n- name: ITV\n  enabled: false\n"
    assert mod.count_rules_in_yaml(text) == 2


def test_schedule_create_payload_strips_channel_group_ids():
    mod = _load()
    payload, warnings = mod.schedule_create_payload({
        "name": "Daily Morning",
        "enabled": True,
        "schedule_type": "daily",
        "schedule_time": "00:00",
        "timezone": "Europe/London",
        "days_of_week": [],
        "day_of_month": None,
        "interval_seconds": None,
        "parameters": {"channel_groups": [3882, 3889], "timeout": 15},
        "id": 8,
        "task_id": "stream_probe",
    })
    assert payload["schedule_type"] == "daily"
    assert payload["parameters"] == {"timeout": 15}
    assert "stripped parameters.channel_groups" in warnings
    assert "id" not in payload


def test_schedule_create_rejects_cron_type():
    mod = _load()
    try:
        mod.schedule_create_payload({"schedule_type": "cron", "enabled": True})
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "cron" in str(exc)
