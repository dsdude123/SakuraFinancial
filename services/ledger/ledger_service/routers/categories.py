from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CATEGORY_KINDS, Category, DEFAULT_CATEGORIES, Payee, Split
from ..serialize import category_dict

router = APIRouter(prefix="/api", tags=["categories"])


class CategoryIn(BaseModel):
    name: str
    kind: str = "expense"
    parent_id: int | None = None


class CategoryUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    active: bool | None = None


@router.get("/categories")
def list_categories(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = select(Category).order_by(Category.kind, Category.name)
    if not include_inactive:
        query = query.where(Category.active)
    return [category_dict(c) for c in db.execute(query).scalars().all()]


@router.post("/categories")
def create_category(body: CategoryIn, db: Session = Depends(get_db)):
    if body.kind not in CATEGORY_KINDS:
        raise HTTPException(422, f"kind must be one of {CATEGORY_KINDS}")
    if body.parent_id is not None:
        parent = db.get(Category, body.parent_id)
        if parent is None:
            raise HTTPException(422, f"no parent category {body.parent_id}")
        if parent.parent_id is not None:
            raise HTTPException(422, "categories can nest only one level deep")
        if parent.kind != body.kind:
            raise HTTPException(422, "subcategory kind must match its parent")
    clash = db.execute(
        select(Category).where(Category.name == body.name, Category.parent_id == body.parent_id)
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, f"category {body.name!r} already exists at this level")
    category = Category(name=body.name, kind=body.kind, parent_id=body.parent_id)
    db.add(category)
    db.commit()
    return category_dict(category)


@router.put("/categories/{category_id}")
def update_category(category_id: int, body: CategoryUpdate, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, f"no category {category_id}")
    if body.name is not None:
        category.name = body.name
    if body.parent_id is not None:
        if body.parent_id == category_id:
            raise HTTPException(422, "category cannot be its own parent")
        parent = db.get(Category, body.parent_id)
        if parent is None:
            raise HTTPException(422, f"no parent category {body.parent_id}")
        category.parent_id = body.parent_id
    if body.active is not None:
        category.active = body.active
    db.commit()
    return category_dict(category)


@router.delete("/categories/{category_id}")
def delete_category(category_id: int, db: Session = Depends(get_db)):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(404, f"no category {category_id}")
    in_use = db.execute(
        select(func.count()).select_from(Split).where(Split.category_id == category_id)
    ).scalar_one()
    used_as_default = db.execute(
        select(func.count()).select_from(Payee).where(Payee.default_category_id == category_id)
    ).scalar_one()
    has_children = db.execute(
        select(func.count()).select_from(Category).where(Category.parent_id == category_id)
    ).scalar_one()
    if in_use or used_as_default or has_children:
        raise HTTPException(
            409,
            "category is in use (splits, payee defaults, or subcategories) — "
            "deactivate it instead of deleting",
        )
    db.delete(category)
    db.commit()
    return {"deleted": category_id}


@router.post("/seed-defaults")
def seed_defaults(db: Session = Depends(get_db)):
    """Install the starter category set. Only allowed while the category list
    is empty, so it can never mangle real data."""
    existing = db.execute(select(func.count()).select_from(Category)).scalar_one()
    if existing:
        raise HTTPException(409, "categories already exist — refusing to seed defaults")
    created = 0
    for kind, names in DEFAULT_CATEGORIES.items():
        for name in names:
            db.add(Category(name=name, kind=kind))
            created += 1
    db.commit()
    return {"created": created}
