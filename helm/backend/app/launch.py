"""Host-aware application URLs routed through Traefik."""


def app_url(
    subdomain: str | None,
    domain: str,
    request_host: str | None,
    local_domain: str,
    https_port: str | int | None = None,
) -> str | None:
    if not subdomain:
        return None
    loopback = request_host in {"127.0.0.1", "::1", "localhost"}
    target_domain = local_domain if loopback else domain
    port = str(https_port or "443").strip() or "443"
    suffix = "" if port in {"443", "80"} else f":{port}"
    return f"https://{subdomain}.{target_domain}{suffix}"
