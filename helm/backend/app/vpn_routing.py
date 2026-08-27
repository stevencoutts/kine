"""Generate compose override YAML for multi-Gluetun egress routing."""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

from . import vpn_profiles, wireguard

ROUTING_STUB_REL = pathlib.Path("compose/vpn-routing.override.yml")
ROUTING_GENERATED_REL = pathlib.Path("compose/vpn-routing.generated.yml")
# Back-compat alias for tests/docs that refer to the generated path.
ROUTING_REL = ROUTING_GENERATED_REL


class _ResetMapping(dict):
    """Compose ``depends_on: !reset`` — replace static merge, do not union."""


def _reset_mapping_representer(dumper: yaml.Dumper, data: _ResetMapping) -> yaml.Node:
    return dumper.represent_mapping("!reset", dict(data))


yaml.add_representer(_ResetMapping, _reset_mapping_representer)
yaml.SafeDumper.add_representer(_ResetMapping, _reset_mapping_representer)


def container_name_for_tunnel_service(service: str) -> str:
    """Map a Gluetun compose service name to its ``container_name``."""
    if service == "gluetun":
        return "kine-gluetun"
    if service.startswith("gluetun-"):
        return f"kine-gluetun-{service.removeprefix('gluetun-')}"
    # Legacy underscore service names from the first multi-tunnel builds.
    if service.startswith("gluetun_"):
        return f"kine-gluetun-{service.removeprefix('gluetun_')}"
    return f"kine-{service}"


# Ports shared inside a tunnel namespace (docs/port-map.md).
# Apps without Traefik routers still need a port entry for pinning.
APP_PORTS: dict[str, int] = {
    "ecm": 6100,
    "ecm-mcp": 6101,
    "bazarr": 6767,
    "nzbget": 6789,
    "radarr": 7878,
    "sonarr": 8989,
    "transmission": 9091,
    "jackett": 9117,
    "dispatcharr": 9191,
    "teamarr": 9195,
    "prowlarr": 9696,
    "unpackerr": 5656,
}

# Traefik Host() subdomain; shortcuts match vpn.gluetun.yml.
APP_TRAEFIK_HOST: dict[str, str] = {
    "sonarr": "sonarr",
    "radarr": "radarr",
    "prowlarr": "prowlarr",
    "jackett": "jackett",
    "bazarr": "bazarr",
    "transmission": "transmission",
    "nzbget": "nzbget",
    "dispatcharr": "tv",
    "ecm": "channels",
    "ecm-mcp": "mcp",
    "teamarr": "sports",
}


def _traefik_router_label_lines(
    apps: list[str],
    *,
    kine_domain: str,
    kine_local_domain: str,
) -> list[str]:
    """Full Traefik router label lines (for tests / fragments merge)."""
    labels = ["traefik.enable=true"]
    for app in apps:
        if app not in APP_TRAEFIK_HOST:
            continue
        host = APP_TRAEFIK_HOST[app]
        port = APP_PORTS[app]
        labels.append(
            f"traefik.http.routers.{app}.rule="
            f"Host(`{host}.{kine_domain}`) || Host(`{host}.{kine_local_domain}`)"
        )
        labels.append(f"traefik.http.routers.{app}.service={app}")
        labels.append(
            f"traefik.http.services.{app}.loadbalancer.server.port={port}"
        )
    return labels


def _traefik_labels(
    apps: list[str],
    *,
    kine_domain: str,
    kine_local_domain: str,
) -> list[str]:
    # Router labels used to live on Gluetun containers. Traefik's Docker
    # provider filters unhealthy/starting containers, and a flapping secondary
    # VPN then drops tv./channels./sports. routes with a bare 404. Routes are
    # written to the file provider instead (see write_traefik_dynamic).
    _ = (apps, kine_domain, kine_local_domain)
    return ["traefik.enable=true"]


def traefik_dynamic_path(stack_root: str | pathlib.Path) -> pathlib.Path:
    return pathlib.Path(stack_root) / "config" / "traefik" / "dynamic" / "vpn-tunnels.yml"


def render_traefik_dynamic(
    data: dict[str, Any],
    *,
    enabled_apps: set[str],
    kine_domain: str,
    kine_local_domain: str,
    vpn_enabled: bool = True,
) -> dict[str, Any]:
    """Traefik file-provider config for tunnelled app Host() routers."""
    routers: dict[str, Any] = {}
    services: dict[str, Any] = {}
    if vpn_enabled:
        for app in _enabled_tunnel_apps(enabled_apps):
            if app not in APP_TRAEFIK_HOST:
                continue
            host = APP_TRAEFIK_HOST[app]
            port = APP_PORTS[app]
            tunnel = vpn_profiles.tunnel_service(data, app)
            routers[app] = {
                "rule": (
                    f"Host(`{host}.{kine_domain}`) || "
                    f"Host(`{host}.{kine_local_domain}`)"
                ),
                "service": app,
                "entryPoints": ["websecure"],
                "tls": {},
            }
            services[app] = {
                "loadBalancer": {
                    "servers": [{"url": f"http://{tunnel}:{port}"}],
                },
            }
    return {"http": {"routers": routers, "services": services}}


def write_traefik_dynamic(
    stack_root: str | pathlib.Path,
    data: dict[str, Any],
    *,
    enabled_apps: set[str],
    kine_domain: str,
    kine_local_domain: str,
    vpn_enabled: bool = True,
) -> pathlib.Path:
    path = traefik_dynamic_path(stack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = render_traefik_dynamic(
        data,
        enabled_apps=enabled_apps,
        kine_domain=kine_domain,
        kine_local_domain=kine_local_domain,
        vpn_enabled=vpn_enabled,
    )
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _secondary_environment(conf: str) -> dict[str, str]:
    fields = wireguard.parse_conf(conf)
    if not fields:
        raise ValueError("invalid WireGuard config for secondary tunnel")
    env: dict[str, str] = {
        "TZ": "${KINE_TIMEZONE}",
        "VPN_SERVICE_PROVIDER": fields.get("VPN_SERVICE_PROVIDER") or "custom",
        "VPN_TYPE": fields.get("VPN_TYPE") or "wireguard",
        "WIREGUARD_PRIVATE_KEY": fields.get("WIREGUARD_PRIVATE_KEY") or "",
        "WIREGUARD_ADDRESSES": fields.get("WIREGUARD_ADDRESSES") or "",
        "WIREGUARD_PUBLIC_KEY": fields.get("WIREGUARD_PUBLIC_KEY") or "",
        "WIREGUARD_PRESHARED_KEY": fields.get("WIREGUARD_PRESHARED_KEY") or "",
        "WIREGUARD_ENDPOINT_IP": fields.get("WIREGUARD_ENDPOINT_IP") or "",
        "WIREGUARD_ENDPOINT_PORT": fields.get("WIREGUARD_ENDPOINT_PORT") or "",
        "SERVER_COUNTRIES": fields.get("VPN_SERVER_COUNTRIES") or "",
        "VPN_PORT_FORWARDING": fields.get("VPN_PORT_FORWARDING") or "off",
        "VPN_PORT_FORWARDING_PROVIDER": (
            fields.get("VPN_PORT_FORWARDING_PROVIDER") or ""
        ),
        "FIREWALL_OUTBOUND_SUBNETS": "${FIREWALL_OUTBOUND_SUBNETS}",
        "HTTP_CONTROL_SERVER_ADDRESS": ":8000",
        "DOT": "off",
    }
    return env


def _secondary_service(
    profile: dict[str, Any],
    apps: list[str],
    *,
    stack_root: str,
    kine_domain: str,
    kine_local_domain: str,
) -> dict[str, Any]:
    _ = stack_root  # host path is always ${STACK_ROOT} for compose interpolation
    sid = vpn_profiles.short_id(profile["id"])
    conf = profile.get("conf") or ""
    return {
        "image": "qmcgaw/gluetun:${GLUETUN_TAG}",
        "container_name": f"kine-gluetun-{sid}",
        "profiles": ["gluetun"],
        "restart": "unless-stopped",
        "cap_add": ["NET_ADMIN"],
        "devices": ["/dev/net/tun:/dev/net/tun"],
        "environment": _secondary_environment(conf),
        "volumes": [f"${{STACK_ROOT}}/config/gluetun-{sid}:/gluetun"],
        "networks": ["kine_internal", "kine_edge"],
        "healthcheck": {
            "test": ["CMD", "/gluetun-entrypoint", "healthcheck"],
            "interval": "30s",
            "timeout": "10s",
            "retries": 3,
            "start_period": "30s",
        },
        "labels": _traefik_labels(
            apps,
            kine_domain=kine_domain,
            kine_local_domain=kine_local_domain,
        ),
    }


def _enabled_tunnel_apps(enabled_apps: set[str]) -> list[str]:
    return [app for app in APP_PORTS if app in enabled_apps]


def _app_network_override(tunnel: str) -> dict[str, Any]:
    return {
        "network_mode": f"service:{tunnel}",
        "depends_on": _ResetMapping(
            {tunnel: {"condition": "service_healthy"}},
        ),
    }


def render_override(
    data: dict[str, Any],
    *,
    enabled_apps: set[str],
    stack_root: str,
    kine_domain: str,
    kine_local_domain: str,
    vpn_enabled: bool = True,
) -> str:
    """Build compose override YAML for secondary tunnels and app pinning."""
    if not vpn_enabled:
        doc = {"services": {}}
        return yaml.safe_dump(
            doc,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    services: dict[str, Any] = {}
    tunnel_apps: dict[str, list[str]] = {}
    for app in _enabled_tunnel_apps(enabled_apps):
        svc = vpn_profiles.tunnel_service(data, app)
        tunnel_apps.setdefault(svc, []).append(app)

    primary_apps = tunnel_apps.get("gluetun", [])
    if primary_apps:
        services["gluetun"] = {
            "labels": _traefik_labels(
                primary_apps,
                kine_domain=kine_domain,
                kine_local_domain=kine_local_domain,
            ),
        }

    primary_id = data.get("primary_id")
    profiles_by_id = {
        p.get("id"): p
        for p in (data.get("profiles") or [])
        if isinstance(p, dict) and p.get("id")
    }
    for svc, apps in tunnel_apps.items():
        if svc == "gluetun" or not (
            svc.startswith("gluetun-") or svc.startswith("gluetun_")
        ):
            continue
        sid = svc.removeprefix("gluetun-").removeprefix("gluetun_")
        profile = None
        for p in profiles_by_id.values():
            if p.get("id") == primary_id:
                continue
            if vpn_profiles.short_id(p["id"]) == sid:
                profile = p
                break
        if profile is None:
            continue
        services[svc] = _secondary_service(
            profile,
            apps,
            stack_root=stack_root,
            kine_domain=kine_domain,
            kine_local_domain=kine_local_domain,
        )

    for app in _enabled_tunnel_apps(enabled_apps):
        tunnel = vpn_profiles.tunnel_service(data, app)
        services[app] = _app_network_override(tunnel)

    if "transmission" in enabled_apps:
        tx_tunnel = vpn_profiles.tunnel_service(data, "transmission")
        services["vpn-portsync"] = _app_network_override(tx_tunnel)

    doc = {"services": services}
    return yaml.safe_dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def write_override(repo: pathlib.Path, text: str) -> pathlib.Path:
    path = pathlib.Path(repo) / ROUTING_GENERATED_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text = text + "\n"
    path.write_text(text)
    return path


def ensure_generated_stub(repo: pathlib.Path) -> pathlib.Path:
    """Create an empty generated override if missing (fresh clone / first up)."""
    path = pathlib.Path(repo) / ROUTING_GENERATED_REL
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")
    return path


def running_secondaries(
    data: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    """Non-primary profiles that own at least one app, with compose service names."""
    primary_id = data.get("primary_id")
    out: list[tuple[dict[str, Any], str]] = []
    for profile in data.get("profiles") or []:
        if not isinstance(profile, dict) or not profile.get("id"):
            continue
        if profile.get("id") == primary_id:
            continue
        if not (profile.get("apps") or []):
            continue
        sid = vpn_profiles.short_id(profile["id"])
        out.append((profile, vpn_profiles.secondary_tunnel_service(profile["id"])))
    return out


def stale_secondary_services(data: dict[str, Any]) -> list[str]:
    """Secondary tunnel services that should be stopped (empty apps or promoted)."""
    primary_id = data.get("primary_id")
    current = {svc for _, svc in running_secondaries(data)}
    stale: list[str] = []
    for profile in data.get("profiles") or []:
        if not isinstance(profile, dict) or not profile.get("id"):
            continue
        svc = vpn_profiles.secondary_tunnel_service(profile["id"])
        # Also stop any leftover underscore-named service from older builds.
        legacy = f"gluetun_{vpn_profiles.short_id(profile['id'])}"
        if profile.get("id") == primary_id or svc not in current:
            stale.append(svc)
            if legacy != svc:
                stale.append(legacy)
    return stale


def active_tunnel_services(data: dict[str, Any]) -> list[str]:
    """Primary + running secondary compose service names."""
    return ["gluetun", *[svc for _, svc in running_secondaries(data)]]


def peers_for(
    data: dict[str, Any],
    service: str,
    enabled: set[str],
) -> list[str]:
    """Enabled tunnel apps whose ``network_mode`` targets ``service``."""
    peers = [
        app
        for app in _enabled_tunnel_apps(enabled)
        if vpn_profiles.tunnel_service(data, app) == service
    ]
    if (
        service == vpn_profiles.tunnel_service(data, "transmission")
        and "transmission" in enabled
    ):
        peers.append("vpn-portsync")
    return peers


def recreate_group(
    data: dict[str, Any],
    service: str,
    enabled: set[str],
) -> list[str]:
    """Compose services to recreate for one tunnel namespace."""
    peers = peers_for(data, service, enabled)
    group = [service, *peers]
    seen: set[str] = set()
    return [s for s in group if not (s in seen or seen.add(s))]


def apply_filesystem(
    stack_root: str,
    repo: pathlib.Path,
    data: dict[str, Any],
    enabled: set[str],
    *,
    kine_domain: str,
    kine_local_domain: str,
    vpn_enabled: bool = True,
) -> None:
    """Write primary/secondary wg0.conf files and regenerate the compose override.

    Does not run ``docker compose``; callers recreate tunnel groups separately.
    When ``vpn_enabled`` is false, only writes an empty override (no wg0 rewrite).
    """
    ensure_generated_stub(repo)
    if vpn_enabled:
        primary_id = data.get("primary_id")
        by_id = {
            p.get("id"): p
            for p in (data.get("profiles") or [])
            if isinstance(p, dict) and p.get("id")
        }
        if primary_id and primary_id in by_id:
            conf = (by_id[primary_id].get("conf") or "").strip()
            if conf:
                wireguard.write_gluetun_conf(conf, stack_root)

        for profile, _svc in running_secondaries(data):
            apps = [a for a in (profile.get("apps") or []) if a in enabled]
            if not apps:
                continue
            conf = (profile.get("conf") or "").strip()
            if not conf:
                continue
            wireguard.write_secondary_conf(
                stack_root, vpn_profiles.short_id(profile["id"]), conf
            )

    text = render_override(
        data,
        enabled_apps=enabled,
        stack_root="${STACK_ROOT}",
        kine_domain=kine_domain,
        kine_local_domain=kine_local_domain,
        vpn_enabled=vpn_enabled,
    )
    write_override(repo, text)
    write_traefik_dynamic(
        stack_root,
        data,
        enabled_apps=enabled,
        kine_domain=kine_domain,
        kine_local_domain=kine_local_domain,
        vpn_enabled=vpn_enabled,
    )


# Alias used in plan / Task 4 notes.
apply_routing_sync = apply_filesystem
ensure_routing = apply_filesystem
