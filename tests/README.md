# Tests

```bash
pip install pytest pyyaml
python -m pytest tests -q
```

These are not unit tests of business logic; the appliance barely has
any. They assert the structural invariants that Compose and Docker will
happily let you violate, and that only surface as strange runtime
behaviour:

- a tunnelled app still carrying its own ports, networks or Traefik
  labels, which Compose silently ignores rather than rejecting
- two apps claiming the same port inside the shared VPN namespace
- a tunnelled app with no router on gluetun, so it is unreachable with
  no error anywhere to say so
- an app starting before the tunnel is healthy, so its first requests
  leave untunnelled
- an image pinned without an env tag, which the updater cannot manage
- Sonarr or Radarr given split media and download mounts, which costs
  hardlinks silently
- Helm reaching the raw Docker socket instead of the proxy
- the seeder overwriting an API key an external client already holds

Run them before every commit. `test_no_port_collisions_inside_the_tunnel`
and `test_tunnelled_apps_own_nothing_networkish` in particular have
already caught real mistakes in this repo.
