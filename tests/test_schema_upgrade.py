from __future__ import annotations

from decimal import Decimal

from sqlalchemy import inspect, text

from metering.models import Invoice, InvoiceLine, RatedCharge
from metering.schema_upgrade import ADDED_COLUMNS, upgrade_schema

from .conftest import OCT1, ingest, sample


def drop_new_columns(engine) -> None:
    """Put the tables back to how v0.1.0 created them."""
    with engine.begin() as conn:
        for added in ADDED_COLUMNS:
            conn.execute(text(f"ALTER TABLE {added.table} DROP COLUMN {added.column}"))


def columns(engine, table: str) -> set[str]:
    return {col["name"] for col in inspect(engine).get_columns(table)}


def test_fresh_database_needs_nothing(app):
    assert upgrade_schema(app.state.engine) == []


def test_upgrade_adds_columns_and_keeps_data(client, app, db, tenant):
    ingest(client, [sample("c", "compute.instance.m1.small", 1, OCT1, hours=2)])
    r = client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"})
    invoice_id = r.json()["id"]
    db.close()
    engine = app.state.engine
    drop_new_columns(engine)
    assert "overage_total" not in columns(engine, "invoices")

    applied = upgrade_schema(engine)

    assert applied == [f"{a.table}.{a.column}" for a in ADDED_COLUMNS]
    assert upgrade_schema(engine) == []  # idempotent
    invoice = db.get(Invoice, invoice_id)
    assert invoice.overage_total == invoice.subtotal == Decimal("5")
    assert invoice.committed_total == 0 and invoice.unused_commitment_total == 0
    assert [ln.line_type for ln in invoice.lines] == ["usage"]
    assert invoice.lines[0].commitment_id is None


def test_upgraded_tables_accept_new_rows(client, app, db, tenant):
    db.close()
    engine = app.state.engine
    drop_new_columns(engine)
    upgrade_schema(engine)

    ingest(client, [sample("c", "compute.instance.m1.small", 1, OCT1)])
    r = client.post("/v1/tenants/acme/invoices", params={"period": "2026-10"})
    assert r.status_code == 201, r.text
    assert db.query(RatedCharge).count() == 1
    assert db.query(InvoiceLine).one().line_type == "usage"
