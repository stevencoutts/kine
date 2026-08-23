"""Invariants that keep this appliance from quietly breaking.

Every one of these encodes a failure that is silent at build time and
expensive at runtime: a port collision inside the shared VPN namespace,
a tunnelled app that still carries its own ports, an app in the
catalogue with no fragment behind it, or a Traefik router with no
service. Run them before every commit:

    python -m pytest tests -q
"""
import ast
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOGUE = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]
FRAGMENTS = sorted((ROOT / "compose").glob("*.yml"))
HELM_MAIN = ROOT / "helm" / "backend" / "app" / "main.py"
VPN_LEAKTEST = ROOT / "scripts" / "vpn-leaktest.sh"


def fragments():
    out = {}
    for f in FRAGMENTS:
        if f.name.startswith("_"):
            continue
        data = yaml.safe_load(f.read_text()) or {}
        for name, svc in (data.get("services") or {}).items():
            out[name] = (f.name, svc)
    return out


SERVICES = fragments()
TUNNELLED = {k for k, v in CATALOGUE.items() if v.get("tunnelled") == "forced"}


# ── structure ───────────────────────────────────────────────────
def test_compose_project_name_is_stable_across_working_directories():
    top = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert top.get("name") == "kine"


def test_fresh_profiles_contain_no_visible_apps():
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    fresh = {p for p in env["COMPOSE_PROFILES"].split(",") if p}
    visible = {
        app_id for app_id, meta in CATALOGUE.items()
        if not meta.get("hidden") and not meta.get("mandatory")
    }
    assert not fresh & visible
    assert fresh == {"mdns"}
    assert env.get("APP_DEV_CHANNELS", "") == ""


def test_shell_env_loader_does_not_expand_password_hash(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'HELM_ADMIN_HASH=$argon2id$v=19$m=65536,t=3,p=4$hash\n'
        'VPN_SERVER_COUNTRIES="United Kingdom"\n'
    )
    result = subprocess.run(
        [
            "bash", "-c",
            'source scripts/lib.sh; load_env "$1"; '
            'printf "%s\\n%s\\n" "$HELM_ADMIN_HASH" "$VPN_SERVER_COUNTRIES"',
            "bash", str(env_file),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "$argon2id$v=19$m=65536,t=3,p=4$hash",
        "United Kingdom",
    ]


def test_merge_missing_env_keys_fills_gaps_without_clobbering(tmp_path):
    example = tmp_path / ".env.example"
    env_file = tmp_path / ".env"
    example.write_text(
        "STACK_ROOT=/srv/kine\n"
        "DATA_ROOT=/srv/media-data\n"
        "CUSTOM=keep-me\n"
    )
    env_file.write_text("CUSTOM=already-set\nDATA_ROOT=/custom/data\n")
    result = subprocess.run(
        [
            "bash", "-c",
            'source scripts/lib.sh; merge_missing_env_keys "$1" "$2"; cat "$1"',
            "bash", str(env_file), str(example),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "CUSTOM=already-set" in text
    assert "DATA_ROOT=/custom/data" in text
    assert "STACK_ROOT=/srv/kine" in text
    assert text.count("CUSTOM=") == 1


def test_ensure_traefik_ports_rewrites_busy_defaults(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TRAEFIK_HTTP_PORT=8080\nTRAEFIK_HTTPS_PORT=8443\n")
    script = r'''
source scripts/lib.sh
port_busy() {
  case "$1" in
    8080|8443) return 0 ;;
    *) return 1 ;;
  esac
}
export -f port_busy
export TRAEFIK_HTTP_PORT=8080 TRAEFIK_HTTPS_PORT=8443
ensure_traefik_ports "$1"
echo "rc:$?"
echo "http:$TRAEFIK_HTTP_PORT"
echo "https:$TRAEFIK_HTTPS_PORT"
cat "$1"
'''
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(env_file)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "rc:0" in result.stdout
    assert "http:8081" in result.stdout
    assert "https:8444" in result.stdout
    assert "TRAEFIK_HTTP_PORT=8081" in env_file.read_text()
    assert "TRAEFIK_HTTPS_PORT=8444" in env_file.read_text()


def test_leaktest_probes_from_inside_gluetun():
    script = VPN_LEAKTEST.read_text()
    assert "docker exec kine-gluetun wget" in script
    assert "--network container:kine-gluetun" not in script


def test_top_level_includes_every_fragment():
    top = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    included = {pathlib.Path(p).name for p in top["include"]}
    on_disk = {f.name for f in FRAGMENTS if not f.name.startswith("_")}
    assert included == on_disk, f"include drift: {included ^ on_disk}"


@pytest.mark.parametrize("app", sorted(CATALOGUE))
def test_every_catalogued_app_has_a_service(app):
    assert app in SERVICES, f"{app} is in the catalogue with no compose fragment"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_service_declares_its_own_profile(name):
    _, svc = SERVICES[name]
    # Core platform services run unconditionally and carry no profile.
    if name in {"traefik", "helm", "dockerproxy", "provision"}:
        assert "profiles" not in svc
        return
    assert svc.get("profiles") == [name.replace("vpn-portsync", "gluetun")] or \
        svc.get("profiles") == ["gluetun"], \
        f"{name} must declare profiles: [{name}]"


@pytest.mark.parametrize("app", sorted(CATALOGUE))
def test_requires_point_at_real_apps(app):
    for dep in CATALOGUE[app].get("requires", []):
        assert dep in CATALOGUE, f"{app} requires unknown app {dep}"


# ── the VPN namespace ───────────────────────────────────────────
@pytest.mark.parametrize("app", sorted(TUNNELLED))
def test_tunnelled_apps_join_the_namespace(app):
    _, svc = SERVICES[app]
    assert svc.get("network_mode") == "service:gluetun", \
        f"{app} is marked tunnelled but does not join gluetun's namespace"


@pytest.mark.parametrize("app", sorted(TUNNELLED))
def test_tunnelled_apps_own_nothing_networkish(app):
    """A tunnelled app with its own ports, networks or labels is a leak.

    Compose does not reject these; it ignores them, so the app looks
    configured and silently is not.
    """
    fname, svc = SERVICES[app]
    for forbidden in ("ports", "networks"):
        assert forbidden not in svc, \
            f"{app} ({fname}) declares {forbidden}, which is ignored inside a shared namespace"
    labels = svc.get("labels") or []
    assert not any("traefik" in str(l) for l in labels), \
        f"{app} carries Traefik labels; they belong on the gluetun service"


@pytest.mark.parametrize("app", sorted(TUNNELLED))
def test_tunnelled_apps_wait_for_a_healthy_tunnel(app):
    _, svc = SERVICES[app]
    dep = svc.get("depends_on") or {}
    assert "gluetun" in dep, f"{app} does not depend on gluetun"
    assert dep["gluetun"].get("condition") == "service_healthy", \
        f"{app} starts before the tunnel is up, so its first requests leak"


def test_no_port_collisions_inside_the_tunnel():
    """One namespace is one port space. A clash is a container that
    will not start, discovered at 2am rather than at commit time."""
    _, gluetun = SERVICES["gluetun"]
    seen: dict[int, str] = {}
    for label in gluetun.get("labels", []):
        m = re.match(r"traefik\.http\.services\.([\w-]+)\.loadbalancer\.server\.port=(\d+)",
                     str(label))
        if not m:
            continue
        app, port = m.group(1), int(m.group(2))
        assert port not in seen, \
            f"port {port} claimed by both {seen[port]} and {app}"
        seen[port] = app
    assert 8000 not in seen, "8000 is gluetun's control server"
    assert seen, "no tunnelled services routed"


def test_every_tunnelled_app_is_reachable():
    """A tunnelled app with no router on gluetun is unreachable, with
    no error anywhere to tell you so."""
    _, gluetun = SERVICES["gluetun"]
    labels = " ".join(str(l) for l in gluetun.get("labels", []))
    for app in TUNNELLED:
        if not CATALOGUE[app].get("subdomain"):
            continue  # headless: unpackerr, recyclarr
        assert f"routers.{app}.rule" in labels, \
            f"{app} has no Traefik router on the gluetun service"
        assert f"services.{app}.loadbalancer" in labels, \
            f"{app} has a router but no service definition"


def test_vpn_restart_list_matches_tunnelled_catalogue():
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    assert set(env["VPN_TUNNELLED_APPS"].split(",")) == TUNNELLED


def test_jackett_uses_maintained_image_without_unused_downloads_mount():
    _, jackett = SERVICES["jackett"]
    assert jackett["image"] == "lscr.io/linuxserver/jackett:${JACKETT_TAG}"
    assert jackett["volumes"] == ["${STACK_ROOT}/config/jackett:/config"]
    _, gluetun = SERVICES["gluetun"]
    labels = " ".join(str(label) for label in gluetun["labels"])
    assert "services.jackett.loadbalancer.server.port=9117" in labels


def test_seerr_is_untunnelled_with_official_image_and_traefik():
    _, seerr = SERVICES["seerr"]
    assert seerr["image"] == "ghcr.io/seerr-team/seerr:${SEERR_TAG}"
    assert seerr.get("init") is True
    assert seerr.get("network_mode") != "service:gluetun"
    assert "kine_internal" in seerr.get("networks", [])
    assert seerr["volumes"] == ["${STACK_ROOT}/config/seerr:/app/config"]
    labels = " ".join(str(label) for label in seerr.get("labels", []))
    assert "routers.seerr.rule" in labels
    assert "services.seerr.loadbalancer.server.port=5055" in labels
    assert "seerr" not in TUNNELLED


def test_tdarr_is_untunnelled_with_media_and_cache_mounts():
    _, tdarr = SERVICES["tdarr"]
    assert tdarr["image"] == "ghcr.io/haveagitgat/tdarr:${TDARR_TAG}"
    assert tdarr.get("network_mode") != "service:gluetun"
    assert "kine_internal" in tdarr.get("networks", [])
    vols = tdarr.get("volumes", [])
    assert "${DATA_ROOT}/media:/media" in vols
    assert "${DATA_ROOT}/cache/tdarr:/temp" in vols
    labels = " ".join(str(label) for label in tdarr.get("labels", []))
    assert "routers.tdarr.rule" in labels
    assert "services.tdarr.loadbalancer.server.port=8265" in labels
    assert "tdarr" not in TUNNELLED
    assert CATALOGUE["tdarr"]["default"] is True
    assert CATALOGUE["tdarr"]["tier"] == "process"

def test_recyclarr_image_uses_published_tag():
    """GHCR publishes major-version tags (8), not latest."""
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    assert env["RECYCLARR_TAG"] == "8"


def test_recyclarr_is_tunnelled_with_config_healthcheck():
    _, recyclarr = SERVICES["recyclarr"]
    assert recyclarr.get("network_mode") == "service:gluetun"
    assert recyclarr["volumes"] == ["${STACK_ROOT}/config/recyclarr:/config"]
    health = recyclarr.get("healthcheck", {})
    assert "recyclarr.yml" in str(health.get("test", ""))


# ── everything else ─────────────────────────────────────────────
def test_untunnelled_web_apps_have_routers():
    for app, meta in CATALOGUE.items():
        if app in TUNNELLED or not meta.get("subdomain") or meta.get("hidden"):
            continue
        _, svc = SERVICES[app]
        labels = " ".join(str(l) for l in svc.get("labels", []))
        assert f"routers.{app}.rule" in labels, f"{app} has no Traefik router"


def test_web_apps_have_local_traefik_aliases():
    _, gluetun = SERVICES["gluetun"]
    for app, meta in CATALOGUE.items():
        if not meta.get("subdomain") or meta.get("hidden"):
            continue
        svc = gluetun if app in TUNNELLED else SERVICES[app][1]
        labels = " ".join(str(label) for label in svc.get("labels", []))
        assert f"{meta['subdomain']}.${{KINE_LOCAL_DOMAIN}}" in labels


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_images_are_pinned_through_env(name):
    _, svc = SERVICES[name]
    image = svc.get("image")
    if not image or "local" in image:
        return  # built here
    assert "${" in image, f"{name} pins an image without an env tag: {image}"


def test_env_example_defines_every_tag_referenced():
    env = (ROOT / ".env.example").read_text()
    declared = set(re.findall(r"^([A-Z0-9_]+)=", env, re.M))
    for name, (fname, svc) in SERVICES.items():
        for var in re.findall(r"\$\{([A-Z0-9_]+)\}", str(svc.get("image", ""))):
            assert var in declared, f"{fname}: {var} is used but not in .env.example"


def test_traefik_supports_modern_docker_api_negotiation():
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    version = tuple(int(part) for part in env["TRAEFIK_TAG"].lstrip("v").split("."))
    # v3.6.0+ lists all containers; v3.6.14+ logs vanished-container
    # inspect races at DEBUG instead of spamming WRN on recreate.
    assert version >= (3, 6, 14)


def test_arr_apps_get_one_data_mount_not_two():
    """Split media and downloads mounts cost hardlinks silently: the
    import still succeeds, it is just a full copy every time."""
    for app in ("sonarr", "radarr", "transmission"):
        _, svc = SERVICES[app]
        targets = [v.split(":")[1] for v in svc["volumes"] if ":" in v]
        data = [t for t in targets if t.startswith("/data")]
        assert data == ["/data"], \
            f"{app} mounts {data}; it must be a single /data mount"


def test_helm_never_touches_the_raw_docker_socket():
    _, helm = SERVICES["helm"]
    mounts = " ".join(helm.get("volumes", []))
    assert "docker.sock" not in mounts, \
        "Helm must reach Docker through the socket proxy, not the raw socket"
    assert helm["environment"]["DOCKER_HOST"].startswith("tcp://dockerproxy")


def test_traefik_uses_socket_proxy_for_docker_discovery():
    _, traefik = SERVICES["traefik"]
    mounts = " ".join(traefik.get("volumes", []))
    assert "docker.sock" not in mounts
    assert "--providers.docker.endpoint=tcp://dockerproxy:2375" in traefik["command"]
    assert "kine_ctrl" in traefik["networks"]
    assert "dockerproxy" in traefik["depends_on"]
    _, proxy = SERVICES["dockerproxy"]
    # Traefik needs list+inspect+events through the proxy; denying
    # either makes discovery silent or forces a raw-socket mount.
    assert str(proxy["environment"]["EVENTS"]) == "1"
    assert str(proxy["environment"]["CONTAINERS"]) == "1"


def test_traefik_publishes_configurable_host_ports():
    _, traefik = SERVICES["traefik"]
    ports = traefik.get("ports", [])
    assert "${TRAEFIK_HTTP_PORT:-8080}:${TRAEFIK_HTTP_PORT:-8080}" in ports
    assert "${TRAEFIK_HTTPS_PORT:-8443}:${TRAEFIK_HTTPS_PORT:-8443}" in ports
    assert "80:80" not in ports
    assert "443:443" not in ports
    cmd = " ".join(traefik["command"])
    assert "--entrypoints.web.address=:${TRAEFIK_HTTP_PORT:-8080}" in cmd
    assert "--entrypoints.websecure.address=:${TRAEFIK_HTTPS_PORT:-8443}" in cmd
    env = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key] = value
    assert env["TRAEFIK_HTTP_PORT"] == "8080"
    assert env["TRAEFIK_HTTPS_PORT"] == "8443"


def test_helm_mounts_repository_root():
    """Include-relative paths must mount the repo, not compose/."""
    _, helm = SERVICES["helm"]
    assert "../:/repo" in helm.get("volumes", [])


def test_helm_can_resolve_host_absolute_env_files():
    """Compose validates host-absolute env_file paths inside Helm."""
    _, helm = SERVICES["helm"]
    assert "${STACK_ROOT}:${STACK_ROOT}" in helm.get("volumes", [])


def test_vpn_portsync_mounts_the_real_script():
    _, portsync = SERVICES["vpn-portsync"]
    assert "../scripts/vpn-portsync.sh:/portsync.sh:ro" in portsync.get("volumes", [])


def test_first_run_starts_newly_enabled_profiles():
    tree = ast.parse(HELM_MAIN.read_text())
    first_run = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "first_run"
    )
    compose_calls = [
        tuple(arg.value for arg in node.args if isinstance(arg, ast.Constant))
        for node in ast.walk(first_run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "compose"
        and node.func.attr == "run"
    ]
    assert ("up", "-d", "--force-recreate", "gluetun") in compose_calls

    password_write = next(
        node.lineno for node in ast.walk(first_run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "auth"
        and node.func.attr == "set_password"
    )
    gluetun_start = next(
        node.lineno for node in ast.walk(first_run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "compose"
        and node.func.attr == "run"
    )
    assert password_write > gluetun_start


def test_socket_proxy_denies_the_dangerous_endpoints():
    _, proxy = SERVICES["dockerproxy"]
    env = proxy["environment"]
    for denied in ("SECRETS", "SWARM", "NODES", "SERVICES", "CONFIGS"):
        assert str(env[denied]) == "0", f"socket proxy allows {denied}"
