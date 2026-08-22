"""Deterministic API keys.

Every internal API key is derived from MC_SECRET, so the provisioner
knows Sonarr's key before Sonarr has ever started. That is what lets
the appliance ship pre-wired: there is no chicken-and-egg where you
must start an app, log in, copy a key and paste it somewhere else.

Consequence to be aware of: rotating MC_SECRET re-keys the whole stack,
and any external client holding an old key stops working. `./mc rekey`
does it properly by re-seeding and re-provisioning together.
"""
import hashlib
import os


def api_key(app: str) -> str:
    secret = os.environ["MC_SECRET"]
    if not secret:
        raise RuntimeError("MC_SECRET is empty; run install.sh")
    digest = hashlib.sha256(f"{secret}:{app}".encode()).hexdigest()
    return digest[:32]
