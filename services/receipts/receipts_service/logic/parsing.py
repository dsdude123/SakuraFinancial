"""LLM parsing of extracted receipt text into vendor/date/total + line items
with category guesses. Optional: without a configured LLM the document lands
in needs_review with its extracted text for manual entry."""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from sakura_common import jsonutil
from sakura_common.llm import LLMClient, LLMError, LLMNotConfigured, config_from_settings

from ..models import Document, ReceiptItem

SYSTEM_PROMPT = (
    "You extract structured data from receipts and invoices for a personal "
    "finance app. Reply with ONLY a JSON object, no prose, in this shape:\n"
    '{"vendor": "...", "date": "YYYY-MM-DD", "total": "123.45", '
    '"currency": "USD", "items": [{"description": "...", "amount": "12.34", '
    '"category": "..."}]}\n'
    "Rules: total is the amount actually charged (after tax/tip/discounts). "
    "Item amounts should sum close to the total; fold tax proportionally into "
    "items or add a 'Tax' item. Pick each item's category ONLY from the "
    "provided category list; if unsure use the closest fit. Dates in ISO. "
    "If a field is unreadable, use null."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_document(
    db: Session,
    document: Document,
    settings_client,
    categories: list[dict],
    image_data: tuple[str, bytes] | None = None,
) -> Document:
    """Run the LLM over the document. Mutates the document in-session."""
    try:
        config = config_from_settings(settings_client)
    except LLMNotConfigured:
        document.status = "needs_review"
        document.parse_note = (
            "no AI provider configured — fill in the fields manually "
            "(Settings page can enable AI parsing)"
        )
        return document

    # Offer the LLM full paths ("Food: Groceries") so it can pick the specific
    # subcategory instead of guessing between same-named leaves.
    category_names = [
        c.get("path") or c["name"] for c in categories if c.get("kind") == "expense"
    ]
    prompt = (
        f"CATEGORY LIST: {', '.join(category_names) or '(none defined yet)'}\n\n"
        f"FILENAME: {document.filename}\n\n"
        f"RECEIPT TEXT:\n{document.extracted_text[:8000] or '(no text extracted — see image)'}"
    )
    images = []
    if image_data is not None and not document.extracted_text:
        media_type, content = image_data
        images.append((media_type, base64.b64encode(content).decode("ascii")))
    try:
        reply = LLMClient(config).complete(prompt, system=SYSTEM_PROMPT, images=images)
    except LLMError as exc:
        document.status = "needs_review"
        document.parse_note = f"AI parsing failed: {exc}"
        return document

    match = _JSON_RE.search(reply)
    if match is None:
        document.status = "needs_review"
        document.parse_note = f"AI reply was not JSON: {reply[:200]}"
        return document
    try:
        data = json.loads(match.group(0))
    except ValueError:
        document.status = "needs_review"
        document.parse_note = f"AI reply JSON did not parse: {reply[:200]}"
        return document

    if data.get("vendor"):
        document.vendor = str(data["vendor"])[:200]
    if data.get("date"):
        try:
            document.doc_date = jsonutil.parse_date(data["date"])
        except ValueError:
            pass
    if data.get("total") is not None:
        try:
            document.total = Decimal(str(data["total"]))
        except InvalidOperation:
            pass
    if data.get("currency"):
        document.currency = str(data["currency"])[:3].upper()

    # Resolve a guess against the full path first, then the bare leaf name, so
    # both "Food: Groceries" and "Groceries" land on the right category.
    by_name: dict[str, int] = {}
    for c in categories:
        by_name.setdefault(c["name"].lower(), c["id"])
    for c in categories:
        if c.get("path"):
            by_name[c["path"].lower()] = c["id"]
    document.items.clear()
    for item in data.get("items") or []:
        try:
            amount = Decimal(str(item["amount"]))
        except (KeyError, InvalidOperation, TypeError):
            continue
        category_name = str(item.get("category") or "")
        document.items.append(
            ReceiptItem(
                description=str(item.get("description") or "")[:500],
                amount=amount,
                category_name=category_name,
                category_id=by_name.get(category_name.lower()),
            )
        )
    document.status = "parsed"
    document.parse_note = ""
    return document
