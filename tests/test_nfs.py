"""NFS export browsing and mount-script invariants."""
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


def test_helm_image_includes_showmount():
    assert "nfs-common" in DOCKERFILE


def test_frontend_can_browse_and_pick_exports():
    assert "browse-nfs" in FRONTEND
    assert "/nfs/exports" in FRONTEND
    assert "NFS_CACHE" in FRONTEND


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
