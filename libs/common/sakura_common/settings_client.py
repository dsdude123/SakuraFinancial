"""Client for the settings-service key-value store.

Services read configuration through this at call time — never at startup — so
values changed in the UI apply immediately. Reads are deliberately forgiving:
a missing key or an unreachable settings service returns the caller's default,
because core money management must keep working when optional infrastructure
is down. Writes raise, because losing a write silently would be worse.
"""

from __future__ import annotations

import os

import httpx

MASK = "********"


class SettingsWriteError(Exception):
    pass


class SettingsClient:
    def __init__(self, base_url: str | None = None, timeout: float = 5.0, transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or os.environ.get("SETTINGS_URL", "http://settings:8005")).rstrip("/")
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the raw (unmasked) value, or ``default`` if missing/unreachable."""
        try:
            response = self._http.get(f"{self.base_url}/api/settings/{key}", params={"reveal": "true"})
        except httpx.HTTPError:
            return default
        if response.status_code != 200:
            return default
        return response.json().get("value", default)

    def set(self, key: str, value: str, is_secret: bool = False) -> None:
        try:
            response = self._http.put(
                f"{self.base_url}/api/settings/{key}",
                json={"value": value, "is_secret": is_secret},
            )
        except httpx.HTTPError as exc:
            raise SettingsWriteError(f"settings service unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise SettingsWriteError(f"settings write failed ({response.status_code}): {response.text[:300]}")

    def delete(self, key: str) -> None:
        try:
            self._http.delete(f"{self.base_url}/api/settings/{key}")
        except httpx.HTTPError as exc:
            raise SettingsWriteError(f"settings service unreachable: {exc}") from exc
