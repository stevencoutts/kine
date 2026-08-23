# Adding an application

Three files, in this order.

## 1. A compose fragment

`compose/<tier>.<app>.yml`. Rules that are not optional:

- `profiles: ["<app>"]` matching the app's own name
- `container_name: kine-<app>`
- config at `${STACK_ROOT}/config/<app>`, media at `${DATA_ROOT}` as a
  single `/data` mount if it is an *arr-style app
- `image: <repo>:${<APP>_TAG}` with the tag in `.env.example`
- a healthcheck, because the updater's rollback depends on one
- Traefik labels, unless it is tunnelled

If it belongs to tier 2, it goes through the VPN, and that is not
optional: give it `network_mode: "service:gluetun"`, a
`depends_on: gluetun` gated on `service_healthy`, and no ports, no
networks and no labels. Its Traefik router goes on the gluetun service
instead, and its catalogue entry needs `requires: [gluetun]`.

Before you pick its port, check `docs/port-map.md`. Tunnelled apps
share one network stack, so a port already claimed in there is not a
conflict you resolve in configuration; it is a container that refuses
to start. Add your app to that table in the same commit.

## 2. A catalogue entry

`catalogue.yml`. `summary` and `releases` are what the GUI shows.
`requires` makes the GUI pull dependencies in when you enable it, and
refuse to disable something that is still depended on. Optional `dev_tag`
(for example `develop`, `development`, `dev`, `beta`, `edge`) declares the
unstable image channel for the Apps page Dev Version control: Helm adds the
app to `APP_DEV_CHANNELS`, saves the current pin in `<APP>_STABLE_TAG`, and
points `<APP>_TAG` at `dev_tag` (clearing `<APP>_DIGEST`). Omit `dev_tag`
when there is no public develop/nightly channel.

## 3. A provisioning recipe (only if it needs wiring)

`provision/recipes/<name>.py` with a `configure(...)` function, called
from `provision.py`. Every write must go through `ArrClient.ensure()`,
`JackettClient.ensure_indexer()`, or an equivalent existence check:
`./kine provision` runs on every enable and must stay safe to repeat.

If the app takes an API key, derive it with `keys.api_key("<app>")`
rather than generating one, and seed its config file in `seed.py` so it
adopts that key on first start (`config.xml` for *arr apps;
`config/jackett/Jackett/ServerConfig.json` for Jackett).

## Then

```bash
./kine enable <app>
./kine provision
```
