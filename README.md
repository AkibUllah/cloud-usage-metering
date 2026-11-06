<!-- topics: fastapi,python,cloud,metering,billing,openstack,usage-based-pricing,sqlalchemy,saas -->
# Cloud Usage Metering

Usage metering, rating and invoicing for cloud resources: compute hours, GB-hours of storage, floating IPs and bandwidth, priced with tiered and regional plans and turned into invoices and budget alerts.

[![CI](https://github.com/AkibUllah/cloud-usage-metering/actions/workflows/ci.yml/badge.svg)](https://github.com/AkibUllah/cloud-usage-metering/actions/workflows/ci.yml)
[![CodeQL](https://github.com/AkibUllah/cloud-usage-metering/actions/workflows/codeql.yml/badge.svg)](https://github.com/AkibUllah/cloud-usage-metering/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/AkibUllah/cloud-usage-metering)](https://github.com/AkibUllah/cloud-usage-metering/releases)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)
![License](https://img.shields.io/badge/license-MIT-green)

## Try it live

| | |
|---|---|
| API reference | https://akibullah.github.io/cloud-usage-metering/ — Swagger UI built from the exported OpenAPI document (static; no backend behind it) |
| Run in your browser | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/AkibUllah/cloud-usage-metering?quickstart=1) — starts API + PostgreSQL with `docker compose` and runs the simulator (a BDT plan, three tenants, three days of hourly usage, invoices); open forwarded port 8000 at `/docs`. |
| One-click deploy | [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/AkibUllah/cloud-usage-metering) — `render.yaml` provisions the web service and a free PostgreSQL; `API_KEY` is generated, so send it as `X-API-Key` (or run the simulator with `--api-key`). |
| Local | `docker compose up` (see Quick Start) |

## Screenshots

![Swagger UI API reference](docs/images/api-reference.png)

Live API reference on GitHub Pages: plans, tenants, usage ingest, invoices and budgets.

![simulator run against a local API](docs/images/terminal-demo.png)

A real local run: the simulator ingests three days of hourly usage for three tenants and generates invoices, a replay is fully deduplicated with identical totals, and one tenant's rated month-to-date usage.

## Why this exists

Running an OpenStack-based cloud for customers means turning Ceilometer-style samples into bills that survive scrutiny: a server that ran 37 minutes must be charged 37 minutes, the first floating IP must stay free, egress must be priced in graduated tiers, and replaying a batch of samples must never double-bill anyone. This service is that pipeline, written to be exact (Decimal end to end, no floats), idempotent (sample ids are the dedupe key, invoices regenerate in place) and testable without infrastructure.

## Features

- Batch ingest of usage samples `(tenant, resource_id, metric, quantity, start, end)` with per-sample validation and dedupe by client-supplied sample id, within a batch and across batches
- Metric registry distinguishing **gauges** (instance count, volume GB, floating IPs: level x hours) from **deltas** (egress GB, requests: totals split by time overlap)
- Rating engine that prorates samples into hourly or daily buckets, aggregates per metric and region, and prices chronologically so free tiers and graduated tiers apply across the whole period
- Pricing plans with per-metric unit prices, free quantities, graduated tiers, glob fallbacks (`compute.instance.*`) and regional overrides; plan assignment per tenant with effective dates (mid-period plan changes are priced bucket by bucket)
- Commitments (reserved pricing): prepaid quantity per billing period at a commitment price, drawn down before pay-as-you-go tiers, prorated to the microsecond when they start or end mid-period, with committed, overage and unused amounts shown separately on invoices
- Invoice generation per tenant and billing period: line items, percentage discounts (global or per metric), credits (projected on drafts, consumed on finalize), JSON and CSV export
- Budget rules per tenant (total or per metric) evaluated on every ingest; one stored and logged event per rule per period
- Exact money handling: Decimal everywhere, `NUMERIC` on Postgres and canonical strings on SQLite, so tests run on SQLite with identical results
- Usage summary endpoint that rates a period on the fly without persisting
- Optional shared API key, request ids and timing logs, `/health`
- `scripts/simulate_usage.py` generates realistic multi-tenant OpenStack-style usage (flavors, volumes, floating IPs, office-hours servers) and drives the whole flow through the API

## Architecture

```mermaid
flowchart LR
    Agents[Collectors / Ceilometer-style agents] -->|POST /v1/usage batch| Ingest[Ingest\nvalidate · dedupe by sample id]
    Ingest --> Samples[(usage_samples)]
    Ingest --> Budgets[Budget evaluation\nmonth-to-date spend]
    Budgets --> Events[(budget_events)]
    Samples --> Rating[Rating engine\nprorate · aggregate · price]
    Plans[(pricing_plans\nplan_assignments)] --> Rating
    Commitments[(commitments)] --> Rating
    Rating -->|settlement per commitment| Invoicing
    Rating --> Charges[(rated_charges)]
    Charges --> Invoicing[Invoicing\nlines · discounts · credits]
    Invoicing --> Invoices[(invoices · lines)]
    Invoices -->|JSON / CSV| Billing[Billing / finance]
```

Key decisions:

- **Proration by overlap.** Each sample is clipped to the billing window and split across buckets by the seconds it overlaps. Gauges become `quantity x hours`, deltas are divided proportionally. A 50 GB volume alive for 30 minutes is 25 GB-hours, no special cases.
- **Incremental pricing with a running total.** Buckets are priced in order with a per-(metric, region) consumed counter, so a free tier of 100 GB and graduated tiers over the month come out exactly equal to pricing the monthly total, while still leaving per-hour charges for drill-down.
- **Commitments rate first, and on their own counter.** A bucket is drawn down from active commitments and only the remainder reaches the plan, so overage starts at the first free unit and the first tier. See [Pricing](#pricing).
- **Idempotency at two levels.** Samples dedupe on their id, so collectors can retry freely. Re-rating a window deletes and rewrites its charges; regenerating a draft invoice updates it in place; finalized invoices are immutable.
- **Credits are consumed only on finalize.** Drafts show the projected credit so finance can review, but nothing is deducted until the invoice is locked, which makes regeneration safe.
- **Decimal, always.** Quantities, prices and amounts are `Decimal`; per-bucket charges keep six decimals and invoice lines are rounded once at the end (half-up to 2 places).

## Tech Stack

| Layer | Choice |
| --- | --- |
| Web framework | FastAPI, Pydantic v2, pydantic-settings |
| Persistence | SQLAlchemy 2.0 ORM; SQLite (dev/tests), PostgreSQL via psycopg 3 (compose) |
| Money / time | `decimal.Decimal`, `Money` and `UTCDateTime` type decorators |
| Tooling | pytest, httpx TestClient, ruff, GitHub Actions |
| Runtime | Docker (python:3.11-slim, non-root), docker compose with Postgres 16 |

## Quick Start

### Docker compose (Postgres)

```bash
git clone git@github.com:AkibUllah/cloud-usage-metering.git
cd cloud-usage-metering
docker compose up --build -d
# API: http://localhost:8000  ·  OpenAPI docs: http://localhost:8000/docs

# Seed a BDT plan, three tenants, 3 days of hourly usage, then generate invoices
docker compose exec api python scripts/simulate_usage.py --base-url http://localhost:8000 --days 3 --invoice
```

### Local (SQLite)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn metering.asgi:app --reload

# in another shell
python scripts/simulate_usage.py --days 2 --invoice
```

Sample simulator output (`--days 3 --tenants 3 --seed 42`, October 2026):

```
samples=1368 accepted=1368 duplicates=0 rejected=0
INV-202610-00001  Swift Courier                BDT       475.89  (4 lines)
INV-202610-00002  Demo Grocer Infrastructure   BDT      1066.98  (4 lines)
INV-202610-00003  Riverline Tickets            BDT       386.77  (5 lines)
```

Run it again and every sample is reported as a duplicate while the invoices are regenerated with the same totals. Add `--commitments` to sell each tenant a reserved block of instance-hours and print the committed, unused and overage split per invoice.

### Manual walk-through

```bash
# 1. a plan: flat compute prices, GB-hour storage, one free floating IP, tiered egress with 100 GB free
curl -s -X POST localhost:8000/v1/plans -H 'content-type: application/json' -d '{
  "id": "standard-bdt", "name": "Standard (BDT)", "currency": "BDT",
  "prices": [
    {"metric": "compute.instance.m1.small", "unit_price": "2.50"},
    {"metric": "compute.instance.m1.small", "region": "ctg-1", "unit_price": "2.00"},
    {"metric": "storage.volume.gb", "unit_price": "0.0002"},
    {"metric": "network.floating_ip", "unit_price": "0.50", "free_quantity": "744"},
    {"metric": "network.bandwidth.egress.gb", "free_quantity": "100",
     "tiers": [{"up_to": "1000", "unit_price": "8.00"}, {"up_to": null, "unit_price": "6.00"}]}
  ]}'

# 2. a tenant on that plan
curl -s -X POST localhost:8000/v1/tenants -H 'content-type: application/json' \
  -d '{"id": "acme", "name": "Acme Cloud Ltd", "default_region": "dhaka-1"}'
curl -s -X POST localhost:8000/v1/tenants/acme/plan -H 'content-type: application/json' \
  -d '{"plan_id": "standard-bdt", "effective_from": "2026-01-01T00:00:00Z"}'

# 3. usage: one small instance for 90 minutes
curl -s -X POST localhost:8000/v1/usage -H 'content-type: application/json' -d '{"samples": [
  {"id": "srv-1:compute:2026-10-01T10", "tenant_id": "acme", "resource_id": "srv-1",
   "metric": "compute.instance.m1.small", "quantity": "1",
   "start": "2026-10-01T10:00:00Z", "end": "2026-10-01T11:30:00Z"}]}'

# 4. what does October look like so far?  -> 1.5 instance-hours, 3.75 BDT
curl -s 'localhost:8000/v1/tenants/acme/usage/summary?period=2026-10'

# 5. invoice it
curl -s -X POST 'localhost:8000/v1/tenants/acme/invoices?period=2026-10'
```

## Pricing

Pay-as-you-go comes from the tenant's plan: unit prices, free quantities, graduated tiers, regional overrides. A plan is resolved per bucket, so a mid-period plan change takes effect at its exact hour.

### Commitments

A commitment is a prepaid block of one metric: `quantity` units per billing period at `unit_price`, running over `[starts_at, ends_at)`. Leave `ends_at` out for no fixed end, and `region` out to cover usage in every region from one pool.

```bash
# 310 instance-hours a month at 1.50 instead of 2.50, from 22 October
curl -s -X POST localhost:8000/v1/tenants/acme/commitments -H 'content-type: application/json' -d '{
  "id": "acme-reserved-small", "metric": "compute.instance.m1.small",
  "quantity": "310", "unit_price": "1.50", "starts_at": "2026-10-22T00:00:00Z"}'
```

How it rates:

- **Committed first.** While a commitment is active, usage draws its pool down at the commitment price. Overage falls through to the plan, where free quantity and graduated tiers start from zero: with 100 GB free and a 150 GB commitment, 300 GB of egress is 150 committed, 100 free and 50 in the first tier.
- **Proration by time.** The pool is `quantity x active time / period length`, computed as an exact fraction and rounded half-up once to six decimals. A full month gets the whole quantity whatever its length. Boundaries split rating buckets, so usage at 10:20 is not covered by a commitment starting at 10:30, in hourly or daily rating.
- **Pools do not roll over.** Each billing period gets a fresh pool, and the fee stays due whether or not it is used.
- **Several commitments stack.** They are drawn region-specific first, then soonest to end, then cheapest, so nothing expires unused while later usage overflows.

Worked example for the commitment above, with 10 hours of usage on 1 October and 80 hours from 22 October. It is active for 10 of the 31 days, so its pool is 100 hours:

| Line | `line_type` | Quantity | Unit price | Amount |
| --- | --- | --- | --- | --- |
| Usage inside the commitment | `committed` | 80 | 1.50 | 120.00 |
| Usage before it started | `usage` | 10 | 2.50 | 25.00 |
| Rest of the pool | `unused_commitment` | 20 | 1.50 | 30.00 |

Invoices carry `committed_total`, `unused_commitment_total` and `overage_total`, which add up to `subtotal`, and the CSV export has the same split. The committed and unused amounts always add up to the rounded fee for the pool, so usage can never move what a commitment costs by a cent. `/usage/summary` reports the same totals plus the drawdown of each commitment.

Rules worth knowing:

- Percentage discounts apply to `usage` lines only, because a commitment price is already the negotiated one. Credits apply to the whole invoice.
- Committed usage counts towards budget rules; the unused part does not, since it is only known when the period closes.
- `metric` must be an exact registered name, not a glob: instance flavors differ in value, so a block of one is not a block of another.
- A commitment bills in one currency, by default that of the plan in force at `starts_at`. If the tenant later moves to a plan in another currency, invoicing returns 409 rather than bill the wrong value.
- A finalized invoice locks what it used. A window it covers cannot gain a commitment, its quantity, price and start cannot change and it cannot be deleted; set `ends_at` to a date after the last finalized period instead. Regenerate any draft after changing a commitment.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness + database check |
| GET | `/v1/metrics` | Metric registry (gauge/delta, units) |
| POST / GET | `/v1/plans` | Create plan with prices, list plans |
| GET | `/v1/plans/{plan_id}` | Plan detail |
| POST / GET | `/v1/tenants` | Create tenant, list tenants |
| GET | `/v1/tenants/{id}` | Tenant detail |
| POST | `/v1/tenants/{id}/plan` | Assign plan from `effective_from` (closes the previous assignment) |
| GET | `/v1/tenants/{id}/plan` · `/plan/history` | Current plan, assignment history |
| POST | `/v1/usage` | Ingest a batch of samples -> `accepted`, `duplicates`, `rejected[]`, `budget_events[]` |
| GET | `/v1/usage` | Query samples by tenant, metric, resource, time window |
| GET | `/v1/tenants/{id}/usage/summary?period=YYYY-MM` | Rate a period on the fly; per-metric quantity, free and committed quantity, cost, and each commitment's drawdown |
| POST / GET | `/v1/tenants/{id}/commitments` | Create / list commitments (optional client-chosen `id`, so a retried create is a 409, not a second block) |
| GET / PATCH / DELETE | `/v1/tenants/{id}/commitments/{commitment_id}` | Read, update or delete a commitment, within the limits of finalized invoices |
| POST | `/v1/tenants/{id}/invoices?period=YYYY-MM` | Generate or refresh the draft invoice |
| GET | `/v1/tenants/{id}/invoices` | Invoices of a tenant |
| GET | `/v1/invoices/{id}` · `/csv` | Invoice JSON, CSV export |
| POST | `/v1/invoices/{id}/finalize` | Lock invoice and consume credits |
| POST / GET | `/v1/tenants/{id}/credits` | Add / list credits |
| POST / GET | `/v1/tenants/{id}/discounts` | Add / list percentage discounts (optional metric glob and validity window) |
| POST / GET | `/v1/tenants/{id}/budgets` | Create / list budget rules |
| DELETE | `/v1/tenants/{id}/budgets/{rule_id}` | Delete rule |
| GET | `/v1/tenants/{id}/budget-events` | Alerts emitted for the tenant |

When `API_KEY` is set, all `/v1/*` routes require `X-API-Key`. `/health` is always open.

### Metrics

| Metric | Kind | Sample quantity | Rated unit |
| --- | --- | --- | --- |
| `compute.instance.<flavor>` | gauge | instances | instance-hour |
| `compute.vcpu`, `compute.ram.gb` | gauge | vCPUs / GB | vcpu-hour / GB-hour |
| `storage.volume.gb`, `storage.snapshot.gb`, `storage.object.gb` | gauge | GB | GB-hour |
| `network.floating_ip`, `network.loadbalancer` | gauge | count | ip-hour / lb-hour |
| `network.bandwidth.egress.gb`, `network.bandwidth.ingress.gb` | delta | GB | GB |
| `storage.object.requests` | delta | requests | request |

## Project Structure

```
src/metering/
├── asgi.py          # uvicorn entrypoint
├── main.py          # create_app() factory
├── config.py        # Settings
├── database.py      # engine/session, Money + UTCDateTime type decorators
├── models.py        # Tenant, UsageSample, PricingPlan/PlanPrice, PlanAssignment, Commitment, RatedCharge, Invoice, Credit, Discount, BudgetRule/Event
├── metrics.py       # metric registry (gauge vs delta)
├── periods.py       # YYYY-MM periods, hourly/daily buckets, overlap maths
├── pricing.py       # PriceSpec, Tier, graduated/free-tier rating, PriceBook resolution
├── plans.py         # plan in force at a time, assignment with effective dates
├── commitments.py   # entitlement proration, drawdown ledger, settlement per period
├── rating.py        # prorate -> aggregate -> draw down -> price -> RatedCharge
├── ingest.py        # batch validation + dedupe
├── invoices.py      # lines, discounts, credits, finalize, CSV
├── budgets.py       # threshold rules, one event per rule per period
├── schema_upgrade.py  # adds new columns to existing databases at startup
├── schemas.py       # Pydantic models
├── deps.py          # API key + tenant lookup dependencies
└── routers/         # health, plans, tenants, commitments, usage, invoices, budgets
scripts/simulate_usage.py   # realistic usage generator / end-to-end driver
tests/                      # 156 tests, SQLite in-memory by default
```

## Testing

```bash
make test        # or: pytest
make lint

# the same suite on PostgreSQL, against a throwaway database
TEST_DATABASE_URL=postgresql+psycopg://metering:metering@localhost:5432/metering make test-postgres
```

CI runs both.

Coverage highlights:

- pricing: flat, free tier, graduated tiers, free tier + tiers, incremental == total, tier validation, regional/glob resolution order
- rating: partial-hour proration, multi-bucket gauges, proportional deltas, window clipping, daily buckets, aggregation, free tier across buckets, regional override, mid-period plan change, unpriced metrics, re-rating idempotency
- commitments: proration by time and to the instant, drawdown order, stacking, regional pools, fresh pools per period, overage through free and graduated tiers, boundaries inside a bucket, fee independent of usage, invoice lines and totals, CRUD validation, finalized-invoice locks, an API flow from commitment to invoice
- schema upgrade: columns added in place, data kept, idempotent, backfill of earlier invoices
- ingest: batch accept, duplicates within and across batches, replay does not double-bill, rejection reasons, validation, batch size limit, API key enforcement
- invoices: totals equal line sums (hand-computed), global and per-metric discounts, expired discounts, credits projected vs consumed, oversize credit remainder, expired credits, finalize lock, CSV export
- budgets: fires once per period, metric-scoped rules, disabled rules, separate periods, no-plan tenants
- simulator: deterministic generation, end-to-end run and idempotent re-run through the API, JSON output mode

## Roadmap

- [ ] Alembic migrations (tables are created with `create_all` at startup) (#1)
- [ ] Async rating worker so budget evaluation and invoice generation leave the request path (#2)
- [ ] Invoice PDF rendering and a webhook sink for budget events (#4)
- [ ] Ingest adapter for Ceilometer / Gnocchi measures (#5)

## License

MIT © 2026 Akib Jafor
