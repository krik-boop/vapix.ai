"""hh.ru -> PostgreSQL sync: employer vacancies, negotiations (responses) and minimal candidates."""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import upsert
from app.integrations import sync_state as st
from app.integrations.hh.client import HhClient
from app.integrations.utils import as_aware, from_iso, to_int, utcnow
from app.models import HhCandidate, HhNegotiation, HhVacancy

logger = logging.getLogger(__name__)

SOURCE = "hh"

# Whitelist of experience fields we keep. Description, company URL, employer logos etc. are dropped.
EXPERIENCE_FIELDS = ("position", "company", "start", "end")


def _name(obj: Any) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def map_vacancy(raw: dict, now: datetime, archived: bool | None = None) -> dict | None:
    if not raw.get("id"):
        return None
    salary = raw.get("salary") or {}
    area = raw.get("area") or {}
    return {
        "id": str(raw["id"]),
        "name": raw.get("name"),
        "area_id": str(area["id"]) if area.get("id") else None,
        "area_name": area.get("name"),
        "archived": raw.get("archived") if raw.get("archived") is not None else archived,
        "published_at": from_iso(raw.get("published_at")),
        "salary_from": to_int(salary.get("from")),
        "salary_to": to_int(salary.get("to")),
        "salary_currency": salary.get("currency"),
        "alternate_url": raw.get("alternate_url"),
        "synced_at": now,
    }


def map_negotiation(raw: dict, vacancy_id: str, collection: str | None, now: datetime) -> dict | None:
    if not raw.get("id"):
        return None
    resume = raw.get("resume") or {}
    return {
        "id": str(raw["id"]),
        "vacancy_id": vacancy_id,
        "collection": collection,
        "state_id": (raw.get("state") or {}).get("id"),
        "employer_state_id": (raw.get("employer_state") or {}).get("id"),
        "source": raw.get("source"),
        "resume_id": str(resume["id"]) if resume.get("id") else None,
        "created_at": from_iso(raw.get("created_at")),
        "updated_at": from_iso(raw.get("updated_at")),
        "synced_at": now,
    }


def map_candidate(resume: dict, now: datetime) -> dict | None:
    """Keep only experience, skills, city and desired salary.

    Explicitly NOT stored: first/last/middle name, photo, age/birth date, gender,
    phone, email or any other contact data.
    """
    if not isinstance(resume, dict) or not resume.get("id"):
        return None
    salary = resume.get("salary") or {}
    experience = []
    for item in resume.get("experience") or []:
        if not isinstance(item, dict):
            continue
        exp = {k: item.get(k) for k in EXPERIENCE_FIELDS}
        exp["industry"] = _name(item.get("industry"))
        experience.append(exp)
    skills = [s for s in resume.get("skill_set") or [] if isinstance(s, str)]
    return {
        "resume_id": str(resume["id"]),
        "city": _name(resume.get("area")),
        "desired_salary_amount": to_int(salary.get("amount")),
        "desired_salary_currency": salary.get("currency"),
        "total_experience_months": to_int((resume.get("total_experience") or {}).get("months")),
        "experience": experience or None,
        "skills": skills or None,
        "resume_updated_at": from_iso(resume.get("updated_at")),
        "synced_at": now,
    }


class HhSync:
    def __init__(self, session: Session, client: HhClient, settings: Settings) -> None:
        self.session = session
        self.client = client
        self.settings = settings
        self.report = st.SyncReport()

    def _run(self, entity: str, mode: str, fn: st.EntityFn) -> None:
        st.run_entity(self.session, self.report, SOURCE, entity, mode, self.settings.sync_overlap_seconds, fn)

    def run(self, mode: str) -> st.SyncReport:
        if mode not in st.MODES:
            raise ValueError(f"Unknown mode {mode!r}")
        vacancy_ids: list[str] = []

        def vacancies(_since: datetime | None) -> tuple[int, datetime | None]:
            employer_id = self.client.get_employer_id()
            count, ids = self._sync_vacancies(employer_id, include_archived=(mode == st.FULL))
            vacancy_ids.extend(ids)
            return count, None

        # TODO(hh-docs): vacancy lists have no documented "updated since" filter or updated_at
        # field, so the vacancy list is always re-read in full (cheap: up to 50/1000 per page).
        self._run("vacancies", st.FULL, vacancies)
        if "vacancies" in self.report.errors:
            return self.report
        self._run("negotiations", mode, lambda since: self._sync_negotiations(vacancy_ids, since))
        return self.report

    def _sync_vacancies(self, employer_id: str, include_archived: bool) -> tuple[int, list[str]]:
        now, total, ids = utcnow(), 0, []
        sources = [(self.client.iter_active_vacancies, False)]
        if include_archived:
            sources.append((self.client.iter_archived_vacancies, True))
        for iterator, archived in sources:
            for page in iterator(employer_id):
                rows = [r for r in (map_vacancy(v, now, archived) for v in page if isinstance(v, dict)) if r]
                self.report.skip(SOURCE, "vacancies", len(page) - len(rows))
                total += upsert(self.session, HhVacancy, rows)
                self.session.commit()
                ids.extend(r["id"] for r in rows)
        return total, ids

    def _sync_negotiations(self, vacancy_ids: list[str], since: datetime | None) -> tuple[int, datetime | None]:
        # TODO(hh-docs): collection lists accept `order_by` with values from collections[].order_types,
        # which are account-specific and not enumerated in the spec. Without a guaranteed
        # "updated_at desc" order we cannot stop paging early, so incremental mode reads all pages
        # and only writes negotiations (and candidates) with updated_at >= since.
        now, total, cursor = utcnow(), 0, None
        for vacancy_id in dict.fromkeys(vacancy_ids):
            for collection in self.client.get_negotiation_collections(vacancy_id):
                coll_id = collection.get("id")
                for page in self.client.iter_negotiations(collection, vacancy_id):
                    rows, candidates = [], []
                    for raw in page:
                        row = map_negotiation(raw, vacancy_id, coll_id, now) if isinstance(raw, dict) else None
                        if row is None:
                            self.report.skip(SOURCE, "negotiations", 1)
                            continue
                        cursor = st.max_dt(cursor, row["updated_at"])
                        updated = as_aware(row["updated_at"])
                        if since is not None and updated is not None and updated < since:
                            continue
                        rows.append(row)
                        candidate = map_candidate(raw.get("resume"), now)
                        if candidate:
                            candidates.append(candidate)
                    total += upsert(self.session, HhNegotiation, rows)
                    upsert(self.session, HhCandidate, candidates)
                    self.session.commit()
                    self.report.add("candidates", len(candidates))
        return total, cursor
