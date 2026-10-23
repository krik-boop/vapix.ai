from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.types import JsonType


class AmoUser(Base):
    # Email is intentionally not stored.
    __tablename__ = "amo_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(255))
    lang: Mapped[str | None] = mapped_column(String(8))
    is_admin: Mapped[bool | None]
    is_active: Mapped[bool | None]
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoPipeline(Base):
    __tablename__ = "amo_pipelines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(255))
    sort: Mapped[int | None] = mapped_column(Integer)
    is_main: Mapped[bool | None]
    is_unsorted_on: Mapped[bool | None]
    is_archive: Mapped[bool | None]
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoStatus(Base):
    # System statuses (142 "won", 143 "lost") share the same id in every pipeline,
    # so the key is (pipeline_id, id).
    __tablename__ = "amo_statuses"

    pipeline_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(255))
    sort: Mapped[int | None] = mapped_column(Integer)
    is_editable: Mapped[bool | None]
    color: Mapped[str | None] = mapped_column(String(16))
    type: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoLossReason(Base):
    __tablename__ = "amo_loss_reasons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoLead(Base):
    __tablename__ = "amo_leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None] = mapped_column(BigInteger)
    pipeline_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    responsible_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    loss_reason_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closest_task_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool | None]
    tags: Mapped[list[str] | None] = mapped_column(JsonType)
    custom_fields_values: Mapped[Any | None] = mapped_column(JsonType)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoTask(Base):
    __tablename__ = "amo_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    responsible_user_id: Mapped[int | None] = mapped_column(BigInteger)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    task_type_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str | None] = mapped_column(Text)
    is_completed: Mapped[bool | None]
    complete_till: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration: Mapped[int | None] = mapped_column(Integer)
    result_text: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AmoNote(Base):
    __tablename__ = "amo_notes"

    entity_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    note_type: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[Any | None] = mapped_column(JsonType)
    responsible_user_id: Mapped[int | None] = mapped_column(BigInteger)
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
