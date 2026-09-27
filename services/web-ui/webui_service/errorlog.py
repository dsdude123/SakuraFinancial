"""A small ring buffer of recent failures, so the error page can offer the
traceback instead of sending the user to ``docker compose logs``.

Kept in memory on purpose: it is diagnostic breadcrumbs for the session you are
in, not a record worth persisting or backing up.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import OrderedDict

MAX_ENTRIES = 25


class ErrorLog:
    def __init__(self, limit: int = MAX_ENTRIES):
        self._entries: OrderedDict[str, dict] = OrderedDict()
        self._limit = limit

    def record(
        self,
        *,
        service: str,
        status: int | None,
        message: str,
        traceback: str = "",
        exception: str = "",
        path: str = "",
    ) -> str:
        error_id = uuid.uuid4().hex[:8]
        self._entries[error_id] = {
            "id": error_id,
            "when": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "service": service,
            "status": status,
            "message": message,
            "traceback": traceback,
            "exception": exception,
            "path": path,
        }
        while len(self._entries) > self._limit:
            self._entries.popitem(last=False)
        return error_id

    def get(self, error_id: str) -> dict | None:
        return self._entries.get(error_id)

    def recent(self) -> list[dict]:
        return list(reversed(self._entries.values()))
