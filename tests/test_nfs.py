"""NFS export browsing and mount-script invariants."""
import contextlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend" / "app"))

import nfs_exports  # noqa: E402

MAIN = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
INSTALL = (ROOT / "install.sh").read_text()
MOUNT_SCRIPT = (ROOT / "scripts" / "mount-media.sh").read_text()
DOCKERFILE = (ROOT / "helm" / "Dockerfile").read_text()
FRONTEND = (ROOT / "helm" / "frontend" / "index.html").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


SAMPLE_SHOWMOUNT = """\
Export list for 192.168.1.10:
/exports/media *
/exports/downloads 192.168.0.0/24
/exports/cache
"""


def test_helm_only_saves_nfs_settings():
    assert 'compose.script("mount-media.sh")' not in MAIN


def test_installer_mounts_after_local_ownership_is_set():
    mount = INSTALL.index("./scripts/mount-media.sh")
    assert mount > INSTALL.index('chmod -R g+rwX "${DATA_ROOT}"')


def test_mount_script_is_self_contained():
    for helper in ("bold", "warn", "die", "ok"):
        assert f"{helper}()" in MOUNT_SCRIPT


def test_nfs_mounts_are_persisted_for_reboot():
    assert "# BEGIN kine-nfs" in MOUNT_SCRIPT
    assert "/etc/fstab" in MOUNT_SCRIPT


def test_changed_export_replaces_existing_mount():
    assert 'findmnt -n -o SOURCE --target "$mount_point"' in MOUNT_SCRIPT
    assert 'umount "$mount_point"' in MOUNT_SCRIPT


def test_mount_script_includes_tdarr_cache():
    assert 'fstab_line "${DATA_ROOT}/cache/tdarr" "${NFS_CACHE:-}"' in MOUNT_SCRIPT
    assert 'mount_export "${DATA_ROOT}/cache/tdarr" "${NFS_CACHE:-}" "Tdarr cache"' in MOUNT_SCRIPT


def test_env_example_documents_nfs_cache():
    assert "NFS_CACHE=" in ENV_EXAMPLE


def test_settings_api_includes_nfs_cache():
    assert "NFS_CACHE" in MAIN
    assert "/api/nfs/exports" in MAIN
    assert "/api/nfs/browse" in MAIN


def test_helm_container_can_mount_for_browse():
    assert "SYS_ADMIN" in (ROOT / "compose" / "core.helm.yml").read_text()


def test_helm_image_includes_showmount():
    assert "nfs-common" in DOCKERFILE


def test_frontend_can_browse_and_pick_exports():
    assert "browse-nfs" in FRONTEND
    assert "/nfs/browse" in FRONTEND
    assert "nfs-browser-select" in FRONTEND
    assert "NFS_CACHE" in FRONTEND


def test_export_root_for_longest_matching_prefix():
    exports = ["/exports", "/exports/media", "/other"]
    assert nfs_exports.export_root_for("/exports/media/tv", exports) == "/exports/media"
    assert nfs_exports.export_root_for("/exports/media", exports) == "/exports/media"


def test_parent_path_steps_up_within_export():
    exports = ["/exports/media"]
    assert nfs_exports.parent_path("/exports/media/tv", exports) == "/exports/media"
    assert nfs_exports.parent_path("/exports/media", exports) == ""


def test_validate_export_path_rejects_traversal():
    with pytest.raises(ValueError):
        nfs_exports.validate_export_path("/exports/../etc")
    assert nfs_exports.validate_export_path("/exports/media/tv") == "/exports/media/tv"


def test_browse_root_lists_exports_without_mount(monkeypatch):
    monkeypatch.setattr(
        nfs_exports,
        "list_export_rows",
        lambda server, timeout=10.0: [
            ("/exports/media", "*"),
            ("/exports/downloads", "*"),
        ],
    )
    data = nfs_exports.browse("nas.local", "")
    assert data["path"] == ""
    assert data["parent"] is None
    assert [e["path"] for e in data["entries"]] == [
        "/exports/downloads",
        "/exports/media",
    ]
    assert [e["name"] for e in data["entries"]] == ["downloads", "media"]


def test_export_label_unwraps_unifi_data_suffix():
    assert (
        nfs_exports.export_label(
            "/volume/uuid/.srv/.unifi-drive/media/.data"
        )
        == "media"
    )
    assert nfs_exports.export_label("/exports/tv") == "tv"


def test_browse_subfolder_uses_temporary_mount(monkeypatch):
    monkeypatch.setattr(
        nfs_exports,
        "list_export_rows",
        lambda server, timeout=10.0: [("/exports/media", "192.168.1.10")],
    )

    class FakeMount:
        def joinpath(self, *parts):
            return self

        def is_dir(self):
            return True

    @contextlib.contextmanager
    def fake_mount(server, export, clients=""):
        assert server == "nas.local"
        assert export == "/exports/media"
        yield FakeMount()

    class FakeEntry:
        name = "TV"

        def is_dir(self, follow_symlinks=False):
            return True

    monkeypatch.setattr(nfs_exports, "_nfs_mount", fake_mount)
    monkeypatch.setattr(nfs_exports.os, "scandir", lambda path: [FakeEntry()])
    data = nfs_exports.browse("nas.local", "/exports/media")
    assert data["path"] == "/exports/media"
    assert data["parent"] == ""
    assert data["entries"] == [
        {"name": "TV", "path": "/exports/media/TV", "kind": "dir"},
    ]


def test_parse_showmount_extracts_export_paths():
    assert nfs_exports.parse_showmount(SAMPLE_SHOWMOUNT) == [
        "/exports/media",
        "/exports/downloads",
        "/exports/cache",
    ]


def test_validate_server_rejects_injection():
    with pytest.raises(ValueError):
        nfs_exports.validate_server("evil; rm -rf /")
    with pytest.raises(ValueError):
        nfs_exports.validate_server("")
    assert nfs_exports.validate_server("nas.local") == "nas.local"
