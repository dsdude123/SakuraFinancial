"""settings-service: the platform's key-value configuration store.

Everything the user can configure at runtime lives here — LLM provider and
keys, trading-philosophy instructions for stock analysis, monthly-update skip
flags, the web UI password hash. Other services read it per-call through
sakura_common.settings_client, so a change in the UI applies immediately.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from sakura_common.llm import LLMClient, LLMConfig, LLMError, config_from_settings
from sakura_common.settings_client import MASK

from .db import Base, make_engine, make_session_factory
from .models import Setting


class SettingIn(BaseModel):
    value: str
    is_secret: bool = False


class LLMTestIn(BaseModel):
    """Optional overrides; anything omitted falls back to stored settings.
    Lets the settings page test a key before saving it."""

    provider: str | None = None
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None


def serialize(setting: Setting, reveal: bool = False) -> dict:
    return {
        "key": setting.key,
        "value": setting.value if (reveal or not setting.is_secret) else MASK,
        "is_secret": setting.is_secret,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


def create_app(database_url: str | None = None) -> FastAPI:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="SakuraFinancial settings-service", version="1.0")

    def get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/settings")
    def list_settings(db: Session = Depends(get_db)):
        rows = db.execute(select(Setting).order_by(Setting.key)).scalars().all()
        return [serialize(row) for row in rows]

    @app.get("/api/settings/{key}")
    def get_setting(key: str, reveal: bool = Query(False), db: Session = Depends(get_db)):
        row = db.get(Setting, key)
        if row is None:
            raise HTTPException(404, f"no setting {key!r}")
        return serialize(row, reveal=reveal)

    @app.put("/api/settings/{key}")
    def put_setting(key: str, body: SettingIn, db: Session = Depends(get_db)):
        row = db.get(Setting, key)
        value = body.value
        # A form that redisplays a masked secret will submit the mask back;
        # that means "keep the existing value", never store the mask itself.
        if value == MASK and row is not None and row.is_secret:
            value = row.value
        if row is None:
            row = Setting(key=key, value=value, is_secret=body.is_secret)
            db.add(row)
        else:
            row.value = value
            row.is_secret = body.is_secret
        db.commit()
        return serialize(row)

    @app.delete("/api/settings/{key}")
    def delete_setting(key: str, db: Session = Depends(get_db)):
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
            db.commit()
        return {"deleted": key}

    @app.post("/api/llm/test")
    def llm_test(body: LLMTestIn, db: Session = Depends(get_db)):
        """Validate an LLM configuration with a one-token round trip."""

        class DbSettings:
            def get(self, key, default=None):
                row = db.get(Setting, key)
                return row.value if row is not None and row.value else default

        stored = DbSettings()
        try:
            base = config_from_settings(stored) if body.provider is None else None
        except LLMError:
            base = None
        config = LLMConfig(
            provider=body.provider or (base.provider if base else stored.get("llm.provider") or ""),
            api_key=(body.api_key if body.api_key not in (None, MASK) else None)
            or (base.api_key if base else stored.get("llm.api_key") or ""),
            model=body.model or (base.model if base else stored.get("llm.model") or ""),
            base_url=body.base_url or (base.base_url if base else stored.get("llm.base_url")),
        )
        try:
            config.validate()
            reply = LLMClient(config, timeout=30).complete(
                "Reply with exactly the word OK.", max_tokens=10
            )
        except LLMError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "reply": reply.strip()[:100]}

    @app.get("/api/export")
    def export(db: Session = Depends(get_db)):
        rows = db.execute(select(Setting).order_by(Setting.key)).scalars().all()
        return {
            "service": "settings",
            "settings": [
                {"key": row.key, "value": row.value, "is_secret": row.is_secret} for row in rows
            ],
        }

    @app.post("/api/import")
    def import_(data: dict, db: Session = Depends(get_db)):
        rows = data.get("settings")
        if not isinstance(rows, list):
            raise HTTPException(422, "expected {'settings': [...]} export format")
        for entry in rows:
            if not isinstance(entry, dict) or "key" not in entry:
                raise HTTPException(422, f"bad settings entry: {entry!r}")
        db.query(Setting).delete()
        for entry in rows:
            db.add(
                Setting(
                    key=entry["key"],
                    value=entry.get("value", ""),
                    is_secret=bool(entry.get("is_secret", False)),
                )
            )
        db.commit()
        return {"imported": len(rows)}

    return app
