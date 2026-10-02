"""Row -> dict serializers for API responses.

All monetary amounts are serialized as **strings** ("1234.50") so no precision
is lost in JSON; every client (web-ui, budget, receipts) parses them back into
Decimal. Dates serialize as ISO YYYY-MM-DD.
"""

from __future__ import annotations

from sakura_common.money import money_str

from .models import Account, Category, FxRate, Payee, PayeeAlias, Split, Transaction


def account_dict(account: Account, balance=None) -> dict:
    data = {
        "id": account.id,
        "name": account.name,
        "type": account.type,
        "currency_code": account.currency_code,
        "opening_balance": money_str(account.opening_balance),
        "active": account.active,
        "note": account.note,
    }
    if balance is not None:
        data["balance"] = money_str(balance)
    return data


def category_dict(category: Category) -> dict:
    return {
        "id": category.id,
        "name": category.name,
        # Full "Food: Groceries" label — what every picker should display, so
        # a subcategory is never mistaken for a top-level one.
        "path": category_path(category),
        "parent_id": category.parent_id,
        "parent_name": category.parent.name if category.parent else None,
        "kind": category.kind,
        "active": category.active,
    }


def category_path(category: Category | None) -> str:
    if category is None:
        return ""
    if category.parent is not None:
        return f"{category.parent.name}: {category.name}"
    return category.name


def category_sort_key(category: Category) -> tuple:
    """Order categories so children follow their parent: income/expense
    grouping first, then the parent's name, then parent before children."""
    top_name = category.parent.name if category.parent else category.name
    return (category.kind, top_name.lower(), 1 if category.parent else 0, category.name.lower())


def payee_dict(payee: Payee, last_amount=None, last_category_id=None) -> dict:
    return {
        "id": payee.id,
        "name": payee.name,
        "default_category_id": payee.default_category_id,
        "active": payee.active,
        "last_amount": money_str(last_amount),
        "last_category_id": last_category_id,
    }


def alias_dict(alias: PayeeAlias) -> dict:
    return {
        "id": alias.id,
        "payee_id": alias.payee_id,
        "pattern": alias.pattern,
        "match_type": alias.match_type,
    }


def fx_dict(rate: FxRate) -> dict:
    return {
        "id": rate.id,
        "date": rate.date.isoformat(),
        "from_code": rate.from_code,
        "to_code": rate.to_code,
        "rate": money_str(rate.rate),
    }


def split_dict(split: Split) -> dict:
    return {
        "id": split.id,
        "category_id": split.category_id,
        "category_name": category_path(split.category),
        "amount": money_str(split.amount),
        "memo": split.memo,
    }


def transaction_dict(txn: Transaction) -> dict:
    return {
        "id": txn.id,
        "account_id": txn.account_id,
        "account_name": txn.account.name if txn.account else None,
        "currency_code": txn.account.currency_code if txn.account else None,
        "date": txn.date.isoformat(),
        "payee_id": txn.payee_id,
        "payee_name": txn.payee.name if txn.payee else None,
        "memo": txn.memo,
        "status": txn.status,
        "kind": txn.kind,
        "transfer_group_id": txn.transfer_group_id,
        "external_account": txn.external_account,
        "total": money_str(txn.total),
        "splits": [split_dict(split) for split in txn.splits],
    }
