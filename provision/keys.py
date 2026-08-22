"""Deterministic API keys.

Every internal API key is derived from KINE_SECRET, so the provisioner
knows Sonarr's key before Sonarr has ever started. That is what lets
the appliance ship pre-wired: there is no chicken-and-egg where you
must start an app, log in, copy a key and paste it somewhere else.

Consequence to be aware of: rotating KINE_SECRET re-keys the whole stack,
and any external client holding an old key stops working. `./kine rekey`
does it properly by re-seeding and re-provisioning together.

If an app was started before seed (or retained a pre-existing config.xml),
`resolve_key` reads the live key from disk so wiring still matches the
process that is actually running.
"""
import hashlib
import json
import os
import pathlib
import xml.etree.ElementTree as ET

STACK = pathlib.Path("/stack")


def api_key(app: str) -> str:
    secret = os.environ["KINE_SECRET"]
    if not secret:
        raise RuntimeError("KINE_SECRET is empty; run install.sh")
    digest = hashlib.sha256(f"{secret}:{app}".encode()).hexdigest()
    return digest[:32]


def resolve_key(app: str) -> str:
    """Return the API key the running app will accept.

    Prefer on-disk config when present (seed adopts existing keys and never
    overwrites them). Fall back to the derived key for first-time seed.
    """
    if app == "jackett":
        cfg = STACK / "config" / "jackett" / "Jackett" / "ServerConfig.json"
        if cfg.exists():
            try:
                existing = json.loads(cfg.read_text()).get("APIKey")
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing:
                return existing
        return api_key("jackett")

    cfg = STACK / "config" / app / "config.xml"
    if cfg.exists():
        try:
            existing = ET.parse(cfg).getroot().findtext("ApiKey")
        except ET.ParseError:
            existing = None
        if existing:
            return existing
    return api_key(app)
