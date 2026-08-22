"""Which names to advertise over mDNS: the appliance's own domain plus
every enabled app's subdomain.

Split into a pure function so it's testable without a running avahi
daemon. Not called "gen_hosts" for /etc/avahi/hosts any more --
verified against a running avahi-daemon 0.8 that the static-hosts file
refuses ("Local name collision") to alias a name onto an address the
daemon already owns as its own interface address, which is exactly
this appliance's situation. avahi-publish, talking to the daemon over
D-Bus, does not have that restriction; entrypoint.sh uses these names
with it instead.
"""
import os
import pathlib

import yaml


def build_names(domain: str, profiles: set[str], catalogue: dict) -> list[str]:
    names = [domain]
    for key, meta in catalogue.items():
        sub = meta.get("subdomain")
        if sub and key in profiles:
            names.append(f"{sub}.{domain}")
    return names


def main() -> None:
    domain = os.environ.get("KINE_DOMAIN", "kine.local")
    profiles = {p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
    catalogue = yaml.safe_load(pathlib.Path("/catalogue.yml").read_text())["apps"]
    for name in build_names(domain, profiles, catalogue):
        print(name)


if __name__ == "__main__":
    main()
