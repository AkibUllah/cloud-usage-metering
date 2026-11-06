"""In-place schema upgrades for databases created by an earlier release.

Tables come from ``create_all``, which adds missing tables but never touches existing ones. Columns
added to an existing table are listed here and applied at startup, so an upgraded deployment keeps
its data. Each entry is idempotent, and the column definition is read from the ORM model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .database import Base

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AddedColumn:
    table: str
    column: str
    backfill: str | None = None  # SQL to run once, right after the column is added


ADDED_COLUMNS: tuple[AddedColumn, ...] = (
    AddedColumn("rated_charges", "commitment_id"),
    AddedColumn("invoice_lines", "line_type"),
    AddedColumn("invoice_lines", "commitment_id"),
    AddedColumn("invoices", "committed_total"),
    AddedColumn("invoices", "unused_commitment_total"),
    # Every invoice issued before commitments existed is pay-as-you-go in full.
    AddedColumn("invoices", "overage_total", "UPDATE invoices SET overage_total = subtotal"),
)


def _add_column(conn: Connection, added: AddedColumn) -> None:
    column = Base.metadata.tables[added.table].c[added.column]
    definition = column.type.compile(dialect=conn.dialect)
    if column.server_default is not None:
        definition += f" DEFAULT '{column.server_default.arg}'"
    if not column.nullable:
        definition += " NOT NULL"
    # Two workers can start at once; on PostgreSQL the second one then skips the column.
    guard = "IF NOT EXISTS " if conn.dialect.name == "postgresql" else ""
    conn.execute(text(f"ALTER TABLE {added.table} ADD COLUMN {guard}{added.column} {definition}"))
    if added.backfill:
        conn.execute(text(added.backfill))
    log.info("schema upgrade: added %s.%s", added.table, added.column)


def upgrade_schema(engine: Engine) -> list[str]:
    """Add the columns this version needs; returns the ones it added."""
    applied: list[str] = []
    with engine.begin() as conn:
        inspector = inspect(conn)
        existing = {
            table: {col["name"] for col in inspector.get_columns(table)}
            for table in {added.table for added in ADDED_COLUMNS}
        }
        for added in ADDED_COLUMNS:
            if added.column not in existing[added.table]:
                _add_column(conn, added)
                applied.append(f"{added.table}.{added.column}")
    return applied
