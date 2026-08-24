"""Dispatcharr Emby tuner + token env helpers."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "provision"))

from recipes.dispatcharr import (  # noqa: E402
    DISPATCHARR_HDHR,
    tuner_already_linked,
    tuner_host_payload,
)


def test_tuner_host_payload_shape():
    body = tuner_host_payload()
    assert body["Type"] == "hdhomerun"
    assert body["Url"] == DISPATCHARR_HDHR
    assert body["FriendlyName"] == "Dispatcharr"
    assert body["ImportFavoritesOnly"] is False


def test_tuner_already_linked_matches_url():
    assert tuner_already_linked([
        {"Url": "http://other:5004", "Type": "hdhomerun"},
    ]) is False
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR, "Type": "hdhomerun"},
    ]) is True
    assert tuner_already_linked([
        {"Url": DISPATCHARR_HDHR + "/", "Type": "hdhomerun"},
    ]) is True
