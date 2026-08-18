# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A production FastAPI middleware that syncs data bidirectionally between **RentAsst** (Cloud Rental ERP, REST API) and **Tally Prime** (Windows accounting software, HTTP XML server on port 9000). It runs as a Windows service or standalone `.exe` and ships with a web dashboard.

Detailed design docs already exist at the repo root — read the relevant one before making non-trivial changes instead of re-deriving this from source:
- `ARCHITECTURE.md` — component/layer diagram, module responsibilities
- `SYNC_FLOW.md` — step-by-step forward/reverse sync pipeline
- `DATABASE.md` — full SQLite schema (8 tables) with column meanings
- `ERROR_HANDLING.md` — retry/backoff math, DLQ, `WAITING_FOR_DEPENDENCY`, crash recovery
- `RECONCILIATION.md` — reconciliation engine + field-ownership + conflict resolution policy
- `DEPLOYMENT.md` — NSSM service install, secrets, health probes, backup/restore
- `TROUBLESHOOTING.md` — common operational failures and fixes

## Commands

```powershell
# Activate venv (Windows)
.\venv\Scripts\Activate.ps1

# Run the dev server (auto-reload, port 8088 — NOT 8000 despite README)
python run.py

# Run the full test suite
python -m unittest discover -s tests

# Run a single test file
python -m unittest tests.test_idempotency

# Run a single test case / method
python -m unittest tests.test_idempotency.TestIdempotency.test_content_hash_dedup

# Build the standalone Windows executable (PyInstaller, uses RentalMiddleware.spec)
python build.py

# Run as a Windows service entrypoint (used by NSSM/pywin32)
python service.py
```

There is no separate lint/format command configured — none of `ruff`/`black`/`flake8` are declared in `requirements.txt`.

## Architecture

Request flow: `Dashboard UI (app/dashboard)` → `FastAPI routers (app/api/*)` → `Service layer (app/services/*)` → `Scheduler (app/scheduler/manager.py)` / `Queue worker (app/queue/worker.py)` → `Sync pipeline (app/sync/base.py)` → RentAsst REST client / Tally connectors → `SQLite state.db (app/mapping/store.py, app/queue/queue_store.py)`.

**Entity sync order is a hard dependency chain, enforced by `app/sync/dependencies.py`:**
`Customer → Asset Units (UOM) → Equipment → Rental Order → Invoice → Payment`. A child entity queued before its parent mapping exists transitions to `WAITING_FOR_DEPENDENCY` and retries after 60s rather than failing — never try to sync a child entity type standalone without confirming its parent mapping strategy.

**Idempotency is mapping+hash based, not just "check if it exists":** every synced record gets an `integration_key` (`company_id:entity_type:direction:source_id`) in the `mapping` table plus a SHA-256 `payload_hash`. A sync is skipped only if the hash is unchanged AND a live existence check against the target system (Tally) confirms the record is still there — if Tally lost the record (e.g. manual deletion), it resyncs even with a matching hash. When touching `app/sync/idempotency.py`, preserve this two-part check; don't optimize it down to a pure hash comparison.

**Tally is queried, never trusted on HTTP 200 alone.** `app/connectors/tally/parser.py` inspects `<CREATED>`/`<ALTERED>`/`<EXISTS>`/`<LINEERROR>` in the XML response body — an HTTP 200 with a `<LINEERROR>` is a failure. The `mapping` table is only updated after this parse confirms success. Any new Tally-writing code must go through this same parse-then-commit pattern, not treat the HTTP response as sufficient.

**Field ownership is directional and asymmetric**, enforced in `app/sync/ownership.py`: RentAsst owns customer identity fields (name, mobile, GSTIN) on the forward path; Tally owns accounting fields (balances, voucher numbers) on the reverse path. If both sides changed the same field since last sync, `app/sync/conflicts.py` logs to `sync_conflicts` (status `OPEN`) instead of picking a winner — resolution is a human action via `/api/conflicts/resolve`.

**Legacy top-level modules are dead code, not alternates to edit.** `app/mapping_store.py`, `app/rentasst_client.py`, `app/external_client.py`, `app/scheduler.py`, and `app/models.py` are old pre-refactor versions superseded by the package equivalents (`app/mapping/store.py`, `app/clients/rentasst_client.py`, `app/clients/external_client.py`, `app/scheduler/manager.py`, `app/models/domain.py`). `app/main.py` and the active `app/sync/*`/`app/api/*` code only import from the package versions. Verify which one a symbol comes from before editing — grep for the actual import, don't assume the flat file is current.

**Connector abstraction:** `app/connectors/factory.py` picks `TallyConnector` vs `RestConnector` based on `AppConfig.external_system_type`, so non-Tally targets are meant to be pluggable — but in practice Tally is the only exercised path (`app/connectors/tally/` has one module per concern: `client`, `company`, `ledger`, `unit`, `stock_item`, `sales_voucher`, `receipt_voucher`, `xml_builder`, `parser`).

**Config resolution order:** encrypted `.data/config.json.enc` (Fernet AES-256, `app/security/encryption.py`) → environment variable overrides (`RENTASST_API_KEY`, `RENTASST_URL`, `EXTERNAL_URL`) → `DiscoveryService` auto-detection of a local RentAsst install by scanning known install paths (`app/services/discovery_service.py`) for a `.env` file. All secrets are masked in logs/API responses by `app/security/masking.py` — never log raw config values when adding diagnostics.

**State lives entirely in one SQLite file**, `.data/state.db`: `mapping`, `sync_queue`, `sync_locks`, `dead_letters`, `sync_history`, `reconciliation_runs`, `reconciliation_discrepancies`, `sync_conflicts`. Locking is row-level via `sync_locks` (lease-based, expires after 300s) rather than relying on SQLite's own locking — `app/queue/lock_manager.py` is process- and thread-safe by design; don't add a second locking mechanism on top of it.

**Startup does real recovery work**, not just process init: `app/main.py`'s lifespan handler starts the queue worker, loads config, starts the polling scheduler, and fires an immediate sync — and `QueueWorker`/`QueueStore` recover any jobs stuck in `PROCESSING` (crash artifacts) back to `RETRYING`/`DLQ`. Keep new startup logic inside the `lifespan` context so shutdown (`scheduler.stop()`, `worker.stop()`) stays paired with it.

## Notes on doc drift

Treat the root `.md` docs as design intent, not verified-current facts — a couple of details have drifted from the code:
- README says port `8000`; `run.py`/`service.py` actually bind `8088`.
- README says "95 tests"; `tests/` currently has more (currently 112 passing via `python -m unittest discover -s tests`).

If you touch code these docs describe, update the doc in the same change rather than letting drift compound.
