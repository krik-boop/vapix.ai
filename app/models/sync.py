from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SyncState(Base):
    """Per-entity cursor for incremental sync."""

    __tablename__ = "sync_state"
    __table_args__ = (UniqueConstraint("source", "entity", name="uq_sync_state_source_entity"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))
    entity: Mapped[str] = mapped_column(String(64))
    # Max "last modified" timestamp seen from the API (server time).
    cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_mode: Mapped[str | None] = mapped_column(String(16))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_items: Mapped[int | None]
