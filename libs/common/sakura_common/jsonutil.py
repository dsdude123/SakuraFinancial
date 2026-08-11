"""JSON helpers that round-trip Decimal and date values.

Used by every service's /api/export and /api/import endpoints so backups are
human-readable JSON without losing monetary precision. Decimals are written as
strings (e.g. "1234.50"); dates as ISO "YYYY-MM-DD"; datetimes as ISO 8601.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


class SakuraJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)


def dumps(data: Any, indent: int | None = 2) -> str:
    """Serialize with stable key order — exports should diff cleanly."""
    return json.dumps(data, cls=SakuraJSONEncoder, indent=indent, sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text)


def parse_decimal(value: Any) -> Decimal | None:
    """Read a Decimal back out of an export file (stored as string)."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    return date.fromisoformat(str(value))


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(str(value))
