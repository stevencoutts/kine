"""Generate compose override YAML for multi-Gluetun egress routing."""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

from . import vpn_profiles, wireguard

ROUTING_REL = pathlib.Path("compose/vpn-routing.override.yml")


def container_name_for_tunnel_service(service: str) -> str:
    """Map a Gluetun compose service name to its ``container_name``."""
    if service == "gluetun":
        return "kine-gluetun"
    if service.startswith("gluetun_"):
        return f"kine-gluetun-{service.removeprefix('gluetun_')}"
    return f"kine-{service}"


# Ports shared inside a tunnel namespace (docs/port-map.md).
APP_PORTS: dict[str, int] = {
    "ecm": 6100,
    "bazarr": 6767,
    "nzbget": 6789,
    "radarr": 7878,
    "sonarr": 8989,
    "transmission": 9091,
    "jackett": 9117,
    "dispatcharr": 9191,
    "teamarr": 9195,
    "prowlarr": 9696,
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
    "teamarr": "sports",
}


def _traefik_labels(
    apps: list[str],
    *,
    kine_domain: str,
    kine_local_domain: str,
) -> list[str]:
    labels = ["traefik.enable=true"]
    for app in apps:
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


def render_override(
    data: dict[str, Any],
    *,
    enabled_apps: set[str],
    stack_root: str,
    kine_domain: str,
    kine_local_domain: str,
) -> str:
    """Build compose override YAML for secondary tunnels and app pinning."""
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
        if svc == "gluetun" or not svc.startswith("gluetun_"):
            continue
        sid = svc.removeprefix("gluetun_")
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
        services[app] = {
            "network_mode": f"service:{tunnel}",
            "depends_on": {
                tunnel: {"condition": "service_healthy"},
            },
        }

    doc = {"services": services}
    return yaml.safe_dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def write_override(repo: pathlib.Path, text: str) -> pathlib.Path:
    path = pathlib.Path(repo) / ROUTING_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text = text + "\n"
    path.write_text(text)
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
        out.append((profile, f"gluetun_{sid}"))
    return out


def peers_for(
    data: dict[str, Any],
    service: str,
    enabled: set[str],
) -> list[str]:
    """Enabled tunnel apps whose ``network_mode`` targets ``service``."""
    return [
        app
        for app in _enabled_tunnel_apps(enabled)
        if vpn_profiles.tunnel_service(data, app) == service
    ]


def apply_filesystem(
    stack_root: str,
    repo: pathlib.Path,
    data: dict[str, Any],
    enabled: set[str],
    *,
    kine_domain: str,
    kine_local_domain: str,
) -> None:
    """Write primary/secondary wg0.conf files and regenerate the compose override.

    Does not run ``docker compose``; callers recreate tunnel groups separately.
    """
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
    )
    write_override(repo, text)


# Alias used in plan / Task 4 notes.
apply_routing_sync = apply_filesystem
ensure_routing = apply_filesystem
