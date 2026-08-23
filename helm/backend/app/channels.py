"""Per-app image channel (stable vs development).

Catalogue entries that publish a distinct unstable image declare
`dev_tag`. Membership lives in `APP_DEV_CHANNELS` the same way enablement
lives in `COMPOSE_PROFILES`. Enabling a channel swaps `<APP>_TAG` to that
dev tag (remembering the prior pin in `<APP>_STABLE_TAG`) and clears the
digest so Compose tracks the floating channel again.
"""
from __future__ import annotations

from . import config


def env_prefix(app_id: str) -> str:
    return app_id.upper().replace("-", "_")


def tag_key(app_id: str) -> str:
    return f"{env_prefix(app_id)}_TAG"


def digest_key(app_id: str) -> str:
    return f"{env_prefix(app_id)}_DIGEST"


def stable_tag_key(app_id: str) -> str:
    return f"{env_prefix(app_id)}_STABLE_TAG"


def supported(meta: dict) -> bool:
    return bool(meta.get("dev_tag"))


def channels() -> list[str]:
    raw = config.read().get("APP_DEV_CHANNELS", "")
    return [p for p in (x.strip() for x in raw.split(",")) if p]


def set_channels(items: list[str]) -> None:
    config.write({"APP_DEV_CHANNELS": ",".join(dict.fromkeys(items))})


def plan(app_id: str, meta: dict, *, enabled: bool) -> dict[str, str]:
    """Env updates for switching channel. Raises ValueError if unsupported."""
    if not supported(meta):
        raise ValueError(f"{app_id} has no development image channel")
    env = config.read()
    current = channels()
    updates: dict[str, str] = {}
    tkey = tag_key(app_id)
    dkey = digest_key(app_id)
    skey = stable_tag_key(app_id)

    if enabled:
        if app_id not in current:
            # Remember the stable pin only when first entering dev, so a
            # re-toggle does not overwrite it with the develop tag itself.
            if skey not in env or not env.get(skey):
                updates[skey] = env.get(tkey, "latest") or "latest"
            current = [*current, app_id]
        updates[tkey] = str(meta["dev_tag"])
        updates[dkey] = ""
        updates["APP_DEV_CHANNELS"] = ",".join(dict.fromkeys(current))
        return updates

    if app_id in current:
        current = [p for p in current if p != app_id]
    restore = env.get(skey) or "latest"
    updates[tkey] = restore
    updates[dkey] = ""
    updates[skey] = ""
    updates["APP_DEV_CHANNELS"] = ",".join(dict.fromkeys(current))
    return updates


def apply(app_id: str, meta: dict, *, enabled: bool) -> dict[str, str]:
    updates = plan(app_id, meta, enabled=enabled)
    config.write(updates)
    return updates
