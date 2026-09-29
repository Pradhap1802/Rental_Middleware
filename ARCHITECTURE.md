# Component & Layer Architecture

This document describes the production component architecture, layer separation, modular Tally integration, queue processing lifecycle, and threading model.

---

## Architectural Diagram

```mermaid
graph TD
    UI["Web Dashboard UI (app/ui/index.html)"] --> API["FastAPI REST Layer (app/api/)"]
    API --> SVC["Service Layer (app/services/)"]
    SVC --> SCHED["Sync Scheduler (app/scheduler/manager.py)"]
    SVC --> WORKER["Queue Worker (app/queue/worker.py)"]
    
    WORKER --> LOCK["Lock Manager (app/queue/lock_manager.py)"]
    WORKER --> PIPELINE["Sync Pipeline Engine (app/sync/base.py)"]
    
    PIPELINE --> VAL["Validator Layer (app/validation/validator.py)"]
    PIPELINE --> DEP["Dependency Resolver (app/sync/dependencies.py)"]
    PIPELINE --> IDEM["Idempotency Engine (app/sync/idempotency.py)"]
    PIPELINE --> CONF["Conflict Detector (app/sync/conflicts.py)"]
    
    PIPELINE --> STORE["SQLite MappingStore (app/mapping/store.py)"]
    PIPELINE --> CACHE["TTLCache (app/utils/cache.py)"]
    
    PIPELINE --> RA["RentAsst REST Client (app/clients/rentasst_client.py)"]
    PIPELINE --> TALLY["Isolated Tally Connectors (app/connectors/tally/)"]
    
    TALLY --> T_CLIENT["client.py"]
    TALLY --> T_COMPANY["company.py"]
    TALLY --> T_LEDGER["ledger.py"]
    TALLY --> T_STOCK["stock_item.py"]
    TALLY --> T_SALES["sales_voucher.py"]
    TALLY --> T_RECEIPT["receipt_voucher.py"]
    TALLY --> T_XML["xml_builder.py"]
    TALLY --> T_PARSER["parser.py"]
    
    STORE [(SQLite Database: state.db)]
```

---

## Modular Component Structure

### 1. Isolated Tally Connector Architecture (`app/connectors/tally/`)
- `client.py`: High-level Tally XML HTTP client managing connection pings and request dispatches.
- `company.py`: Fetches active company names from Tally Prime XML server.
- `ledger.py`: Generates and parses Sundry Debtors and Accounting Ledger creation requests.
- `stock_item.py`: Generates and parses StockItem/Equipment creation requests.
- `sales_voucher.py`: Builds Sales Voucher XML payloads for Rental Orders and Invoices.
- `receipt_voucher.py`: Builds Receipt Voucher XML payloads for Customer Payments.
- `xml_builder.py`: Low-level XML envelope generator wrapping `<ENVELOPE>`, `<HEADER>`, and `<BODY>`.
- `parser.py`: Robust response parser extracting `<CREATED>`, `<ALTERED>`, `<EXISTS>`, and `<LINEERROR>` tags without assuming HTTP 200 is an accounting success.

### 2. SQLite Repository & State Engine (`app/mapping/store.py` & `app/queue/`)
- `store.py`: `MappingStore` managing persistent entity mappings, checkpoints, history, and dead letters.
- `queue_store.py`: `QueueStore` managing `sync_queue` lifecycle states and deduplication.
- `lock_manager.py`: Process-safe and thread-safe record-level SQLite lock manager with lease expiration.
- `worker.py`: Asynchronous background worker thread pool executing enqueued jobs.

### 3. Sync Pipelines & Core Domain Engines (`app/sync/` & `app/reconciliation/`)
- `base.py`: Resilient generic `run_sync_pipeline()` managing chunked batch execution, locks, validation, and idempotency.
- `dependencies.py`: Enforces execution hierarchy: `Customer` $\to$ `Equipment` $\to$ `Order` $\to$ `Invoice` $\to$ `Payment`.
- `idempotency.py`: Generates multi-company scoped integration keys and conducts target system pre-checks.
- `ownership.py`: Implements field ownership policy filtering for forward and reverse directions.
- `conflicts.py`: `ConflictDetector` logging bidirectional modification conflicts for human resolution.
- `tally_to_rentasst.py`: Production reverse sync runner transferring Tally vouchers to RentAsst Cloud API.
- `engine.py`: `ReconciliationEngine` performing read-only macro audits across datasets.

### 4. Security & Configuration (`app/security/` & `app/configuration/`)
- `encryption.py`: Fernet AES-256 encryption engine securing configuration secrets at rest.
- `masking.py`: Redacts passwords, API keys, and bearer tokens across logs and responses.
- `store.py`: `ConfigStore` loading configuration from environment variables or encrypted disk storage.
