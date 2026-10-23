"""amoCRM -> PostgreSQL sync (full / incremental)."""

import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import upsert
from app.integrations import sync_state as st
from app.integrations.amo.client import AmoClient
from app.integrations.utils import from_unix, to_int, utcnow
from app.models import AmoLead, AmoLossReason, AmoNote, AmoPipeline, AmoStatus, AmoTask, AmoUser

logger = logging.getLogger(__name__)

SOURCE = "amo"


# --- mappers: tolerate missing fields, never raise on incomplete data ------------------

def map_user(raw: dict, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    rights = raw.get("rights") or {}
    return {
        "id": to_int(raw["id"]),
        "name": raw.get("name"),
        "lang": raw.get("lang"),
        "is_admin": rights.get("is_admin"),
        "is_active": rights.get("is_active"),
        "group_id": to_int(rights.get("group_id")),
        "synced_at": now,
    }


def map_pipeline(raw: dict, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    return {
        "id": to_int(raw["id"]),
        "name": raw.get("name"),
        "sort": to_int(raw.get("sort")),
        "is_main": raw.get("is_main"),
        "is_unsorted_on": raw.get("is_unsorted_on"),
        "is_archive": raw.get("is_archive"),
        "synced_at": now,
    }


def map_status(raw: dict, pipeline_id: int, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    return {
        "pipeline_id": to_int(raw.get("pipeline_id")) or pipeline_id,
        "id": to_int(raw["id"]),
        "name": raw.get("name"),
        "sort": to_int(raw.get("sort")),
        "is_editable": raw.get("is_editable"),
        "color": raw.get("color"),
        "type": to_int(raw.get("type")),
        "synced_at": now,
    }


def map_lead(raw: dict, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    embedded = raw.get("_embedded") or {}
    tags = [t.get("name") for t in embedded.get("tags") or [] if isinstance(t, dict) and t.get("name")]
    return {
        "id": to_int(raw["id"]),
        "name": raw.get("name"),
        "price": to_int(raw.get("price")),
        "pipeline_id": to_int(raw.get("pipeline_id")),
        "status_id": to_int(raw.get("status_id")),
        "responsible_user_id": to_int(raw.get("responsible_user_id")),
        "group_id": to_int(raw.get("group_id")),
        "loss_reason_id": to_int(raw.get("loss_reason_id")),
        "created_by": to_int(raw.get("created_by")),
        "updated_by": to_int(raw.get("updated_by")),
        "created_at": from_unix(raw.get("created_at")),
        "updated_at": from_unix(raw.get("updated_at")),
        "closed_at": from_unix(raw.get("closed_at")),
        "closest_task_at": from_unix(raw.get("closest_task_at")),
        "is_deleted": raw.get("is_deleted"),
        "tags": tags or None,
        "custom_fields_values": raw.get("custom_fields_values"),
        "synced_at": now,
    }


def loss_reasons_from_lead(raw: dict, now: datetime) -> list[dict]:
    # Documented as `_embedded[loss_reason]` (array of {id, name}) when with=loss_reason.
    items = (raw.get("_embedded") or {}).get("loss_reason") or []
    if isinstance(items, dict):
        items = [items]
    return [
        {"id": to_int(r.get("id")), "name": r.get("name"), "synced_at": now}
        for r in items
        if isinstance(r, dict) and to_int(r.get("id")) is not None
    ]


def map_task(raw: dict, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    result = raw.get("result") or {}
    return {
        "id": to_int(raw["id"]),
        "entity_type": raw.get("entity_type"),
        "entity_id": to_int(raw.get("entity_id")),
        "responsible_user_id": to_int(raw.get("responsible_user_id")),
        "group_id": to_int(raw.get("group_id")),
        "task_type_id": to_int(raw.get("task_type_id")),
        "text": raw.get("text"),
        "is_completed": raw.get("is_completed"),
        "complete_till": from_unix(raw.get("complete_till")),
        "duration": to_int(raw.get("duration")),
        "result_text": result.get("text") if isinstance(result, dict) else None,
        "created_by": to_int(raw.get("created_by")),
        "updated_by": to_int(raw.get("updated_by")),
        "created_at": from_unix(raw.get("created_at")),
        "updated_at": from_unix(raw.get("updated_at")),
        "synced_at": now,
    }


def map_note(raw: dict, entity_type: str, now: datetime) -> dict | None:
    if to_int(raw.get("id")) is None:
        return None
    return {
        "entity_type": entity_type,
        "id": to_int(raw["id"]),
        "entity_id": to_int(raw.get("entity_id")),
        "note_type": raw.get("note_type"),
        "params": raw.get("params"),
        "responsible_user_id": to_int(raw.get("responsible_user_id")),
        "group_id": to_int(raw.get("group_id")),
        "created_by": to_int(raw.get("created_by")),
        "updated_by": to_int(raw.get("updated_by")),
        "created_at": from_unix(raw.get("created_at")),
        "updated_at": from_unix(raw.get("updated_at")),
        "synced_at": now,
    }


# --- sync ---------------------------------------------------------------------------

def _map_all(raws: Iterable[dict], mapper: Callable[[dict], dict | None]) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for raw in raws:
        row = mapper(raw) if isinstance(raw, dict) else None
        if row is None:
            skipped += 1
        else:
            rows.append(row)
    return rows, skipped


class AmoSync:
    def __init__(self, session: Session, client: AmoClient, settings: Settings) -> None:
        self.session = session
        self.client = client
        self.settings = settings
        self.report = st.SyncReport()

    def _run(self, entity: str, mode: str, fn: st.EntityFn) -> None:
        st.run_entity(self.session, self.report, SOURCE, entity, mode, self.settings.sync_overlap_seconds, fn)

    def run(self, mode: str) -> st.SyncReport:
        if mode not in st.MODES:
            raise ValueError(f"Unknown mode {mode!r}")
        # Reference data is small and has no documented "updated since" filter: always full.
        self._run("users", st.FULL, self._sync_users)
        self._run("pipelines", st.FULL, self._sync_pipelines)
        self._run("leads", mode, self._sync_leads)
        self._run("tasks", mode, self._sync_tasks)
        self._run("notes_leads", mode, self._sync_notes)
        return self.report

    def _sync_users(self, _since: datetime | None) -> tuple[int, datetime | None]:
        now, total = utcnow(), 0
        for page in self.client.iter_users():
            rows, skipped = _map_all(page, lambda r: map_user(r, now))
            self.report.skip(SOURCE, "users", skipped)
            total += upsert(self.session, AmoUser, rows)
            self.session.commit()
        return total, None

    def _sync_pipelines(self, _since: datetime | None) -> tuple[int, datetime | None]:
        now = utcnow()
        pipelines = self.client.get_pipelines()
        rows, skipped = _map_all(pipelines, lambda r: map_pipeline(r, now))
        self.report.skip(SOURCE, "pipelines", skipped)
        statuses: list[dict] = []
        for raw in pipelines:
            pid = to_int(raw.get("id")) if isinstance(raw, dict) else None
            if pid is None:
                continue
            raw_statuses = (raw.get("_embedded") or {}).get("statuses") or []
            st_rows, st_skipped = _map_all(raw_statuses, lambda r: map_status(r, pid, now))
            self.report.skip(SOURCE, "statuses", st_skipped)
            statuses.extend(st_rows)
        upsert(self.session, AmoPipeline, rows)
        upsert(self.session, AmoStatus, statuses)
        self.session.commit()
        self.report.add("statuses", len(statuses))
        return len(rows), None

    def _sync_leads(self, since: datetime | None) -> tuple[int, datetime | None]:
        now, total, cursor, reasons = utcnow(), 0, None, 0
        for page in self.client.iter_leads(since):
            rows, skipped = _map_all(page, lambda r: map_lead(r, now))
            self.report.skip(SOURCE, "leads", skipped)
            loss_rows = [lr for raw in page if isinstance(raw, dict) for lr in loss_reasons_from_lead(raw, now)]
            total += upsert(self.session, AmoLead, rows)
            reasons += upsert(self.session, AmoLossReason, loss_rows)
            self.session.commit()
            for row in rows:
                cursor = st.max_dt(cursor, row["updated_at"])
        self.report.add("loss_reasons", reasons)
        return total, cursor

    def _sync_tasks(self, since: datetime | None) -> tuple[int, datetime | None]:
        now, total, cursor = utcnow(), 0, None
        for page in self.client.iter_tasks(since):
            rows, skipped = _map_all(page, lambda r: map_task(r, now))
            self.report.skip(SOURCE, "tasks", skipped)
            total += upsert(self.session, AmoTask, rows)
            self.session.commit()
            for row in rows:
                cursor = st.max_dt(cursor, row["updated_at"])
        return total, cursor

    def _sync_notes(self, since: datetime | None) -> tuple[int, datetime | None]:
        now, total, cursor = utcnow(), 0, None
        for page in self.client.iter_notes("leads", since):
            rows, skipped = _map_all(page, lambda r: map_note(r, "leads", now))
            self.report.skip(SOURCE, "notes", skipped)
            total += upsert(self.session, AmoNote, rows)
            self.session.commit()
            for row in rows:
                cursor = st.max_dt(cursor, row["updated_at"])
        return total, cursor
