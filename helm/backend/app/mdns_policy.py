"""When the Avahi sidecar should run.

mDNS (RFC 6762) only resolves the .local TLD. Advertising a real DNS
name such as couttsnet.com is invalid, and Avahi/dbus then crash-loops
the container. Helm keeps the mdns profile for kine.local installs and
drops it once Settings uses a proper domain.
"""


def should_run(domain: str, profiles) -> bool:
    if "mdns" not in set(profiles):
        return False
    name = (domain or "").strip().rstrip(".").lower()
    return name.endswith(".local")
