"""Database engine, session factory and helpers."""

from collections.abc import Iterable
from functools import lru_cache

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    # hide_parameters keeps bound values (possibly tokens) out of SQLAlchemy errors.
    return create_engine(get_settings().get_database_url(), pool_pre_ping=True, hide_parameters=True)


def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def upsert(session: Session, model: type[Base], rows: Iterable[dict]) -> int:
    """INSERT ... ON CONFLICT (pk) DO UPDATE for PostgreSQL and SQLite."""
    rows = list(rows)
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise NotImplementedError(f"upsert is not implemented for {dialect}")

    pk_cols = [c.name for c in inspect(model).primary_key]
    # Postgres rejects a statement that touches the same row twice: keep the last version.
    rows = list({tuple(r[c] for c in pk_cols): r for r in rows}.values())
    # Group rows by key set so each statement has a consistent column list.
    by_keys: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        by_keys.setdefault(tuple(sorted(row)), []).append(row)
    for keys, group in by_keys.items():
        stmt = insert(model).values(group)
        update_cols = {k: stmt.excluded[k] for k in keys if k not in pk_cols}
        if update_cols:
            stmt = stmt.on_conflict_do_update(index_elements=pk_cols, set_=update_cols)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=pk_cols)
        session.execute(stmt)
    return len(rows)
