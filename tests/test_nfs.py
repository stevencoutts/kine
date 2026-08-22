"""NFS integration invariants.

The mounts belong to the host, not the Helm container.  These tests
guard the boundary because a container-side ``mount`` attempt looks
successful at the API layer while changing nothing on the host.
"""
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = (ROOT / "helm" / "backend" / "app" / "main.py").read_text()
INSTALL = (ROOT / "install.sh").read_text()
MOUNT_SCRIPT = (ROOT / "scripts" / "mount-media.sh").read_text()


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
