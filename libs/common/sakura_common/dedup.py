"""Import deduplication helpers.

A CSV row's identity is the tuple (account, date, amount, normalized
description). The same row uploaded again next month — because statement date
ranges overlapped — produces the same hash and is flagged as a duplicate
instead of double-posting.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_description(description: str) -> str:
    """Uppercase and collapse whitespace so cosmetic differences don't defeat
    dedup or payee-alias matching."""
    return _WHITESPACE_RE.sub(" ", description.strip()).upper()


def row_hash(account_id: int, when: date, amount: Decimal, description: str) -> str:
    """Stable identity hash for an imported row."""
    key = f"{account_id}|{when.isoformat()}|{amount}|{normalize_description(description)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
