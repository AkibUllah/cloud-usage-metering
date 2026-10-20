from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from metering.budgets import evaluate_budgets

from .conftest import OCT1, ingest, sample

D = Decimal


def test_rule_fires_once_when_month_to_date_spend_crosses_threshold(client, tenant):
    r = client.post(
        "/v1/tenants/acme/budgets", json={"name": "Dev budget", "threshold_amount": "100"}
    )
    assert r.status_code == 201

    body = ingest(client, [sample("a", "compute.instance.m1.small", 1, OCT1, hours=10)])  # 25.00
    assert body["budget_events"] == []

    body = ingest(
        client, [sample("b", "compute.instance.m1.small", 1, OCT1 + timedelta(hours=10), hours=40)]
    )  # +100 = 125
    assert len(body["budget_events"]) == 1
    event = body["budget_events"][0]
    assert D(event["spend"]) == D("125.00") and D(event["threshold_amount"]) == 100
    assert event["period"] == "2026-10" and "Dev budget" in event["message"]

    body = ingest(
        client, [sample("c", "compute.instance.m1.small", 1, OCT1 + timedelta(hours=50), hours=10)]
    )
    assert body["budget_events"] == []  # already alerted this period
    events = client.get("/v1/tenants/acme/budget-events").json()
    assert len(events) == 1


def test_metric_specific_rule_ignores_other_metrics(client, tenant):
    client.post(
        "/v1/tenants/acme/budgets",
        json={"name": "Egress cap", "threshold_amount": "50", "metric": "network.bandwidth.*"},
    )
    body = ingest(
        client, [sample("c", "compute.instance.m1.small", 1, OCT1, hours=100)]
    )  # 250 BDT compute
    assert body["budget_events"] == []
    body = ingest(
        client, [sample("e", "network.bandwidth.egress.gb", 110, OCT1, resource_id="r")]
    )  # 10 billable GB = 80
    assert len(body["budget_events"]) == 1
    assert D(body["budget_events"][0]["spend"]) == D("80.00")


def test_disabled_rule_never_fires(client, tenant):
    client.post(
        "/v1/tenants/acme/budgets", json={"name": "Off", "threshold_amount": "1", "enabled": False}
    )
    body = ingest(client, [sample("c", "compute.instance.m1.small", 1, OCT1, hours=10)])
    assert body["budget_events"] == []
    rules = client.get("/v1/tenants/acme/budgets").json()
    assert rules[0]["enabled"] is False


def test_separate_periods_alert_separately(client, tenant):
    client.post("/v1/tenants/acme/budgets", json={"name": "Monthly", "threshold_amount": "20"})
    oct_body = ingest(client, [sample("o", "compute.instance.m1.small", 1, OCT1, hours=10)])
    nov_body = ingest(
        client, [sample("n", "compute.instance.m1.small", 1, OCT1 + timedelta(days=31), hours=10)]
    )
    assert [e["period"] for e in oct_body["budget_events"]] == ["2026-10"]
    assert [e["period"] for e in nov_body["budget_events"]] == ["2026-11"]


def test_delete_rule_and_evaluate_without_plan(client, db):
    client.post("/v1/tenants", json={"id": "acme", "name": "Acme"})
    rule = client.post(
        "/v1/tenants/acme/budgets", json={"name": "x", "threshold_amount": "1"}
    ).json()
    assert evaluate_budgets(db, "acme", OCT1) == []  # no plan -> spend 0 -> nothing fires
    assert client.delete(f"/v1/tenants/acme/budgets/{rule['id']}").status_code == 200
    assert client.delete(f"/v1/tenants/acme/budgets/{rule['id']}").status_code == 404
