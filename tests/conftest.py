from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from metering.config import Settings
from metering.main import create_app

ROOT = Path(__file__).resolve().parents[1]

OCT1 = datetime(2026, 10, 1, tzinfo=timezone.utc)
PLAN_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_settings(**overrides) -> Settings:
    base = dict(_env_file=None, database_url="sqlite://", log_level="WARNING", environment="test")
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(app):
    session = app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def simulator():
    """Import scripts/simulate_usage.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "simulate_usage", ROOT / "scripts" / "simulate_usage.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulate_usage"] = module
    spec.loader.exec_module(module)
    return module


def standard_plan() -> dict:
    return {
        "id": "standard-bdt",
        "name": "Standard (BDT)",
        "currency": "BDT",
        "prices": [
            {"metric": "compute.instance.m1.small", "unit_price": "2.50"},
            {"metric": "compute.instance.m1.medium", "unit_price": "5.00"},
            {"metric": "compute.instance.*", "unit_price": "15.00"},
            {"metric": "compute.instance.m1.small", "region": "ctg-1", "unit_price": "2.00"},
            {"metric": "storage.volume.gb", "unit_price": "0.0002"},
            {"metric": "network.floating_ip", "unit_price": "0.50", "free_quantity": "744"},
            {
                "metric": "network.bandwidth.egress.gb",
                "free_quantity": "100",
                "tiers": [
                    {"up_to": "1000", "unit_price": "8.00"},
                    {"up_to": None, "unit_price": "6.00"},
                ],
            },
        ],
    }


def sample(
    sid: str,
    metric: str,
    quantity,
    start: datetime,
    hours: float = 1,
    tenant_id: str = "acme",
    resource_id: str = "res-1",
    region: str | None = None,
) -> dict:
    from datetime import timedelta

    return {
        "id": sid,
        "tenant_id": tenant_id,
        "resource_id": resource_id,
        "metric": metric,
        "quantity": str(quantity),
        "region": region,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=hours)).isoformat(),
    }


@pytest.fixture
def tenant(client) -> dict:
    """Tenant 'acme' on the standard plan since January."""
    r = client.post("/v1/plans", json=standard_plan())
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/tenants", json={"id": "acme", "name": "Acme Cloud Ltd", "default_region": "dhaka-1"}
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/tenants/acme/plan",
        json={"plan_id": "standard-bdt", "effective_from": PLAN_START.isoformat()},
    )
    assert r.status_code == 201, r.text
    return {"id": "acme", "plan_id": "standard-bdt"}


def ingest(client, samples: list[dict]) -> dict:
    r = client.post("/v1/usage", json={"samples": samples})
    assert r.status_code == 202, r.text
    return r.json()
