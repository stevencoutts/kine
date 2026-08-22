"""Thin client for the Sonarr/Radarr/Prowlarr v3-style REST API."""
import time

import httpx


class ArrClient:
    def __init__(self, base: str, key: str, api: str = "v3"):
        self.base = base.rstrip("/")
        self.api = api
        self.http = httpx.Client(
            headers={"X-Api-Key": key}, timeout=30.0, follow_redirects=True
        )

    def _url(self, path: str) -> str:
        return f"{self.base}/api/{self.api}/{path.lstrip('/')}"

    def wait(self, timeout: int = 300) -> bool:
        """Block until the app answers on its API, or give up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.http.get(self._url("system/status"))
                if r.status_code == 200:
                    return True
                if r.status_code == 401:
                    raise RuntimeError(
                        f"{self.base} rejected the derived API key. The app was "
                        f"started before it was seeded. Stop it, delete its "
                        f"config.xml and re-run ./kine provision --force."
                    )
            except httpx.HTTPError:
                pass
            time.sleep(5)
        return False

    def get(self, path: str):
        r = self.http.get(self._url(path))
        r.raise_for_status()
        return r.json()

    def post(self, path: str, payload: dict):
        r = self.http.post(self._url(path), json=payload)
        r.raise_for_status()
        return r.json() if r.content else {}

    def put(self, path: str, payload: dict):
        r = self.http.put(self._url(path), json=payload)
        r.raise_for_status()
        return r.json() if r.content else {}

    def ensure(self, path: str, payload: dict, match_on: str = "name") -> bool:
        """Idempotent create. Returns True if something was created.

        Every recipe goes through this, which is what makes
        `./kine provision` safe to run as many times as you like.
        """
        existing = self.get(path)
        wanted = payload[match_on]
        for item in existing:
            if item.get(match_on) == wanted:
                return False
        self.post(path, payload)
        return True
