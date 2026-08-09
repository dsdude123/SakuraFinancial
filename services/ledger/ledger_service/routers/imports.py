from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.csvengine import CsvValidationError, parse_bank_csv

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
    include: bool | None = None
    transfer_account_id: int | None = None
    learn_alias: bool | None = None


class TransferRuleIn(BaseModel):
    pattern: str
    match_type: str = "prefix"
    account_id: int
    active: bool = True


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
        "active": rule.active,
    }


REQUIRED_KEYS = {"date_column", "description_column"}


def validate_profile_config(config: dict) -> None:
    missing = REQUIRED_KEYS - {k for k, v in config.items() if v not in (None, "")}
    if missing:
        raise HTTPException(422, f"profile config missing {sorted(missing)}")
    mode = config.get("amount_mode", "single")
    if mode == "single":
        if not config.get("amount_column"):
            raise HTTPException(422, "profile config missing amount_column")
    elif mode == "debit_credit":
        if not config.get("debit_column") or not config.get("credit_column"):
            raise HTTPException(422, "debit_credit mode needs debit_column and credit_column")
    else:
        raise HTTPException(422, f"amount_mode must be 'single' or 'debit_credit', not {mode!r}")


@router.get("/import/profiles")
def list_profiles(db: Session = Depends(get_db)):
    rows = db.execute(select(ImportProfile).order_by(ImportProfile.name)).scalars().all()
    return [profile_dict(p) for p in rows]


@router.post("/import/profiles")
def create_profile(body: ProfileIn, db: Session = Depends(get_db)):
    validate_profile_config(body.config)
    if body.account_id is not None and db.get(Account, body.account_id) is None:
        raise HTTPException(422, f"no account {body.account_id}")
    if db.execute(select(ImportProfile).where(ImportProfile.name == body.name)).scalar_one_or_none():
        raise HTTPException(409, f"profile {body.name!r} already exists")
    profile = ImportProfile(name=body.name, account_id=body.account_id, config=body.config)
    db.add(profile)
    db.commit()
    return profile_dict(profile)


@router.put("/import/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileIn, db: Session = Depends(get_db)):
    profile = db.get(ImportProfile, profile_id)
    if profile is None:
        raise HTTPException(404, f"no profile {profile_id}")
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
        raise HTTPException(409, "profile has import batches — keep it for history")
    db.delete(profile)
    db.commit()
    return {"deleted": profile_id}


@router.post("/import/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db)):
    """Validate the whole file and build a review batch. A file with ANY bad
    row is rejected with the complete error list and nothing is stored."""
    profile = db.get(ImportProfile, body.profile_id)
    if profile is None:
        raise HTTPException(404, f"no profile {body.profile_id}")
    account_id = body.account_id or profile.account_id
    if account_id is None:
        raise HTTPException(422, "no account: pass account_id or set one on the profile")
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(422, f"no account {account_id}")
    try:
        parsed_rows = parse_bank_csv(body.content, profile.config)
    except CsvValidationError as exc:
        raise HTTPException(
            422,
            {
                "message": "the file failed validation — fix the import profile (or the file) "
                "and upload again; nothing was imported",
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
            raise HTTPException(422, "month must look like 2026-08")
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
        raise HTTPException(404, f"no batch {batch_id}")
    return import_logic.batch_dict(batch)


@router.put("/import/rows/{row_id}")
def update_row(row_id: int, body: RowUpdate, db: Session = Depends(get_db)):
    from ..models import ImportRow

    row = db.get(ImportRow, row_id)
    if row is None:
        raise HTTPException(404, f"no import row {row_id}")
    if row.batch.status != "review":
        raise HTTPException(422, f"batch is {row.batch.status}, rows can no longer change")
    if body.create_payee_name:
        name = body.create_payee_name.strip()
        payee = db.execute(select(Payee).where(Payee.name == name)).scalar_one_or_none()
        if payee is None:
            payee = Payee(name=name, default_category_id=body.category_id)
            db.add(payee)
            db.flush()
        row.payee_id = payee.id
        row.learn_alias = True if body.learn_alias is None else body.learn_alias
        if row.status == "needs_payee":
            row.status = "ready"
    elif body.payee_id is not None:
        if db.get(Payee, body.payee_id) is None:
            raise HTTPException(422, f"no payee {body.payee_id}")
        row.payee_id = body.payee_id
        row.learn_alias = True if body.learn_alias is None else body.learn_alias
        if row.status == "needs_payee":
            row.status = "ready"
        if body.category_id is None and row.category_id is None:
            row.category_id = db.get(Payee, body.payee_id).default_category_id
    if body.category_id is not None:
        if db.get(Category, body.category_id) is None:
            raise HTTPException(422, f"no category {body.category_id}")
        row.category_id = body.category_id
    if body.transfer_account_id is not None:
        if db.get(Account, body.transfer_account_id) is None:
            raise HTTPException(422, f"no account {body.transfer_account_id}")
        row.transfer_account_id = body.transfer_account_id
        row.status = "transfer"
    if body.include is not None:
        row.include = body.include
    if body.learn_alias is not None:
        row.learn_alias = body.learn_alias
    db.commit()
    return import_logic.row_dict(row)


@router.post("/import/batches/{batch_id}/commit")
def commit(batch_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}")
    if batch.status != "review":
        raise HTTPException(422, f"batch already {batch.status}")
    summary = import_logic.commit_batch(db, batch)
    db.commit()
    background.add_task(import_logic.ping_receipts_service)
    return summary


@router.post("/import/batches/{batch_id}/abort")
def abort(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}")
    if batch.status != "review":
        raise HTTPException(422, f"batch already {batch.status}")
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
        raise HTTPException(422, "match_type must be 'prefix' or 'contains'")
    if db.get(Account, body.account_id) is None:
        raise HTTPException(422, f"no account {body.account_id}")
    if not body.pattern.strip():
        raise HTTPException(422, "pattern is required")
    rule = TransferRule(
        pattern=body.pattern.strip(),
        match_type=body.match_type,
        account_id=body.account_id,
        active=body.active,
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
        raise HTTPException(422, "match_type must be 'prefix' or 'contains'")
    if db.get(Account, body.account_id) is None:
        raise HTTPException(422, f"no account {body.account_id}")
    rule.pattern = body.pattern.strip()
    rule.match_type = body.match_type
    rule.account_id = body.account_id
    rule.active = body.active
    db.commit()
    return rule_dict(rule)


@router.delete("/transfer-rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(TransferRule, rule_id)
    if rule is not None:
        db.delete(rule)
        db.commit()
    return {"deleted": rule_id}
