# Port map inside the tunnel

Every tier 2 application shares gluetun's network namespace, which
means they share one port space. Two apps wanting the same port is not
a configuration clash to resolve later; it is a container that will not
start. Anything added to tier 2 must claim a free port here first.

| Port | Application | Reached from outside as |
|---|---|---|
| 6767 | Bazarr | `gluetun:6767` |
| 6789 | NZBGet | `gluetun:6789` |
| 7878 | Radarr | `gluetun:7878` |
| 8000 | gluetun control server | `gluetun:8000` (internal only) |
| 8989 | Sonarr | `gluetun:8989` |
| 9091 | Transmission | `gluetun:9091` |
| 9696 | Prowlarr | `gluetun:9696` |

Inside the namespace, apps address each other as `127.0.0.1:<port>`.
From `kine_internal` (Traefik, Helm, the provisioner, Emby) they are
`gluetun:<port>`, because gluetun is the container that actually holds
those sockets.

Untunnelled, on their own service names as usual:

| Application | Address |
|---|---|
| Emby | `emby:8096` |
| Dispatcharr | `dispatcharr:9191` |
| ECM | `ecm:8080` |
| Teamarr | `teamarr:8080` |
