"""Exporter rendering and parsing, with no network anywhere."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))

from app import metrics  # noqa: E402


def test_render_emits_help_and_type_once_per_metric():
    out = metrics.render([
        metrics.Sample("kine_app_up", {"app": "sonarr"}, 1),
        metrics.Sample("kine_app_up", {"app": "radarr"}, 0),
    ])
    assert out.count("# TYPE kine_app_up gauge") == 1
    assert out.count("# HELP kine_app_up") == 1
    assert 'kine_app_up{app="sonarr"} 1' in out
    assert 'kine_app_up{app="radarr"} 0' in out
    assert out.endswith("\n")


def test_render_escapes_label_values():
    out = metrics.render([
        metrics.Sample("kine_streams_active", {"user": 'a"b\\c'}, 1),
    ])
    assert 'user="a\\"b\\\\c"' in out


def test_render_writes_bare_name_when_unlabelled():
    out = metrics.render([metrics.Sample("kine_app_up", {}, 1)])
    assert "kine_app_up 1" in out


def test_render_marks_counters_as_counters():
    out = metrics.render([
        metrics.Sample("kine_collect_errors_total", {"app": "sonarr"}, 3),
    ])
    assert "# TYPE kine_collect_errors_total counter" in out


def test_every_declared_metric_is_namespaced_and_documented():
    for name in metrics.METRIC_TYPES:
        assert name.startswith("kine_")
        assert metrics.METRIC_HELP.get(name)
