# RentAsst ↔ Tally Prime Production Integration Middleware

Enterprise-grade, resilient, multi-company middleware connecting **RentAsst Equipment Rental Cloud ERP** and **Tally Prime Accounting Software**.

---

## Key Features

- **Isolated Modular Tally Connectors**: Dedicated modular architecture for Tally XML integration (`connectors/tally/client.py`, `company.py`, `ledger.py`, `stock_item.py`, `sales_voucher.py`, `receipt_voucher.py`, `xml_builder.py`, `parser.py`).
- **Resilient SQLite Queue Engine**: Asynchronous queue management with explicit states (`PENDING`, `PROCESSING`, `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`, `RETRYING`, `WAITING_FOR_DEPENDENCY`, `DLQ`, `CANCELLED`).
- **Multi-Company Isolation**: Complete tenant data isolation using scoped integration keys (`company_id:entity_type:direction:source_id`) and multi-tenant SQLite database constraints.
- **Deterministic Idempotency**: Pre-flight target existence checks and content hash deduplication preventing duplicate vouchers in Tally Prime.
- **Dependency-Aware Hierarchy**: Execution order enforcement: `Customer` $\to$ `Equipment` $\to$ `Rental Order` $\to$ `Invoice` $\to$ `Payment`.
- **Pre-Flight Data & Math Validation**: Pre-flight payload schema and math validation (`subtotal + tax + charges - discount = grand_total`) stopping invalid data before XML transmission.
- **Bidirectional Sync & Conflict Detection**: Explicit field ownership rules (`RentAsst` authoritative for customer info; `Tally` authoritative for balances) with automatic conflict logging.
- **Reverse Sync**: Tally $\to$ RentAsst reverse voucher ingestion for Cloud ERP reconciliation.
- **Read-Only Reconciliation Engine**: Macro-level audit engine identifying missing records, ID mismatches, date discrepancies, and tax variances without altering accounting data.
- **Startup Crash Recovery**: Auto-recovery of stale `PROCESSING` jobs stranded by process crashes or Windows reboots.
- **Production Security & Credential Redaction**: Fernet AES-256 secret encryption at rest and automatic credential masking across all structured logs and API responses.
- **Enterprise Health Monitoring**: Specialized Kubernetes-compatible liveness/readiness probes (`/health`, `/health/live`, `/health/ready`, `/health/rentasst`, `/health/tally`).
- **Production Dashboard UI**: Interactive web interface displaying live system connections, entity sync counts, queue job state breakdowns, reconciliation audit figures, and performance KPIs.
- **State Database Backup & Disaster Recovery**: Verified SQLite online backups (`PRAGMA quick_check;`), pre-restore safety snapshots, and 30-day retention purging.

---

## System Requirements

- **OS**: Windows 10/11 or Windows Server 2016/2019/2022
- **Python**: 3.10+
- **Tally Prime**: Release 1.1+ with HTTP XML Server enabled (Default Port: 9000)
- **RentAsst API**: REST API v1 Access Token

---

## Quick Start

### 1. Installation
```powershell
git clone https://github.com/RentAsst/Rental_Middleware.git
cd Rental_Middleware
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Run the Gateway
```powershell
python run.py
```
The FastAPI middleware server will start at `http://localhost:8000`.

### 3. Open Production Dashboard
Navigate to `http://localhost:8000` in your web browser.

---

## API Endpoints Overview

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Comprehensive system health overview |
| `GET` | `/health/live` | Liveness probe (HTTP 200) |
| `GET` | `/health/ready` | Readiness probe (HTTP 200 / 503) |
| `GET` | `/api/status` | Production Dashboard metrics |
| `GET` | `/api/config` | Read masked system configuration |
| `POST` | `/api/config` | Update encrypted configuration |
| `POST` | `/api/sync/{entity}` | Trigger sync for `customers`, `equipment`, `invoices`, `payments`, `tally_to_rentasst` |
| `GET` | `/api/conflicts` | List open bidirectional conflicts |
| `POST` | `/api/conflicts/resolve` | Resolve conflict with `RENTASST` or `TALLY` authority |
| `POST` | `/api/reconciliation/run` | Execute read-only reconciliation audit |
| `GET` | `/api/deadletter` | List dead-letter queue (DLQ) records |
| `POST` | `/api/deadletter/requeue/{id}` | Requeue DLQ item for retry |
| `POST` | `/api/backups` | Trigger verified SQLite state backup |
| `POST` | `/api/backups/restore/{filename}` | Restore database from verified backup |

---

## Running Automated Tests

Run the complete test suite (95 tests):
```powershell
python -m unittest discover -s tests
```
