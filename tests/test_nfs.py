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
MOUNT_OPTS = (ROOT / "scripts" / "nfs-mount-opts.sh").read_text()
DOCKERFILE = (ROOT / "helm" / "Dockerfile").read_text()
AGENT = (ROOT / "scripts" / "nfs-browse-agent.py").read_text()
MOUNT_SCRIPT = (ROOT / "scripts" / "mount-media.sh").read_text()
FRONTEND = (ROOT / "helm" / "frontend" / "index.html").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()


SAMPLE_SHOWMOUNT = """\
Export list for 192.168.1.10:
/exports/media *
/exports/downloads 192.168.0.0/24
/exports/cache
"""


def test_helm_applies_nfs_mounts_via_agent():
    assert "apply_mounts_via_agent" in MAIN
    assert "library_rescan.after_nfs_mount" in MAIN
    assert "nfs_mount" in MAIN
    assert 'compose.script("mount-media.sh")' not in MAIN


def test_helm_recreates_media_apps_after_nfs_mount():
    assert "_recreate_media_volume_apps" in MAIN
    assert "_ensure_nfs_mounted" in MAIN
    assert "_nfs_configured" in MAIN
    assert "await _ensure_nfs_mounted()" in MAIN
    assert "await _recreate_media_volume_apps()" in MAIN


def test_enable_tier_mounts_nfs_only_for_media_apps():
    """Metrics enable must not remount NFS — that stops Sonarr/Radarr and
    never restarts them, then wire hangs on dead *arr APIs."""
    tier = MAIN.split('@app.post("/api/tiers/{tier}/enable")', 1)[1]
    tier = tier.split("@app.post(", 1)[0]
    assert "needs_media_nfs" in tier
    assert "_MEDIA_VOLUME_APPS" in tier
    assert "await _ensure_nfs_mounted()" in tier
    assert "await _recreate_media_volume_apps()" in tier
    assert tier.index("needs_media_nfs") < tier.index("_ensure_nfs_mounted")
    assert tier.index('up", "-d", *defaults') < tier.index(
        "_recreate_media_volume_apps"
    )


def test_app_enable_restarts_media_peers_after_nfs_remount():
    enable = MAIN.split('@app.post("/api/apps/{app_id}/enable")', 1)[1]
    enable = enable.split("@app.post(", 1)[0]
    assert "remounted" in enable
    assert "await _recreate_media_volume_apps()" in enable
    assert enable.index("_start_app") < enable.index("_recreate_media_volume_apps")


def test_mount_script_supports_host_root_for_agent():
    assert "KINE_HOST_ROOT" in MOUNT_SCRIPT
    assert 'host_path /etc/fstab' in MOUNT_SCRIPT


def test_mount_script_links_title_case_media_dirs():
    assert "link_media_subdir tv TV" in MOUNT_SCRIPT
    assert "removed empty placeholder" in MOUNT_SCRIPT


def test_installer_mounts_after_local_ownership_is_set():
    mount = INSTALL.index("./scripts/mount-media.sh")
    assert mount > INSTALL.index('chmod -R g+rwX "${DATA_ROOT}"')
    assert 'chown -R 1000:1000 "${STACK_ROOT}/config/seerr"' in INSTALL
    assert "seerr/logs" in INSTALL


def test_mount_script_is_self_contained():
    for helper in ("bold", "warn", "die", "ok"):
        assert f"{helper}()" in MOUNT_SCRIPT


def test_nfs_mounts_are_persisted_for_reboot():
    assert "# BEGIN kine-nfs" in MOUNT_SCRIPT
    assert "/etc/fstab" in MOUNT_SCRIPT


def test_changed_export_replaces_existing_mount():
    assert 'findmnt -n -o SOURCE -M "$mount_point"' in MOUNT_SCRIPT
    assert 'umount "$mount_point"' in MOUNT_SCRIPT


def test_mount_script_includes_tdarr_cache():
    assert 'fstab_line "${DATA_ROOT}/media" "${NFS_MEDIA:-}"' in MOUNT_SCRIPT
    assert 'fstab_line "${DATA_ROOT}/cache/tdarr" "${NFS_CACHE:-}"' in MOUNT_SCRIPT
    assert 'mount_export "$CACHE_ROOT" "${NFS_CACHE:-}" "Tdarr cache"' in MOUNT_SCRIPT


def test_mount_script_bind_mounts_downloads_under_media():
    assert "downloads_under_media" in MOUNT_SCRIPT
    assert "mount --bind" in MOUNT_SCRIPT
    assert "mount_downloads" in MOUNT_SCRIPT


def test_mount_script_uses_shared_nfs_options():
    assert "nfs-mount-opts.sh" in MOUNT_SCRIPT
    assert "nolock,intr,tcp,actimeo=1800" in MOUNT_OPTS
    assert 'mount -t nfs -o "$KINE_NFS_FSTAB_OPTS"' in MOUNT_SCRIPT


def test_env_example_documents_nfs_media():
    assert "NFS_MEDIA=" in ENV_EXAMPLE
    assert "NFS_CACHE=" in ENV_EXAMPLE


def test_settings_api_includes_nfs_cache():
    assert "NFS_CACHE" in MAIN
    assert "NFS_MEDIA" in MAIN
    assert "/api/nfs/exports" in MAIN
    assert "/api/nfs/browse" in MAIN
    assert "/api/nfs/apply" in MAIN


def test_nfs_agent_can_apply_mounts_on_host():
    text = (ROOT / "compose" / "core.nfs-agent.yml").read_text()
    assert "network_mode: host" in text
    assert "nfs-browse-agent" in text
    assert "privileged: true" in text
    assert "pid: host" in text
    assert "/:/host" in text
    assert "KINE_HOST_ROOT" in text
    assert "/apply-mounts" in AGENT
    assert "mount-media.sh" in AGENT


def test_helm_container_can_mount_for_browse():
    assert "SYS_ADMIN" in (ROOT / "compose" / "core.helm.yml").read_text()


def test_helm_image_includes_showmount():
    assert "nfs-common" in DOCKERFILE


def test_frontend_can_browse_and_pick_exports():
    assert "load-nfs-shares" in FRONTEND
    assert "/nfs/exports" in FRONTEND
    assert "loadMediaSubfolders" in FRONTEND
    assert "/nfs/browse" in FRONTEND
    assert "mediaSubfolders" in FRONTEND
    assert "data-nfs-select" in FRONTEND
    assert "NFS_CACHE" in FRONTEND
    assert "mount-media.sh" not in FRONTEND
    assert "browse-nfs" not in FRONTEND
    assert "explore only" not in FRONTEND


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


def test_filter_pickable_exports_prefers_var_nfs_shared():
    exports = [
        "/volume/x/.srv/.unifi-drive/media/.data",
        "/var/nfs/shared/media",
        "/var/nfs/shared/cache",
        "/var/nfs/shared/Downloads",
    ]
    pickable = nfs_exports.filter_pickable_exports(exports)
    assert pickable == sorted([
        "/var/nfs/shared/media",
        "/var/nfs/shared/cache",
        "/var/nfs/shared/Downloads",
    ])


def test_filter_pickable_exports_infers_unifi_shared_paths():
    exports = [
        "/volume/x/.srv/.unifi-drive/media/.data",
        "/volume/x/.srv/.unifi-drive/Downloads/.data",
        "/volume/x/.srv/.unifi-drive/cache/.data",
    ]
    assert nfs_exports.filter_pickable_exports(exports) == sorted([
        "/var/nfs/shared/media",
        "/var/nfs/shared/Downloads",
        "/var/nfs/shared/cache",
    ])


def test_suggest_assignments_maps_share_names():
    exports = [
        "/var/nfs/shared/media",
        "/var/nfs/shared/Downloads",
        "/var/nfs/shared/cache",
    ]
    assert nfs_exports.suggest_assignments(exports) == {
        "NFS_MEDIA": "/var/nfs/shared/media",
        # Prefer downloads under media so hardlinks work.
        "NFS_DOWNLOADS": "/var/nfs/shared/media/downloads",
        "NFS_CACHE": "/var/nfs/shared/cache",
    }


def test_suggest_assignments_skips_nested_tv_movies_under_media():
    exports = [
        "/var/nfs/shared/media",
        "/var/nfs/shared/media/TV",
        "/var/nfs/shared/media/Movies",
        "/var/nfs/shared/Downloads",
    ]
    assert nfs_exports.suggest_assignments(exports) == {
        "NFS_MEDIA": "/var/nfs/shared/media",
        "NFS_DOWNLOADS": "/var/nfs/shared/media/downloads",
    }


def test_sort_exports_prefers_var_nfs_shared():
    exports = [
        "/volume/x/.srv/.unifi-drive/media/.data",
        "/var/nfs/shared/media",
        "/var/nfs/shared/cache",
    ]
    sorted_exports = nfs_exports.sort_exports(exports)
    assert sorted_exports[0].startswith("/var/nfs/shared/")
    assert sorted_exports[-1].endswith("/.data")


def test_export_label_unwraps_unifi_data_suffix():
    assert "media" in nfs_exports.export_label(
        "/volume/uuid/.srv/.unifi-drive/media/.data"
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


def test_browse_prefers_host_agent(monkeypatch):
    monkeypatch.setenv("NFS_BROWSE_AGENT", "http://127.0.0.1:8611")
    monkeypatch.setenv("KINE_SECRET", "test-secret")
    monkeypatch.setattr(
        nfs_exports,
        "browse_via_agent",
        lambda server, path="", timeout=15.0: {
            "server": server,
            "path": path,
            "parent": None if not path else "",
            "entries": [{"name": "TV", "path": f"{path}/TV", "kind": "dir"}],
            "via": "host-agent",
        },
    )
    data = nfs_exports.browse("nas.local", "/exports/media")
    assert data["via"] == "host-agent"
    assert data["entries"][0]["name"] == "TV"


def test_mount_error_mentions_host_agent_when_denied():
    msg = nfs_exports._mount_error(
        "10.100.30.222:/exports/media",
        "access denied by server",
        "10.100.100.34",
    )
    assert "bridge IP" in msg
    assert "nfs-browse-agent" in msg



def test_validate_server_rejects_injection():
    with pytest.raises(ValueError):
        nfs_exports.validate_server("evil; rm -rf /")
    with pytest.raises(ValueError):
        nfs_exports.validate_server("")
    assert nfs_exports.validate_server("nas.local") == "nas.local"
