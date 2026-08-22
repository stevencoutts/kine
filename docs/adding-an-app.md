# Adding an application

Three files, in this order.

## 1. A compose fragment

`compose/<tier>.<app>.yml`. Rules that are not optional:

- `profiles: ["<app>"]` matching the app's own name
- `container_name: mc-<app>`
- config at `${STACK_ROOT}/config/<app>`, media at `${DATA_ROOT}` as a
  single `/data` mount if it is an *arr-style app
- `image: <repo>:${<APP>_TAG}` with the tag in `.env.example`
- a healthcheck, because the updater's rollback depends on one
- Traefik labels, unless it is tunnelled

If it must go through the VPN, give it `network_mode: "service:gluetun"`
and no ports, no networks and no labels. Its Traefik router goes on the
gluetun service instead.

## 2. A catalogue entry

`catalogue.yml`. `summary` and `releases` are what the GUI shows.
`requires` makes the GUI pull dependencies in when you enable it, and
refuse to disable something that is still depended on.

## 3. A provisioning recipe (only if it needs wiring)

`provision/recipes/<name>.py` with a `configure(...)` function, called
from `provision.py`. Every write must go through `ArrClient.ensure()` or
an equivalent existence check: `./mc provision` runs on every enable and
must stay safe to repeat.

If the app takes an API key, derive it with `keys.api_key("<app>")`
rather than generating one, and seed its config file in `seed.py` so it
adopts that key on first start.

## Then

```bash
./mc enable <app>
./mc provision
```
