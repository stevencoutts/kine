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


# ── everything else ─────────────────────────────────────────────
def test_untunnelled_web_apps_have_routers():
    for app, meta in CATALOGUE.items():
        if app in TUNNELLED or not meta.get("subdomain") or meta.get("hidden"):
            continue
        _, svc = SERVICES[app]
        labels = " ".join(str(l) for l in svc.get("labels", []))
        assert f"routers.{app}.rule" in labels, f"{app} has no Traefik router"


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


def test_arr_apps_get_one_data_mount_not_two():
    """Split media and downloads mounts cost hardlinks silently: the
    import still succeeds, it is just a full copy every time."""
    for app in ("sonarr", "radarr"):
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
    assert ("up", "-d", "--wait", "gluetun") in compose_calls

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
