from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.types import JsonType


class HhVacancy(Base):
    __tablename__ = "hh_vacancies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    area_id: Mapped[str | None] = mapped_column(String(32))
    area_name: Mapped[str | None] = mapped_column(String(255))
    archived: Mapped[bool | None]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    salary_from: Mapped[int | None] = mapped_column(BigInteger)
    salary_to: Mapped[int | None] = mapped_column(BigInteger)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    alternate_url: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HhNegotiation(Base):
    """A response (отклик) / invitation to a vacancy."""

    __tablename__ = "hh_negotiations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    vacancy_id: Mapped[str] = mapped_column(String(32), index=True)
    collection: Mapped[str | None] = mapped_column(String(64))
    state_id: Mapped[str | None] = mapped_column(String(64))
    employer_state_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(64))
    resume_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HhCandidate(Base):
    """Minimal candidate profile.

    By design there are NO columns for name, photo, birth date/age, gender, phone or email.
    """

    __tablename__ = "hh_candidates"

    resume_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    city: Mapped[str | None] = mapped_column(String(255))
    desired_salary_amount: Mapped[int | None] = mapped_column(BigInteger)
    desired_salary_currency: Mapped[str | None] = mapped_column(String(8))
    total_experience_months: Mapped[int | None] = mapped_column(Integer)
    # [{"position", "company", "industry", "start", "end"}]
    experience: Mapped[list[dict[str, Any]] | None] = mapped_column(JsonType)
    skills: Mapped[list[str] | None] = mapped_column(JsonType)
    resume_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HhOAuthToken(Base):
    """Latest hh.ru token pair, only written when HH_TOKEN_AUTO_REFRESH=true.

    hh.ru refresh tokens are single-use, so a refreshed pair must be persisted somewhere;
    after the first refresh these values take precedence over HH_ACCESS_TOKEN/HH_REFRESH_TOKEN.
    """

    __tablename__ = "hh_oauth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
