"""Host-aware application URLs routed through Traefik."""


def app_url(
    subdomain: str | None,
    domain: str,
    request_host: str | None,
    local_domain: str,
) -> str | None:
    if not subdomain:
        return None
    loopback = request_host in {"127.0.0.1", "::1", "localhost"}
    target_domain = local_domain if loopback else domain
    return f"https://{subdomain}.{target_domain}"
