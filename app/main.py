"""HTTP API (read-only): health check and sync status."""

from fastapi import Depends, FastAPI
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import setup_logging
from app.models import SyncState

setup_logging(get_settings())
app = FastAPI(title="ai-business-os")


def get_session():
    with get_sessionmaker()() as session:
        yield session


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/sync/status")
def sync_status(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.scalars(select(SyncState).order_by(SyncState.source, SyncState.entity))
    return [
        {
            "source": s.source,
            "entity": s.entity,
            "cursor": s.cursor,
            "last_mode": s.last_mode,
            "last_status": s.last_status,
            "last_error": s.last_error,
            "last_started_at": s.last_started_at,
            "last_finished_at": s.last_finished_at,
            "last_items": s.last_items,
        }
        for s in rows
    ]
