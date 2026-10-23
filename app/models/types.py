from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# JSONB on PostgreSQL, plain JSON elsewhere (SQLite in tests).
JsonType = JSON().with_variant(JSONB(), "postgresql")
