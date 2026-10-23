"""Helpers around the sync_state table (incremental cursors + run status)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.utils import as_aware, utcnow
from app.models import SyncState

logger = logging.getLogger(__name__)

FULL = "full"
INCREMENTAL = "incremental"
MODES = (FULL, INCREMENTAL)


def get_state(session: Session, source: str, entity: str) -> SyncState:
    state = session.scalar(select(SyncState).where(SyncState.source == source, SyncState.entity == entity))
    if state is None:
        state = SyncState(source=source, entity=entity)
        session.add(state)
        session.flush()
    return state


def since_for(state: SyncState, mode: str, overlap_seconds: int) -> datetime | None:
    """Lower bound for the "updated since" filter; None means fetch everything."""
    cursor = as_aware(state.cursor)
    if mode != INCREMENTAL or cursor is None:
        return None
    return cursor - timedelta(seconds=overlap_seconds)


def max_dt(current: datetime | None, candidate: datetime | None) -> datetime | None:
    current, candidate = as_aware(current), as_aware(candidate)
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def mark_started(session: Session, state: SyncState, mode: str) -> None:
    state.last_mode = mode
    state.last_status = "running"
    state.last_error = None
    state.last_started_at = utcnow()
    session.commit()


def mark_finished(session: Session, state: SyncState, cursor: datetime | None, items: int) -> None:
    state.cursor = max_dt(state.cursor, cursor)
    state.last_status = "ok"
    state.last_items = items
    state.last_finished_at = utcnow()
    session.commit()


def mark_failed(session: Session, state: SyncState, error: BaseException) -> None:
    session.rollback()
    state.last_status = "error"
    # Only the exception type and the (already non-secret) message; truncated.
    state.last_error = f"{type(error).__name__}: {error}"[:1000]
    state.last_finished_at = utcnow()
    session.commit()


@dataclass
class SyncReport:
    counts: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, key: str, n: int) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def skip(self, source: str, entity: str, n: int) -> None:
        if n:
            self.skipped[entity] = self.skipped.get(entity, 0) + n
            logger.warning("%s %s: skipped %d items without id", source, entity, n)


EntityFn = Callable[[datetime | None], tuple[int, datetime | None]]


def run_entity(
    session: Session, report: SyncReport, source: str, entity: str, mode: str,
    overlap_seconds: int, fn: EntityFn,
) -> None:
    """Run one entity sync, record status/cursor in sync_state; errors don't stop other entities."""
    state = get_state(session, source, entity)
    since = since_for(state, mode, overlap_seconds)
    mark_started(session, state, mode)
    logger.info("%s %s: %s sync%s", source, entity, mode, f" since {since.isoformat()}" if since else "")
    try:
        count, cursor = fn(since)
    except Exception as exc:  # noqa: BLE001 - recorded and reported
        mark_failed(session, state, exc)
        report.errors[entity] = f"{type(exc).__name__}: {exc}"
        logger.error("%s %s failed: %s", source, entity, exc)
        return
    mark_finished(session, state, cursor, count)
    report.counts[entity] = count
    logger.info("%s %s: %d rows", source, entity, count)
