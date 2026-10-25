# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CodeQL code scanning workflow and Dependabot configuration (pip, Docker, GitHub Actions).
- API reference and terminal screenshots in the README.
- Roadmap tracked as GitHub issues under the `v0.2.0` milestone.

### Changed

- CI workflow runs with a read-only `GITHUB_TOKEN` by default.
- The usage simulator's tenant catalogue uses fictional organization names.

## [0.1.0] - 2026-10-25

First public release.

### Added

- Batch ingest of usage samples with per-sample validation and dedupe by client-supplied sample id, within and across batches.
- Metric registry distinguishing gauges (level × hours) from deltas (totals split by time overlap).
- Rating engine that prorates samples into hourly or daily buckets and prices chronologically so free and graduated tiers apply across the period.
- Pricing plans with unit prices, free quantities, graduated tiers, glob fallbacks and regional overrides; plan assignment with effective dates.
- Invoices per tenant and period with discounts, credits (projected on drafts, consumed on finalize) and JSON/CSV export.
- Budget rules evaluated on ingest with one event per rule per period.
- Decimal money end to end (`NUMERIC` on PostgreSQL, canonical strings on SQLite).
- On-the-fly usage summary endpoint, optional API key, request ids, `/health`.
- `scripts/simulate_usage.py` driving realistic OpenStack-style usage through the API.
- Docker image, docker compose with PostgreSQL, CI on Python 3.10/3.11, Pages API reference, Codespaces and Render kits.

### Fixed

- `postgres://` and `postgresql://` database URLs are rewritten to the psycopg 3 driver.

[Unreleased]: https://github.com/AkibUllah/cloud-usage-metering/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AkibUllah/cloud-usage-metering/releases/tag/v0.1.0
