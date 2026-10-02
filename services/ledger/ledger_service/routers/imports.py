from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.csvengine import CsvValidationError, parse_bank_csv
from sakura_common.dedup import normalize_description

from ..db import get_db
from ..logic import imports as import_logic
from ..models import (
    ALIAS_MATCH_TYPES,
    Account,
    Category,
    ImportBatch,
    ImportProfile,
    Payee,
    TransferRule,
)

router = APIRouter(prefix="/api", tags=["imports"])


class ProfileIn(BaseModel):
    name: str
    account_id: int | None = None
    config: dict


class PreviewIn(BaseModel):
    profile_id: int
    account_id: int | None = None
    filename: str = ""
    content: str


class RowUpdate(BaseModel):
    payee_id: int | None = None
    create_payee_name: str | None = None
    category_id: int | None = None
    # A category invented on the review screen: bare name or "Parent: Child".
    create_category_name: str | None = None
    create_category_kind: str | None = None
    include: bool | None = None
    transfer_account_id: int | None = None
    # The counterparty when it isn't a ledger account ("stock:1"); setting one
    # clears the other, since a row has exactly one far side.
    transfer_external_account: str | None = None
    learn_alias: bool | None = None


class GroupResolve(RowUpdate):
    """Answer every row in a batch that shares one bank description."""

    description: str


class TransferRuleIn(BaseModel):
    pattern: str
    match_type: str = "prefix"
    # Exactly one of these: a ledger account, or an account another service owns
    # ("stock:1" for an investment account).
    account_id: int | None = None
    external_account: str | None = None
    active: bool = True
    # Days of slack when matching this transfer against the other account's
    # already-imported leg; the two banks rarely post on the same date.
    match_days: int = 5


def profile_dict(profile: ImportProfile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "account_id": profile.account_id,
        "config": profile.config,
    }


def rule_dict(rule: TransferRule) -> dict:
    return {
        "id": rule.id,
        "pattern": rule.pattern,
        "match_type": rule.match_type,
        "account_id": rule.account_id,
        "account_name": rule.account.name if rule.account else None,
        # An external counterparty's name belongs to the service that owns it,
        # so callers resolve "stock:1" for display themselves.
        "external_account": rule.external_account,
        "active": rule.active,
        "match_days": rule.match_days,
    }


EXTERNAL_ACCOUNT_SERVICES = ("stock",)


def clean_external_account(value: str | None) -> str | None:
    """Validate a counterparty ref another service owns ("stock:1").

    Whether that account exists is the other service's business — this service
    has no way to ask, and refusing to store a ref it can't verify would make
    the seam worse, not safer. The shape is checked so a typo can't become a
    transfer that settles nowhere.
    """
    text = (value or "").strip()
    if not text:
        return None
    service, _, ident = text.partition(":")
    if service not in EXTERNAL_ACCOUNT_SERVICES or not ident.isdigit():
        raise HTTPException(
            422,
            f"{text!r} is not an account in another service; expected one of "
            + ", ".join(f'"{name}:<id>"' for name in EXTERNAL_ACCOUNT_SERVICES),
        )
    return f"{service}:{int(ident)}"


def rule_target(db: Session, body: TransferRuleIn) -> tuple[int | None, str | None]:
    """The counter account a rule points at: a ledger account or an external
    ref, never both and never neither."""
    external = clean_external_account(body.external_account)
    if body.account_id is not None and external is not None:
        raise HTTPException(
            422, "A transfer rule points at one counter account, not both a bank and an investment one."
        )
    if body.account_id is None and external is None:
        raise HTTPException(422, "A transfer rule needs a counter account.")
    if body.account_id is not None and db.get(Account, body.account_id) is None:
        raise HTTPException(422, f"Account {body.account_id} no longer exists.")
    return body.account_id, external


REQUIRED_KEYS = {"date_column", "description_column"}


def validate_profile_config(config: dict) -> None:
    missing = REQUIRED_KEYS - {k for k, v in config.items() if v not in (None, "")}
    if missing:
        raise HTTPException(
            422,
            "The profile is missing "
            + " and ".join(f"a {key.replace('_', ' ')}" for key in sorted(missing))
            + ".",
        )
    mode = config.get("amount_mode", "single")
    if mode == "single":
        if not config.get("amount_column"):
            raise HTTPException(422, "The profile needs an amount column.")
    elif mode == "debit_credit":
        if not config.get("debit_column") or not config.get("credit_column"):
            raise HTTPException(
                422, "Separate debit/credit mode needs both a debit and a credit column."
            )
    else:
        raise HTTPException(
            422,
            f"Amount style must be one signed column or separate debit/credit, not {mode!r}.",
        )


@router.get("/import/profiles")
def list_profiles(db: Session = Depends(get_db)):
    rows = db.execute(select(ImportProfile).order_by(ImportProfile.name)).scalars().all()
    return [profile_dict(p) for p in rows]


@router.post("/import/profiles")
def create_profile(body: ProfileIn, db: Session = Depends(get_db)):
    validate_profile_config(body.config)
    if body.account_id is not None and db.get(Account, body.account_id) is None:
        raise HTTPException(422, f"Account {body.account_id} no longer exists.")
    if db.execute(select(ImportProfile).where(ImportProfile.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, f"A profile named {body.name!r} already exists.")
    profile = ImportProfile(name=body.name, account_id=body.account_id, config=body.config)
    db.add(profile)
    db.commit()
    return profile_dict(profile)


@router.put("/import/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileIn, db: Session = Depends(get_db)):
    profile = db.get(ImportProfile, profile_id)
    if profile is None:
        raise HTTPException(404, f"Import profile {profile_id} no longer exists.")
    validate_profile_config(body.config)
    profile.name = body.name
    profile.account_id = body.account_id
    profile.config = body.config
    db.commit()
    return profile_dict(profile)


@router.delete("/import/profiles/{profile_id}")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.get(ImportProfile, profile_id)
    if profile is None:
        return {"deleted": profile_id}
    in_use = db.execute(
        select(ImportBatch.id).where(ImportBatch.profile_id == profile_id).limit(1)
    ).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(
            409, "That profile has import batches; it is kept so their history stays readable."
        )
    db.delete(profile)
    db.commit()
    return {"deleted": profile_id}


@router.post("/import/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db)):
    """Validate the whole file and build a review batch. A file with ANY bad
    row is rejected with the complete error list and nothing is stored."""
    profile = db.get(ImportProfile, body.profile_id)
    if profile is None:
        raise HTTPException(404, f"Import profile {body.profile_id} no longer exists.")
    account_id = body.account_id or profile.account_id
    if account_id is None:
        raise HTTPException(
            422,
            "This profile has no default account, so the import needs you to choose "
            "one on the upload form.",
        )
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(422, f"Account {account_id} no longer exists.")
    try:
        parsed_rows = parse_bank_csv(body.content, profile.config)
    except CsvValidationError as exc:
        raise HTTPException(
            422,
            {
                "message": "The file did not pass validation, so nothing was imported. "
                "Fix the import profile (or the file) and upload it again.",
                "errors": exc.as_dicts(),
            },
        )
    batch = import_logic.build_batch(
        db, account=account, profile_id=profile.id, filename=body.filename, parsed_rows=parsed_rows
    )
    db.commit()
    return import_logic.batch_dict(batch)


@router.get("/import/batches")
def list_batches(
    account_id: int | None = None,
    month: str | None = None,
    db: Session = Depends(get_db),
):
    query = select(ImportBatch).order_by(ImportBatch.created_at.desc())
    if account_id is not None:
        query = query.where(ImportBatch.account_id == account_id)
    batches = db.execute(query).scalars().all()
    if month is not None:
        try:
            year, mon = (int(part) for part in month.split("-"))
        except ValueError:
            raise HTTPException(422, "The month must look like 2026-08.")
        batches = [
            b
            for b in batches
            if b.created_at is not None
            and (b.created_at.year, b.created_at.month) == (year, mon)
        ]
    return [import_logic.batch_dict(b, with_rows=False) for b in batches]


@router.get("/import/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"Import batch {batch_id} no longer exists.")
    return import_logic.batch_dict(batch)


def resolve_category(db: Session, row, body: RowUpdate) -> int | None:
    """The category this update selects, creating it first when the user typed
    a new one. Returns None when the update doesn't touch the category."""
    if body.create_category_name and body.create_category_name.strip():
        # Income and expense categories live in separate trees; when the caller
        # doesn't say which, the row's own sign is the reliable answer.
        kind = body.create_category_kind or ("income" if row.amount > 0 else "expense")
        try:
            category = import_logic.resolve_or_create_category(
                db, body.create_category_name.strip(), kind
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return category.id
    if body.category_id is not None:
        if db.get(Category, body.category_id) is None:
            raise HTTPException(422, f"Category {body.category_id} no longer exists.")
        return body.category_id
    return None


def apply_row_update(db: Session, row, body: RowUpdate) -> None:
    """Apply one review answer to one row. Shared by the single-row edit and the
    resolve-by-description bulk action so both behave identically."""
    category_id = resolve_category(db, row, body)
    if body.create_payee_name and body.create_payee_name.strip():
        name = body.create_payee_name.strip()
        payee = db.execute(select(Payee).where(Payee.name == name)).scalar_one_or_none()
        if payee is None:
            payee = Payee(name=name, default_category_id=category_id)
            db.add(payee)
            db.flush()
        elif payee.default_category_id is None and category_id is not None:
            payee.default_category_id = category_id
        row.payee_id = payee.id
        row.learn_alias = True if body.learn_alias is None else body.learn_alias
        if row.status == "needs_payee":
            row.status = "ready"
    elif body.payee_id is not None:
        payee = db.get(Payee, body.payee_id)
        if payee is None:
            raise HTTPException(422, f"Payee {body.payee_id} no longer exists.")
        row.payee_id = body.payee_id
        row.learn_alias = True if body.learn_alias is None else body.learn_alias
        if row.status == "needs_payee":
            row.status = "ready"
        if category_id is None and row.category_id is None:
            row.category_id = payee.default_category_id
    if category_id is not None:
        row.category_id = category_id
    if body.transfer_account_id is not None:
        if db.get(Account, body.transfer_account_id) is None:
            raise HTTPException(422, f"Account {body.transfer_account_id} no longer exists.")
        row.transfer_account_id = body.transfer_account_id
        row.transfer_external_account = None
        row.status = "transfer"
    external_account = clean_external_account(body.transfer_external_account)
    if external_account is not None:
        row.transfer_external_account = external_account
        row.transfer_account_id = None
        row.status = "transfer"
    if body.include is not None:
        row.include = body.include
    if body.learn_alias is not None:
        row.learn_alias = body.learn_alias


@router.put("/import/rows/{row_id}")
def update_row(row_id: int, body: RowUpdate, db: Session = Depends(get_db)):
    from ..models import ImportRow

    row = db.get(ImportRow, row_id)
    if row is None:
        raise HTTPException(404, f"Import row {row_id} no longer exists.")
    if row.batch.status != "review":
        raise HTTPException(
            422,
            f"That batch has already been {row.batch.status}; its rows can no longer change.",
        )
    apply_row_update(db, row, body)
    db.commit()
    return import_logic.row_dict(row)


@router.post("/import/batches/{batch_id}/resolve")
def resolve_group(batch_id: int, body: GroupResolve, db: Session = Depends(get_db)):
    """Answer every row in the batch sharing one bank description at once.

    Fifty unknown rows are usually five merchants; this is the endpoint that
    lets the user say it five times instead of fifty."""
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"Import batch {batch_id} no longer exists.")
    if batch.status != "review":
        raise HTTPException(
            422, f"That batch has already been {batch.status}; its rows can no longer change."
        )
    target = normalize_description(body.description)
    matched = [r for r in batch.rows if normalize_description(r.description) == target]
    if not matched:
        raise HTTPException(404, f"no rows in this batch describe {body.description!r}")
    for row in matched:
        apply_row_update(db, row, body)
    db.commit()
    return {
        "batch_id": batch_id,
        "description": body.description,
        "updated": len(matched),
        "rows": [import_logic.row_dict(r) for r in matched],
    }


@router.post("/import/batches/{batch_id}/reclassify")
def reclassify(batch_id: int, db: Session = Depends(get_db)):
    """Re-run classification over a batch still in review, picking up payee
    aliases and transfer rules added since it was uploaded. Rows already
    answered are untouched, so no manual work is lost and there is no need to
    discard the batch and upload the file again."""
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"Import batch {batch_id} no longer exists.")
    if batch.status != "review":
        raise HTTPException(
            422, f"That batch has already been {batch.status}; its rows can no longer change."
        )
    result = import_logic.reclassify_batch(db, batch)
    db.commit()
    return result


@router.post("/import/batches/{batch_id}/commit")
def commit(
    batch_id: int,
    background: BackgroundTasks,
    name_payees_from_descriptions: bool = True,
    db: Session = Depends(get_db),
):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"Import batch {batch_id} no longer exists.")
    if batch.status != "review":
        raise HTTPException(422, f"That batch has already been {batch.status}.")
    summary = import_logic.commit_batch(
        db, batch, name_payees_from_descriptions=name_payees_from_descriptions
    )
    db.commit()
    background.add_task(import_logic.ping_receipts_service)
    return summary


@router.post("/import/batches/{batch_id}/abort")
def abort(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"Import batch {batch_id} no longer exists.")
    if batch.status != "review":
        raise HTTPException(422, f"That batch has already been {batch.status}.")
    batch.status = "aborted"
    db.commit()
    return {"aborted": batch_id}


@router.get("/transfer-rules")
def list_rules(db: Session = Depends(get_db)):
    rules = db.execute(select(TransferRule).order_by(TransferRule.pattern)).scalars().all()
    return [rule_dict(r) for r in rules]


@router.post("/transfer-rules")
def create_rule(body: TransferRuleIn, db: Session = Depends(get_db)):
    if body.match_type not in ("prefix", "contains"):
        raise HTTPException(422, "A transfer rule must match by prefix or by contains.")
    account_id, external_account = rule_target(db, body)
    if not body.pattern.strip():
        raise HTTPException(422, "A transfer rule needs a pattern to match against.")
    if body.match_days < 0:
        raise HTTPException(422, "The matching window cannot be negative.")
    rule = TransferRule(
        pattern=body.pattern.strip(),
        match_type=body.match_type,
        account_id=account_id,
        external_account=external_account,
        active=body.active,
        match_days=body.match_days,
    )
    db.add(rule)
    db.commit()
    return rule_dict(rule)


@router.put("/transfer-rules/{rule_id}")
def update_rule(rule_id: int, body: TransferRuleIn, db: Session = Depends(get_db)):
    rule = db.get(TransferRule, rule_id)
    if rule is None:
        raise HTTPException(404, f"no transfer rule {rule_id}")
    if body.match_type not in ("prefix", "contains"):
        raise HTTPException(422, "A transfer rule must match by prefix or by contains.")
    account_id, external_account = rule_target(db, body)
    if body.match_days < 0:
        raise HTTPException(422, "The matching window cannot be negative.")
    rule.pattern = body.pattern.strip()
    rule.match_type = body.match_type
    rule.account_id = account_id
    rule.external_account = external_account
    rule.active = body.active
    rule.match_days = body.match_days
    db.commit()
    return rule_dict(rule)


@router.delete("/transfer-rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(TransferRule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    return {"deleted": rule_id}
