from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.csvengine import CsvValidationError, parse_stock_csv

from ..db import get_db
from ..logic import imports as import_logic
from ..models import InvestmentAccount, StockImportBatch, StockImportProfile, StockImportRow

router = APIRouter(prefix="/api/import", tags=["imports"])


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
    include: bool


def profile_dict(profile: StockImportProfile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "account_id": profile.account_id,
        "config": profile.config,
    }


def validate_config(config: dict) -> None:
    for key in ("date_column", "action_column"):
        if not config.get(key):
            raise HTTPException(422, f"profile config missing {key}")
    action_map = config.get("action_map")
    if not isinstance(action_map, dict) or not action_map:
        raise HTTPException(
            422,
            "profile config needs a non-empty action_map "
            '(e.g. {"Bought": "buy", "YOU SOLD": "sell"})',
        )


@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db)):
    rows = db.execute(select(StockImportProfile).order_by(StockImportProfile.name)).scalars()
    return [profile_dict(p) for p in rows]


@router.post("/profiles")
def create_profile(body: ProfileIn, db: Session = Depends(get_db)):
    validate_config(body.config)
    if body.account_id is not None and db.get(InvestmentAccount, body.account_id) is None:
        raise HTTPException(422, f"no account {body.account_id}")
    clash = db.execute(
        select(StockImportProfile).where(StockImportProfile.name == body.name)
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, f"profile {body.name!r} already exists")
    profile = StockImportProfile(name=body.name, account_id=body.account_id, config=body.config)
    db.add(profile)
    db.commit()
    return profile_dict(profile)


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: int, body: ProfileIn, db: Session = Depends(get_db)):
    profile = db.get(StockImportProfile, profile_id)
    if profile is None:
        raise HTTPException(404, f"no profile {profile_id}")
    validate_config(body.config)
    profile.name = body.name
    profile.account_id = body.account_id
    profile.config = body.config
    db.commit()
    return profile_dict(profile)


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.get(StockImportProfile, profile_id)
    if profile is None:
        return {"deleted": profile_id}
    in_use = db.execute(
        select(StockImportBatch.id).where(StockImportBatch.profile_id == profile_id).limit(1)
    ).scalar_one_or_none()
    if in_use is not None:
        raise HTTPException(409, "profile has import batches - keep it for history")
    db.delete(profile)
    db.commit()
    return {"deleted": profile_id}


@router.post("/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db)):
    """All-or-nothing validation, including the action map: an unmapped
    broker transaction-type string rejects the whole file with instructions
    to extend the map. Nothing is written on failure."""
    profile = db.get(StockImportProfile, body.profile_id)
    if profile is None:
        raise HTTPException(404, f"no profile {body.profile_id}")
    account_id = body.account_id or profile.account_id
    if account_id is None:
        raise HTTPException(422, "no account: pass account_id or set one on the profile")
    account = db.get(InvestmentAccount, account_id)
    if account is None:
        raise HTTPException(422, f"no account {account_id}")
    try:
        parsed_rows = parse_stock_csv(body.content, profile.config)
    except CsvValidationError as exc:
        raise HTTPException(
            422,
            {
                "message": "the file failed validation — fix the import profile (usually the "
                "action map) and upload again; nothing was imported",
                "errors": exc.as_dicts(),
            },
        )
    batch = import_logic.build_batch(
        db, account=account, profile_id=profile.id, filename=body.filename, parsed_rows=parsed_rows
    )
    db.commit()
    return import_logic.batch_dict(batch)


@router.get("/batches")
def list_batches(
    account_id: int | None = None, month: str | None = None, db: Session = Depends(get_db)
):
    query = select(StockImportBatch).order_by(StockImportBatch.created_at.desc())
    if account_id is not None:
        query = query.where(StockImportBatch.account_id == account_id)
    batches = db.execute(query).scalars().all()
    if month is not None:
        try:
            year, mon = (int(part) for part in month.split("-"))
        except ValueError:
            raise HTTPException(422, "month must look like 2026-08")
        batches = [
            b
            for b in batches
            if b.created_at is not None and (b.created_at.year, b.created_at.month) == (year, mon)
        ]
    return [import_logic.batch_dict(b, with_rows=False) for b in batches]


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(StockImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}")
    return import_logic.batch_dict(batch)


@router.put("/rows/{row_id}")
def update_row(row_id: int, body: RowUpdate, db: Session = Depends(get_db)):
    row = db.get(StockImportRow, row_id)
    if row is None:
        raise HTTPException(404, f"no row {row_id}")
    if row.batch.status != "review":
        raise HTTPException(422, f"batch is {row.batch.status}")
    row.include = body.include
    db.commit()
    return import_logic.row_dict(row)


@router.post("/batches/{batch_id}/commit")
def commit(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(StockImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}")
    if batch.status != "review":
        raise HTTPException(422, f"batch already {batch.status}")
    summary = import_logic.commit_batch(db, batch)
    db.commit()
    return summary


@router.post("/batches/{batch_id}/abort")
def abort(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(StockImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, f"no batch {batch_id}")
    if batch.status != "review":
        raise HTTPException(422, f"batch already {batch.status}")
    batch.status = "aborted"
    db.commit()
    return {"aborted": batch_id}
