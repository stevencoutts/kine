"""Parsing a client WireGuard .conf, for onboarding's "add my config" step.

Split out from main.py so this pure-stdlib bit of parsing is testable
without dragging in FastAPI.
"""


def parse_conf(text: str) -> dict[str, str]:
    """Pull the two fields gluetun needs out of a client .conf.

    Named providers (protonvpn, mullvad, ...) already know their own
    servers; the client only ever needs to bring its private key and
    its tunnel address. Anything else in the file (DNS, peer, endpoint)
    is provider detail gluetun resolves on its own.
    """
    found = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k.lower() == "privatekey":
            found["WIREGUARD_PRIVATE_KEY"] = v
        elif k.lower() == "address":
            found["WIREGUARD_ADDRESSES"] = v
    return found
