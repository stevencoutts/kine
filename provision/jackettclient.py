"""Thin client for Jackett's internal REST API.

Indexer management endpoints require a UI session cookie even when no
admin password is set. The login flow is GET /UI/Login -> TestCookie ->
authenticated session.
"""
import time
from urllib.parse import urljoin

import httpx


class JackettClient:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(
            headers={"X-Api-Key": key},
            timeout=60.0,
            follow_redirects=False,
        )
        self._session_ready = False

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _login(self) -> None:
        if self._session_ready:
            return
        current_url = self._url("/UI/Login")
        response = self.http.get(current_url)
        for _ in range(8):
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get("location", "")
            current_url = urljoin(current_url, location)
            response = self.http.get(current_url)
        if response.status_code == 400 and "Cookies required" in response.text:
            raise RuntimeError("Jackett login failed: cookies required")
        self._session_ready = True

    def wait(self, timeout: int = 300) -> bool:
        """Block until Jackett accepts indexer API calls, or give up."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self._login()
                response = self.http.get(
                    self._url("/api/v2.0/indexers?configured=true")
                )
                if response.status_code == 200:
                    return True
                if response.status_code == 401:
                    raise RuntimeError(
                        f"{self.base} rejected the derived API key. Jackett was "
                        f"started before it was seeded. Stop it, delete "
                        f"config/jackett/Jackett/ServerConfig.json and re-run "
                        f"./kine provision --force."
                    )
            except (httpx.HTTPError, RuntimeError):
                pass
            self._session_ready = False
            time.sleep(5)
        return False

    def configured_ids(self) -> set[str]:
        self._login()
        response = self.http.get(self._url("/api/v2.0/indexers?configured=true"))
        response.raise_for_status()
        return {item["id"] for item in response.json()}

    def config_values(self, indexer_id: str) -> dict[str, str | None]:
        self._login()
        response = self.http.get(
            self._url(f"/api/v2.0/indexers/{indexer_id}/config")
        )
        response.raise_for_status()
        return {
            item["id"]: item.get("value")
            for item in response.json()
            if item.get("type") not in ("displayinfo", "hiddendata")
            and "value" in item
        }

    def apply_config(self, indexer_id: str, settings: dict[str, str]) -> None:
        self._login()
        payload = [{"id": key, "value": value} for key, value in settings.items()]
        response = self.http.post(
            self._url(f"/api/v2.0/indexers/{indexer_id}/config"),
            json=payload,
        )
        response.raise_for_status()

    def ensure_indexer(self, indexer_id: str, settings: dict[str, str]) -> bool:
        """Apply indexer settings when missing or different."""
        configured = indexer_id in self.configured_ids()
        if configured and all(
            self.config_values(indexer_id).get(key) == value
            for key, value in settings.items()
        ):
            return False
        self.apply_config(indexer_id, settings)
        return True
