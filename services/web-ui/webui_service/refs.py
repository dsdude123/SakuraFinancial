"""Account and profile ids, namespaced across the two services that own them.

Bank accounts are numbered by the ledger service and investment accounts by
the stocks service, independently — both have an account 1. Anywhere a form
can mean either one, the value is ``"<kind>:<id>"`` ("bank:3", "stock:1").
A bare id means the bank ledger, which keeps older bookmarks and the forms
that only ever deal in ledger accounts working unchanged.
"""

from __future__ import annotations

KINDS = ("bank", "stock")
KIND_LABELS = {"bank": "Bank / credit card", "stock": "Brokerage"}


def split_ref(value: str, default_kind: str = "bank") -> tuple[str, str]:
    """"stock:4" -> ("stock", "4"). A bare id means the bank ledger, which keeps
    older bookmarks and the monthly page's per-account forms working."""
    text = str(value or "").strip()
    if ":" in text:
        kind, _, ident = text.partition(":")
        if kind in KINDS:
            return kind, ident.strip()
    return default_kind, text


def join_ref(kind: str, account_id: int | str) -> str:
    """The inverse, for what gets stored on a transfer leg: ("stock", 1) ->
    "stock:1"."""
    return f"{kind}:{account_id}"
