"""Helm scheduler behaviour."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = (ROOT / "helm" / "backend" / "app" / "scheduler.py").read_text()


def test_scheduler_polls_seerr_until_arr_linked():
    assert "_seerr_needs_wire" in SCHEDULER
    assert "wire_seerr_if_ready" in SCHEDULER
    assert "_seerr_wire_loop" in SCHEDULER
    assert 'asyncio.create_task(_seerr_wire_loop())' in SCHEDULER


def test_scheduler_polls_dispatcharr_wire():
    assert "_dispatcharr_needs_wire" in SCHEDULER
    assert "wire_dispatcharr_if_ready" in SCHEDULER
    assert "_dispatcharr_wire_loop" in SCHEDULER
    assert 'asyncio.create_task(_dispatcharr_wire_loop())' in SCHEDULER
    assert "/api/v1/settings/{path}" in SCHEDULER
