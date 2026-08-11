"""Jinja2 environment + the filters templates lean on."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).parent / "templates"


def money(value, currency: str = "") -> str:
    """'1234.5' -> '1,234.50'. Falls back to the raw value on bad input so a
    template never 500s over formatting."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = f"{amount:,.2f}"
    return f"{text} {currency}" if currency else text


def is_negative(value) -> bool:
    try:
        return Decimal(str(value)) < 0
    except (InvalidOperation, TypeError):
        return False


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["money"] = money
    templates.env.tests["negative"] = is_negative
    return templates


def render(request: Request, name: str, context: dict | None = None):
    templates: Jinja2Templates = request.app.state.templates
    context = dict(context or {})
    context.setdefault("msg", request.query_params.get("msg", ""))
    context.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, name, context)
