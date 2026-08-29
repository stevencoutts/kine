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
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "helm" / "backend"))
from app import vpn_routing  # noqa: E402

CATALOGUE = yaml.safe_load((ROOT / "catalogue.yml").read_text())["apps"]
FRAGMENTS = sorted((ROOT / "compose").glob("*.yml"))
HELM_MAIN = ROOT / "helm" / "backend" / "app" / "main.py"
VPN_LEAKTEST = ROOT / "scripts" / "vpn-leaktest.sh"


def _generated_primary_tunnel_labels() -> list[str]:
    """Traefik labels Helm used to write onto primary gluetun (for stack tests)."""
    routed = [app for app in vpn_routing.APP_PORTS if app in vpn_routing.APP_TRAEFIK_HOST]
    return vpn_routing._traefik_router_label_lines(
        routed,
        kine_domain="${KINE_DOMAIN}",
        kine_local_domain="${KINE_LOCAL_DOMAIN}",
    )


def fragments():
    out = {}
    for f in FRAGMENTS:
        if f.name.startswith("_"):
            continue
        data = yaml.safe_load(f.read_text()) or {}
        for name, svc in (data.get("services") or {}).items():
            out[name] = (f.name, svc)
    # Static vpn.gluetun.yml no longer carries app routers; merge what the
    # generator places on primary when all forced apps are leftovers.
    if "gluetun" in out:
        fname, svc = out["gluetun"]
        labels = svc.get("labels") or []
        if not any("traefik.http.routers." in str(l) for l in labels):
            merged = dict(svc)
            merged["labels"] = _generated_primary_tunnel_labels()
            out["gluetun"] = (fname, merged)
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
    assert 'docker exec "$container" wget' in script or "docker exec kine-gluetun wget" in script
    assert "kine-gluetun" in script
    assert "--network container:kine-gluetun" not in script


def test_updating_gluetun_recreates_tunnelled_apps():
    """Recreating gluetun alone orphans every network_mode: service:gluetun
    container on the old namespace — Seerr then cannot reach Radarr/Sonarr."""
    script = (ROOT / "scripts" / "updates.sh").read_text()
    assert 'svc" == "gluetun"' in script or "gluetun_*" in script or "gluetun-*" in script
    assert "force-recreate" in script
    assert "service:gluetun" in script
    assert "tunnel_heal" in script or "heal-tunnel" in script
    assert "Healing tunnel orphans" in script


def test_compose_includes_vpn_routing_override():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "compose/vpn-routing.override.yml" in text


def test_static_gluetun_has_no_vpn_routing_app_traefik_routers():
    text = (ROOT / "compose" / "vpn.gluetun.yml").read_text()
    assert "traefik.http.routers.sonarr" not in text
    assert "vpn-routing.override" in text or "generated" in text.lower()


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


def test_dispatcharr_mounts_kine_contrast_css():
    """tv. serves Dispatcharr directly; Helm embed CSS never reaches it."""
    _, svc = SERVICES["dispatcharr"]
    vols = "\n".join(str(v) for v in (svc.get("volumes") or []))
    assert "kine-contrast.css:/app/static/kine-contrast.css" in vols
    assert "kine-contrast.nginx.conf:/etc/nginx/conf.d/kine-contrast.conf" in vols


def test_beets_mounts_kine_import_worker():
    """Lidarr cannot exec beet; a beets-side worker drains the shared queue."""
    _, svc = SERVICES["beets"]
    vols = "\n".join(str(v) for v in (svc.get("volumes") or []))
    assert "kine-import:/custom-services.d/kine-import" in vols


def test_gluetun_forwards_static_vpn_input_ports():
    """Njalla-style ports are not in Gluetun's PF API; the firewall must allow them."""
    _, gluetun = SERVICES["gluetun"]
    env = gluetun.get("environment") or {}
    assert env.get("FIREWALL_VPN_INPUT_PORTS") == "${FIREWALL_VPN_INPUT_PORTS:-}"
    _, portsync = SERVICES["vpn-portsync"]
    ps_env = portsync.get("environment") or {}
    assert ps_env.get("VPN_FORWARDED_PORT") == "${FIREWALL_VPN_INPUT_PORTS:-}"


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
    assert seerr.get("user") == "1000:1000"
    assert seerr.get("network_mode") != "service:gluetun"
    assert "kine_internal" in seerr.get("networks", [])
    assert seerr["volumes"] == ["${STACK_ROOT}/config/seerr:/app/config"]
    labels = " ".join(str(label) for label in seerr.get("labels", []))
    assert "routers.seerr.rule" in labels
    assert "services.seerr.loadbalancer.server.port=5055" in labels
    assert "seerr" not in TUNNELLED


def test_game_thumbs_is_untunnelled_for_teamarr_art():
    """Artwork URLs must be client-reachable via Traefik, not buried in gluetun."""
    _, thumbs = SERVICES["game-thumbs"]
    assert thumbs["image"] == "ghcr.io/sethwv/game-thumbs:${GAME_THUMBS_TAG}"
    assert thumbs.get("network_mode") != "service:gluetun"
    assert "kine_internal" in thumbs.get("networks", [])
    assert "${STACK_ROOT}/config/game-thumbs/cache:/app/.cache" in thumbs.get("volumes", [])
    labels = " ".join(str(label) for label in thumbs.get("labels", []))
    assert "Host(`thumbs.${KINE_DOMAIN}`)" in labels
    assert "services.game-thumbs.loadbalancer.server.port=3000" in labels
    assert "game-thumbs" not in TUNNELLED
    assert "game-thumbs" in CATALOGUE["teamarr"].get("requires", [])


def test_ecm_mcp_is_optional_tunnelled_sidecar():
    _, mcp = SERVICES["ecm-mcp"]
    assert mcp["image"] == "ghcr.io/motwakorb/enhancedchannelmanager-mcp:${ECM_MCP_TAG}"
    assert mcp["network_mode"] == "service:gluetun"
    assert mcp["profiles"] == ["ecm-mcp"]
    assert "ecm-mcp-secrets:/run/secrets/ecm-mcp:ro" in mcp.get("volumes", [])
    assert "${STACK_ROOT}/config/ecm:/config:ro" in mcp.get("volumes", [])
    assert CATALOGUE["ecm-mcp"]["default"] is False
    assert CATALOGUE["ecm-mcp"]["tunnelled"] == "forced"
    assert "ecm" in CATALOGUE["ecm-mcp"]["requires"]
    _, ecm = SERVICES["ecm"]
    assert "ecm-mcp-secrets:/run/secrets/ecm-mcp" in ecm.get("volumes", [])
    assert ecm["environment"].get("MCP_SECRETS_DIR") == "/run/secrets/ecm-mcp"
    assert ecm["environment"].get("MCP_HOST") == "localhost"
    assert vpn_routing.APP_PORTS["ecm-mcp"] == 6101
    assert vpn_routing.APP_TRAEFIK_HOST["ecm-mcp"] == "mcp"


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


def test_recyclarr_runs_on_internal_network_with_config_healthcheck():
    _, recyclarr = SERVICES["recyclarr"]
    assert recyclarr.get("network_mode") != "service:gluetun"
    assert "kine_internal" in recyclarr.get("networks", [])
    assert recyclarr["volumes"] == ["${STACK_ROOT}/config/recyclarr:/config"]
    startup = str(recyclarr.get("command", ""))
    assert "/entrypoint.sh sync" in startup
    health = recyclarr.get("healthcheck", {})
    assert "recyclarr.yml" in str(health.get("test", ""))
    assert "recyclarr" not in TUNNELLED


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


def test_arr_apps_mount_media_and_downloads_directly():
    """Bind media and downloads paths directly so host NFS submounts
    are visible inside containers. Both still sit on one host filesystem."""
    for app in ("sonarr", "radarr", "transmission", "prowlarr", "bazarr"):
        _, svc = SERVICES[app]
        vols = svc["volumes"]
        assert "${DATA_ROOT}/media:/data/media" in vols, app
        assert "${DATA_ROOT}/downloads:/data/downloads" in vols, app
        assert "${DATA_ROOT}:/data" not in vols, app


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


def test_traefik_loads_acme_args_and_provider_env():
    """acme-dns writes CLI flags + ClouDNS creds; Traefik must actually consume them."""
    _, traefik = SERVICES["traefik"]
    entry = traefik.get("entrypoint") or []
    if isinstance(entry, str):
        entry = [entry]
    joined = " ".join(str(x) for x in entry)
    vols = " ".join(traefik.get("volumes", []))
    assert "traefik-entrypoint" in joined or "traefik-entrypoint.sh" in vols
    assert "traefik-entrypoint.sh" in vols
    env_files = traefik.get("env_file") or []
    flat = []
    for item in env_files:
        if isinstance(item, dict):
            flat.append(item.get("path") or "")
        else:
            flat.append(str(item))
    assert any("acme.env" in f for f in flat)
    example = (ROOT / ".env.example").read_text()
    assert "KINE_ACME_DNS_PROVIDER=cloudns" in example
    assert "CLOUDNS_AUTH_ID" in example


def test_tls_setup_acme_dns_writes_resolver_args(tmp_path):
    stack = tmp_path / "stack"
    (stack / "config" / "traefik" / "dynamic").mkdir(parents=True)
    (stack / "config" / "traefik" / "certs").mkdir(parents=True)
    env_file = tmp_path / "proj" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        f"STACK_ROOT={stack}\n"
        "KINE_DOMAIN=example.test\n"
        "KINE_TLS_MODE=acme-dns\n"
        "KINE_ACME_EMAIL=ops@example.test\n"
        "KINE_ACME_DNS_PROVIDER=cloudns\n"
        "KINE_ACME_CA=https://acme-v02.api.letsencrypt.org/directory\n"
    )
    # tls-setup sources lib.sh and load_env .env from cwd
    script = ROOT / "scripts" / "tls-setup.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=env_file.parent,
        text=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    args = (stack / "config" / "traefik" / "acme-args.txt").read_text()
    assert "certificatesresolvers.kineresolver.acme.email=ops@example.test" in args
    assert "dnschallenge.provider=cloudns" in args
    assert "dnschallenge.resolvers=1.1.1.1:53,8.8.8.8:53" in args
    tls = (stack / "config" / "traefik" / "dynamic" / "tls.yml").read_text()
    assert "kineresolver" in tls
    assert "*.example.test" in tls
    acme_env = stack / "config" / "traefik" / "acme.env"
    assert acme_env.is_file()
    assert "CLOUDNS_AUTH_ID" in acme_env.read_text()


def test_tls_setup_internal_clears_acme_args(tmp_path):
    stack = tmp_path / "stack"
    dyn = stack / "config" / "traefik" / "dynamic"
    dyn.mkdir(parents=True)
    (stack / "config" / "traefik" / "certs").mkdir(parents=True)
    stale = stack / "config" / "traefik" / "acme-args.txt"
    stale.write_text("--certificatesresolvers.kineresolver.acme.email=old\n")
    env_file = tmp_path / "proj" / ".env"
    env_file.parent.mkdir()
    env_file.write_text(
        f"STACK_ROOT={stack}\n"
        "KINE_DOMAIN=example.test\n"
        "KINE_TLS_MODE=internal\n"
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "tls-setup.sh")],
        cwd=env_file.parent,
        text=True,
        capture_output=True,
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert not stale.exists() or stale.read_text().strip() == ""


def test_helm_mounts_repository_root():
    """Include-relative paths must mount the repo, not compose/."""
    _, helm = SERVICES["helm"]
    assert "../:/repo" in helm.get("volumes", [])


def test_helm_can_resolve_host_absolute_env_files():
    """Compose validates host-absolute env_file paths inside Helm."""
    _, helm = SERVICES["helm"]
    assert "${STACK_ROOT}:${STACK_ROOT}" in helm.get("volumes", [])


def test_helm_remounts_checkout_so_compose_binds_reach_the_daemon():
    """Relative binds resolve inside Helm then go to dockerd as host paths.

    Without a same-path remount, ``../provision/...`` becomes ``/repo/...``
    on the host and Docker creates empty directories. Dispatcharr's nginx
    then dies because kine-contrast.conf is a directory.
    """
    _, helm = SERVICES["helm"]
    env = helm.get("environment") or {}
    assert env.get("KINE_REPO") == "${KINE_CHECKOUT}"
    assert "${KINE_CHECKOUT}:${KINE_CHECKOUT}" in helm.get("volumes", [])


def test_install_writes_kine_checkout_to_the_host_path():
    text = (ROOT / "install.sh").read_text()
    assert "KINE_CHECKOUT=${REPO}" in text
    example = (ROOT / ".env.example").read_text()
    assert "KINE_CHECKOUT=" in example


def test_helm_mounts_data_root_media_for_status_disk():
    """Status disk usage needs the NFS media bind; parent DATA_ROOT alone hides it."""
    _, helm = SERVICES["helm"]
    vols = helm.get("volumes", [])
    assert "${DATA_ROOT}/media:${DATA_ROOT}/media:ro" in vols


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


# ── metrics tier ────────────────────────────────────────────────
def test_prometheus_stays_off_the_edge_network():
    _, svc = SERVICES["prometheus"]
    assert "kine_edge" not in (svc.get("networks") or [])
    assert not [l for l in (svc.get("labels") or []) if "traefik" in l]


def test_prometheus_retention_is_capped_by_time_and_size():
    _, svc = SERVICES["prometheus"]
    command = " ".join(svc.get("command") or [])
    assert "--storage.tsdb.retention.time=30d" in command
    assert "--storage.tsdb.retention.size=2GB" in command


def test_cadvisor_never_mounts_the_raw_docker_socket():
    _, svc = SERVICES["cadvisor"]
    assert "docker.sock" not in " ".join(svc.get("volumes") or [])
    assert "--docker=tcp://dockerproxy:2375" in (svc.get("command") or [])


def test_grafana_allows_anonymous_viewing_and_embedding():
    _, svc = SERVICES["grafana"]
    env = svc["environment"]
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Viewer"
    assert env["GF_SECURITY_ALLOW_EMBEDDING"] == "true"


def test_only_grafana_is_visible_in_the_metrics_tier():
    visible = [
        app_id for app_id, meta in CATALOGUE.items()
        if meta.get("tier") == "metrics" and not meta.get("hidden")
    ]
    assert visible == ["grafana"]


def test_metrics_apps_are_not_tunnelled():
    for app_id, meta in CATALOGUE.items():
        if meta.get("tier") == "metrics":
            assert meta.get("tunnelled") != "forced"


def test_starting_grafana_pulls_in_the_whole_metrics_chain():
    """Helm runs `up -d` with a tier's visible apps only, so the hidden
    exporters have to arrive as Compose dependencies or they never run."""
    chain, queue = set(), ["grafana"]
    while queue:
        name = queue.pop()
        if name in chain:
            continue
        chain.add(name)
        _, svc = SERVICES[name]
        queue.extend(svc.get("depends_on") or [])
    assert chain == {"grafana", "prometheus", "cadvisor", "node-exporter"}


def test_every_compose_dependency_is_mirrored_in_the_catalogue():
    """A depends_on across profiles makes the whole project invalid when
    the dependency's profile is off, so enabling an app must always
    enable what it depends on. resolve_deps reads `requires`, which is
    therefore not documentation: it is what keeps compose parseable."""
    missing = []
    for name, meta in CATALOGUE.items():
        if name not in SERVICES:
            continue
        _, svc = SERVICES[name]
        requires = set(meta.get("requires") or [])
        for dep in (svc.get("depends_on") or []):
            # Hidden platform services are always running, so they need
            # no catalogue edge to stay enabled.
            if dep not in CATALOGUE:
                continue
            if dep not in requires:
                missing.append(f"{name} depends_on {dep} but does not require it")
    assert not missing, "; ".join(missing)


def test_cadvisor_can_reach_containerd():
    """Without it the docker factory never registers and every
    container metric arrives with no `name` label to group by."""
    volumes = " ".join(SERVICES["cadvisor"][1].get("volumes") or [])
    assert "containerd.sock" in volumes
