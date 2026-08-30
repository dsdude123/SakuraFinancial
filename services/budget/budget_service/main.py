"""budget-service: envelope budgets with auto-assignment, deficit carryover,
and savings goals. Derives all actuals live from the ledger service."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sakura_common import jsonutil
from sakura_common.money import money_str
from sakura_common.errors import install_error_handler
from sakura_common.schema import sync_schema

from . import logic
from .db import Base, get_db, get_ledger, make_engine, make_session_factory
from .ledger_client import LedgerClient, LedgerUnavailable
from .models import CategoryBudget, Goal


class BudgetIn(BaseModel):
    amount: Decimal


class GoalIn(BaseModel):
    name: str
    target_amount: Decimal
    monthly_contribution: Decimal
    target_date: dt.date | None = None
    priority: int = 1
    note: str = ""


class GoalUpdate(BaseModel):
    name: str | None = None
    target_amount: Decimal | None = None
    monthly_contribution: Decimal | None = None
    target_date: dt.date | None = None
    priority: int | None = None
    active: bool | None = None
    note: str | None = None


def goal_dict(goal: Goal) -> dict:
    return {
        "id": goal.id,
        "name": goal.name,
        "target_amount": money_str(goal.target_amount),
        "monthly_contribution": money_str(goal.monthly_contribution),
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "priority": goal.priority,
        "active": goal.active,
        "note": goal.note,
    }


def parse_month_or_422(month: str) -> dt.date:
    try:
        return logic.parse_month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def create_app(database_url: str | None = None, ledger_client=None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    sync_schema(engine, Base, service="budget")

    app = FastAPI(title="SakuraFinancial budget-service", version="1.0")
    install_error_handler(app, "budget")
    app.state.session_factory = make_session_factory(engine)
    app.state.ledger_client = ledger_client or LedgerClient()

    @app.exception_handler(LedgerUnavailable)
    def ledger_unavailable(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/budget/{month}")
    def get_month(month: str, db: Session = Depends(get_db), ledger=Depends(get_ledger)):
        return logic.month_view(db, ledger, parse_month_or_422(month))

    def change_point(db: Session, month_start: dt.date, category_id: int):
        return db.execute(
            select(CategoryBudget).where(
                CategoryBudget.month == month_start, CategoryBudget.category_id == category_id
            )
        ).scalar_one_or_none()

    def plan_entry(db: Session, month_start: dt.date, category_id: int) -> dict:
        amount, since = logic.resolve_plan(db, month_start).get(category_id, (None, None))
        return {
            "month": f"{month_start.year:04d}-{month_start.month:02d}",
            "category_id": category_id,
            "amount": money_str(amount) if amount is not None else "0.00",
            "budgeted": amount is not None,
            "since": f"{since.year:04d}-{since.month:02d}" if since else None,
        }

    @app.put("/api/budget/{month}/categories/{category_id}")
    def set_budget(
        month: str, category_id: int, body: BudgetIn, db: Session = Depends(get_db)
    ):
        """Set this category's budget from ``month`` onward.

        The amount holds for every later month too, until another change
        supersedes it. An amount of 0 stops the category from this month on
        rather than deleting anything — history keeps whatever it had."""
        month_start = parse_month_or_422(month)
        if body.amount < 0:
            raise HTTPException(422, "budget amount cannot be negative")
        row = change_point(db, month_start, category_id)
        already_absent = category_id not in logic.resolve_plan(db, month_start)
        if body.amount == 0 and row is None and already_absent:
            return plan_entry(db, month_start, category_id)  # nothing to stop
        if row is None:
            db.add(CategoryBudget(month=month_start, category_id=category_id, amount=body.amount))
        else:
            row.amount = body.amount
        db.commit()
        return plan_entry(db, month_start, category_id)

    @app.delete("/api/budget/{month}/categories/{category_id}")
    def clear_change(month: str, category_id: int, db: Session = Depends(get_db)):
        """Undo the change made in this month, so it inherits again from
        whatever the previous change said. Not the same as budgeting 0, which
        stops the category going forward."""
        month_start = parse_month_or_422(month)
        row = change_point(db, month_start, category_id)
        if row is None:
            raise HTTPException(
                404,
                f"category {category_id} has no budget change in {month} to undo - "
                "it is inheriting from an earlier month",
            )
        db.delete(row)
        db.commit()
        return plan_entry(db, month_start, category_id)

    @app.get("/api/budget/{month}/categories/{category_id}")
    def get_budget(month: str, category_id: int, db: Session = Depends(get_db)):
        month_start = parse_month_or_422(month)
        entry = plan_entry(db, month_start, category_id)
        entry["changed_here"] = change_point(db, month_start, category_id) is not None
        return entry

    @app.get("/api/goals")
    def list_goals(include_inactive: bool = False, db: Session = Depends(get_db)):
        query = select(Goal).order_by(Goal.priority, Goal.id)
        if not include_inactive:
            query = query.where(Goal.active)
        return [goal_dict(g) for g in db.execute(query).scalars().all()]

    @app.post("/api/goals")
    def create_goal(body: GoalIn, db: Session = Depends(get_db)):
        if body.target_amount <= 0 or body.monthly_contribution <= 0:
            raise HTTPException(422, "target and monthly contribution must be positive")
        goal = Goal(
            name=body.name,
            target_amount=body.target_amount,
            monthly_contribution=body.monthly_contribution,
            target_date=body.target_date,
            priority=body.priority,
            note=body.note,
        )
        db.add(goal)
        db.commit()
        return goal_dict(goal)

    @app.put("/api/goals/{goal_id}")
    def update_goal(goal_id: int, body: GoalUpdate, db: Session = Depends(get_db)):
        goal = db.get(Goal, goal_id)
        if goal is None:
            raise HTTPException(404, f"no goal {goal_id}")
        for field in ("name", "target_amount", "monthly_contribution", "target_date", "priority", "active", "note"):
            value = getattr(body, field)
            if value is not None:
                setattr(goal, field, value)
        db.commit()
        return goal_dict(goal)

    @app.delete("/api/goals/{goal_id}")
    def delete_goal(goal_id: int, db: Session = Depends(get_db)):
        goal = db.get(Goal, goal_id)
        if goal is not None:
            db.delete(goal)
            db.commit()
        return {"deleted": goal_id}

    @app.get("/api/export")
    def export(db: Session = Depends(get_db)):
        return {
            "service": "budget",
            "category_budgets": [
                {
                    "id": row.id,
                    "month": row.month.isoformat(),
                    "category_id": row.category_id,
                    "amount": money_str(row.amount),
                }
                for row in db.execute(
                    select(CategoryBudget).order_by(CategoryBudget.id)
                ).scalars()
            ],
            "goals": [
                {**goal_dict(goal)}
                for goal in db.execute(select(Goal).order_by(Goal.id)).scalars()
            ],
        }

    @app.post("/api/reset")
    def reset(db: Session = Depends(get_db)):
        """Erase every record and come back up as a fresh install."""
        deleted = {
            "category_budgets": db.execute(text("DELETE FROM category_budgets")).rowcount,
            "goals": db.execute(text("DELETE FROM goals")).rowcount,
        }
        db.commit()
        return {"reset": "budget", "deleted": deleted}

    @app.post("/api/import")
    def import_(data: dict, db: Session = Depends(get_db)):
        for key in ("category_budgets", "goals"):
            if not isinstance(data.get(key), list):
                raise HTTPException(422, f"export data missing list {key!r}")
        db.execute(text("DELETE FROM category_budgets"))
        db.execute(text("DELETE FROM goals"))
        for row in data["category_budgets"]:
            db.add(
                CategoryBudget(
                    id=row["id"],
                    month=jsonutil.parse_date(row["month"]),
                    category_id=row["category_id"],
                    amount=jsonutil.parse_decimal(row["amount"]),
                )
            )
        for row in data["goals"]:
            db.add(
                Goal(
                    id=row["id"],
                    name=row["name"],
                    target_amount=jsonutil.parse_decimal(row["target_amount"]),
                    monthly_contribution=jsonutil.parse_decimal(row["monthly_contribution"]),
                    target_date=jsonutil.parse_date(row.get("target_date")),
                    priority=row.get("priority", 1),
                    active=row.get("active", True),
                    note=row.get("note", ""),
                )
            )
        if db.get_bind().dialect.name == "postgresql":
            for table in ("category_budgets", "goals"):
                db.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
                    )
                )
        db.commit()
        return {
            "imported": {
                "category_budgets": len(data["category_budgets"]),
                "goals": len(data["goals"]),
            }
        }

    return app
