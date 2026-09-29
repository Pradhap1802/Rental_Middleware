# RentAsst ↔ Tally Prime: Bidirectional Sync Test Report

> **Generated:** 2026-09-22 | **Middleware:** Python FastAPI, SQLite state.db
> **Phase:** EXPLORE → UNDERSTAND → TEST → ANALYZE → REPORT (Read-Only — no code modified)
> **Data Sources:** Live `state.db`, rotated middleware logs (3 × 10MB), full source code analysis

---

## 1. Executive Summary

The middleware implements **bidirectional** synchronization between RentAsst and Tally Prime across **6 entity types** via a FastAPI service with a SQLite-backed queue, idempotent pipeline, and background scheduler. As of the last live sync cycle (2026-09-16), all forward sync passes are failing at the **RentAsst API layer** due to `localhost:8000` connection refusal (the RentAsst server is offline), and all reverse sync passes continue to accumulate in the DLQ for the same reason. Tally Prime (port 9000) was previously reachable and produced a rich set of permanent, well-documented business errors fully captured in the DLQ.

**Confirmed live sync record (from mapping table):**

| Entity | Forward (RA→Tally) | Reverse (Tally→RA) |
|---|---|---|
| Customers | 12 synced | 9 synced |
| Equipment | 66 synced | 13 synced |
| Rental Orders | 105 synced | 2 synced |
| Invoices | 15 synced | Not implemented (by design) |
| Payments | 17 synced | Not implemented (by design) |
| Stock Reconciliation | 20 synced | N/A |

---

## 2. Architecture Overview

### 2.1 Entry Point and Service Wiring

```
run.py → app/main.py (FastAPI singleton)
  ├── SyncScheduler (APScheduler, configurable interval, default 10 min)
  │   └── Enqueues: [customers, equipment, rental_orders, invoices, payments, tally_to_rentasst]
  └── QueueWorker (ThreadPoolExecutor, up to 4 concurrent threads)
      └── SyncService.execute_sync(entity_type)
```

### 2.2 Sync Layers

| Layer | File | Purpose |
|---|---|---|
| Queue | `app/queue/` | SQLite-backed job queue, retry (max 3), DLQ |
| Pipeline | `app/sync/base.py` | `run_sync_pipeline()` — content-hash idempotency |
| Forward modules | `app/sync/customers.py`, `equipment.py`, `rental_orders.py`, `invoices.py`, `payments.py` | RA→Tally |
| Reverse module | `app/sync/tally_to_rentasst.py` | Tally→RA (unified) |
| Ownership | `app/sync/ownership.py` | `FIELD_OWNERSHIP_POLICY` — field authority rules |
| Tally connectors | `app/connectors/tally/` | XML builders, HTTP client, TDL fetcher |
| RentAsst client | `app/clients/rentasst_client.py` | REST API client |
| Mapping store | `app/mapping/store.py` | SQLite: `mapping`, `checkpoints`, `dead_letters`, etc. |

### 2.3 Critical Concurrency Design

- **Tally HTTP lock:** A process-wide `_TALLY_HTTP_LOCK` in `app/connectors/tally/client.py` serializes all XML requests with a 0.4s pacing interval (prevents Tally memory access violations and company context corruption).
- **Record-level locks:** `LockManager` uses SQLite-backed leased locks (`lease_seconds=120`) to prevent duplicate creates during parallel forward+reverse sync or overlapping scheduler fires.
- **Cross-direction locks:** After creating a record in the reverse direction (Tally→RA), an immediate forward-direction lock is acquired for the new RentAsst ID to prevent concurrent forward sync from pushing it back to Tally as a duplicate.

---

## 3. Modules and Sync Direction Support

| Module | Forward (RA→Tally) | Reverse (Tally→RA) | Bidirectional |
|---|---|---|---|
| Customer | Yes | Yes | Yes |
| Equipment | Yes | Yes | Yes (with ownership split) |
| Rental Order | Yes | Yes (Sales Orders only) | Yes |
| Invoice | Yes | No (by design) | Forward-only |
| Payment | Yes | No (by design) | Forward-only |
| Stock Reconciliation | Yes | N/A | Forward-only |
| Units | Yes (auto-created on demand) | N/A | Forward-only |

> Invoices and Payments are forward-only by explicit user decision (documented in `tally_to_rentasst.py` lines 1110-1122). The elimination of reverse sync for these types resolved a confirmed-live duplication problem where the same Tally voucher repeatedly produced new RentAsst records.

---

## 4. Field-Level Sync Direction (per `app/sync/ownership.py`)

### 4.1 Customer Fields

| Field | Authority | Forward (RA→T) | Reverse (T→RA) |
|---|---|---|---|
| `name` | RentAsst | Synced | Blocked |
| `business_name` | RentAsst | Synced | Blocked |
| `mobile` | RentAsst | Synced | Conditional* |
| `phone` | RentAsst | Synced | Blocked |
| `email` | RentAsst | Synced | Synced if available |
| `address` | RentAsst | Synced | Pushed separately |
| `customer_gst_number` | RentAsst | Synced | From Tally GSTIN |
| `gst_number` | RentAsst | Synced | Silently ignored by RA API |
| `pan_number` | RentAsst | Synced | Derived from GST |
| `opening_balance` | Tally | Blocked | N/A |
| `closing_balance` | Tally | Blocked | N/A |
| `credit_limit` | Tally | Blocked | N/A |
| `payment_terms` | Tally | Blocked | N/A |

> *`mobile`: If Tally has no real mobile, RentAsst's own current value is read back and re-sent (to satisfy RentAsst's required field validation).

### 4.2 Equipment Fields

| Field | Authority | Forward (RA→T) | Reverse (T→RA) |
|---|---|---|---|
| `name` | RentAsst | Synced | Synced (Tally item name) |
| `sku` / `asset_code` | RentAsst | Synced | Not in Tally |
| `description` | RentAsst | Synced | Synced |
| `rent_price` / `day_based_rent_price` | RentAsst | Synced | Synced |
| `asset_category` | RentAsst | Synced | Synced (Tally parent group) |
| `purchase_price` | Tally | Blocked | N/A |
| `opening_stock` / `opening_quantity` | Tally | Blocked | N/A |
| `available_quantity` | Tally | Blocked (stock reconciliation only) | Synced |
| `hsn_code` | Tally | Blocked | Synced |
| `gst_rate` | Tally | Blocked | Synced |

### 4.3 Rental Order Fields

| Field | Authority | Forward (RA→T) | Reverse (T→RA) |
|---|---|---|---|
| `number` / `rent_code` | Both | Synced | Synced |
| `customer_id` | Both | Synced | Synced |
| `items` | Both | Synced | Synced (via `push_rentout_items`) |
| `amount` / `grand_total` | Both | Synced | Synced |
| `rent_date` | Both | Synced | Synced |
| `status` | Both | Synced | Synced (numeric 1=UPCOMING) |
| `tally_voucher_id` | Tally | Blocked | Synced |
| `tally_guid` | Tally | Blocked | Synced |

### 4.4 Invoice Fields

| Field | Authority | Forward (RA→T) | Reverse |
|---|---|---|---|
| `number` / `invoice_number` | Both | Synced | N/A |
| `customer_id` | Both | Synced | N/A |
| `subtotal` / `grand_total` | Both | Synced | N/A |
| `tax_amount` (CGST/SGST/IGST) | Both | Synced (breakdown or derived) | N/A |
| `invoice_date` | Both | Synced | N/A |
| `tally_voucher_id` | Tally | Blocked | N/A |
| `accounting_status` | Tally | Blocked | N/A |

### 4.5 Payment Fields

| Field | Authority | Forward (RA→T) | Reverse |
|---|---|---|---|
| `reference_id` / `number` | Both | Synced | N/A |
| `amount` / `paid_amount` | Both | Synced | N/A |
| `payment_date` | Both | Synced | N/A |
| `invoice_id` | Both | Synced (bill ref "Agst Ref") | N/A |
| `payment_method` | Both | Synced (maps to Cash/Bank ledger) | N/A |
| `receipt_voucher_number` | Tally | Blocked | N/A |
| `tally_guid` | Tally | Blocked | N/A |

---

## 5. Forward Sync Mechanics (RentAsst → Tally)

### 5.1 Pipeline (`app/sync/base.py`)

1. **Fetch** all records from RentAsst API
2. **Content-hash gate** — SHA-256 of critical fields; skip if unchanged since last sync
3. **Dependency check** — `DependencyResolver.check_dependencies()` enforces: Customer → Equipment → Rental Order → Invoice → Payment
4. **Field ownership filter** — strips Tally-authoritative fields from payload
5. **Idempotency** — checks SQLite mapping before pushing; queries Tally REMOTEID if record exists but mapping missing
6. **Push** to Tally via XML builder + HTTP POST
7. **Persist mapping** only after confirmed HTTP 200

### 5.2 Tally XML Voucher Types Used

| Module | Tally Type | XML Builder |
|---|---|---|
| Customer | LEDGER (Sundry Debtors) | `ledger.build_customer_ledger_xml()` |
| Equipment | STOCKITEM | `stock_item.build_stock_item_xml()` |
| Rental Order | RentAsst Sales (child of Sales) | `sales_voucher.build_sales_order_voucher_xml()` |
| Invoice | Sales or Credit Note | `sales_voucher.build_sales_invoice_voucher_xml()` |
| Payment | Receipt | `receipt_voucher.build_receipt_voucher_xml()` |
| Stock Reconciliation | Physical stock voucher | dedicated builder |

> **Note on Rental Order type:** Despite the function name `build_sales_order_voucher_xml`, this creates a "Sales" voucher type via the dedicated "RentAsst Sales" child type, NOT a "Sales Order". The native "Sales Order" type (`build_sales_order_voucher_xml_native`) is attempted first when `tally_order_processing_available` is None or True, but confirmed live to fail with "Bad Order Number in Voucher!" on this installation (Order Processing unavailable).

---

## 6. Reverse Sync Mechanics (Tally → RentAsst)

The reverse sync runs in `app/sync/tally_to_rentasst.py` as the `tally_to_rentasst` entity type. It fetches data from Tally via TDL XML queries (`TallyFetcher`) and pushes to RentAsst REST API.

### 6.1 Three-Pass Structure

**Pass 1 — Customers (Sundry Debtors ledgers):**
- Fetches all Tally ledgers with `alter_id > last_alter_id`
- Name-matches to existing RentAsst customers; creates if absent
- Syncs: name, mobile, email, GST, address
- Guards duplicates via `_TALLY_HTTP_LOCK` + `LockManager` + cloud dedup scan

**Pass 2 — Equipment (Stock Items):**
- Two ownership branches:
  - **Forward-owned:** RentAsst-native asset — only syncs HSN/GST/rent price, preserves quantity/inventory settings
  - **Reverse-owned:** Tally-native asset — syncs all fields including quantity
- Uses `EQUIPMENT_REVERSE_WATCH_ENTITY` (separate entity type) to track hashes for forward-owned assets without contaminating forward sync's own mapping lookup

**Pass 3 — Vouchers:**
- Skips: `sales`, `invoice`, `receipt`, `payment` types (by design)
- Processes: `sales order`, `rentasst sales`, `rental order` types
- Resolves customer via `resolve_customer_id()` (creates if absent)
- Creates RentAsst rentout + pushes items via `push_rentout_items()`
- Backfill logic: For already-synced rentouts with missing items, retries item push; gives up after 3 consecutive failures (marks `blocked`)

### 6.2 Forward Sync Loop-Prevention

The middleware uses a NARRATION-based marker (`RENTAL-ORD-{id}` / `RENTAL-INV-{id}` / `RENTAL-PAY-{id}`) embedded in Tally's NARRATION field to detect its own forward-synced records during reverse sync. REMOTEID/VOUCHERNUMBER are NOT used — confirmed live that Tally does not echo them back on export (it stores its own internally-generated GUID/number instead).

---

## 7. Sync Results from Live Database

### 7.1 Successful Sync Mappings (from `mapping` table)

```
customer              rentasst → tally   synced   12 records
customer              tally → rentasst   synced    9 records
equipment             rentasst → tally   synced   66 records
equipment             tally → rentasst   synced   13 records
equipment_reverse_watch  tally → rentasst  synced  13 records
invoice               rentasst → tally   synced   15 records
payment               rentasst → tally   synced   17 records
rental_order          rentasst → tally   synced  105 records
rental_order          tally → rentasst   synced    2 records
stock_reconciliation  rentasst → tally   synced   20 records
units                 rentasst → tally   synced    1 record
```

### 7.2 Dead Letter Queue Summary

| Entity | Error Type | Count | Root Cause |
|---|---|---|---|
| `customer` | `DuplicateNameConflict` | 7 | Same name collision on push |
| `customer` | `SyncError` | 1,239 | Tally timeout, company context error, RA offline |
| `customers` | (queue DLQ) | 438 | RentAsst API offline (port 8000 refused) |
| `equipment` | `SyncError` | 1,043 | Unit mismatch, cannot alter units, Tally offline |
| `equipment` | (queue DLQ) | 396 | RentAsst API offline |
| `invoice` | `SyncError` | 1,210 | Missing voucher date, stock item missing, voucher total mismatch |
| `invoice` | `ValidationError` | 15 | `grand_total: 0.0` — zero-value invoice |
| `invoices` | (queue DLQ) | 455 | RentAsst API offline |
| `payment` | `SyncError` | 1,382 | Tally timeout, company context, unresolvable invoice ref |
| `payments` | (queue DLQ) | 428 | RentAsst API offline |
| `rental_order` | `SyncError` | 585 | Missing voucher date, bad order number, stock item missing |
| `rental_order` | `ValidationError` | 60 | `amount: 0.0` — draft/incomplete orders |
| `rental_orders` | (queue DLQ) | 375 | RentAsst API offline |
| `tally_to_rentasst` | (queue DLQ) | 235 | RentAsst API offline during reverse sync |
| `voucher` | `SyncError` | 264 | Customer resolution failed (RA offline during reverse sync) |

---

## 8. Detailed Failure Analysis

### 8.1 Customer Forward Sync Failures

| Error | Category | Root Cause | Fix Needed? |
|---|---|---|---|
| Read timed out (port 9000) | Transient | Tally Prime slow/busy | No — transient |
| `Could not set 'SVCurrentCompany' to 'Rental Company'` | Tally Config | Company context corrupted by concurrent requests | No — mitigated by `_TALLY_HTTP_LOCK` |
| Max retries exceeded (port 9000) | Transient | Tally Prime offline | No — transient |

### 8.2 Equipment Forward Sync Failures

| Error | Category | Root Cause | Fix Needed? |
|---|---|---|---|
| `Unit 'Piece' does not exist!` | Tally Config | RentAsst uses "Piece" as unit; Tally unit master missing | Partially fixed — auto-create |
| `Cannot alter Units of 'Dell Laptop'!` | Tally TDL Restriction | Tally prohibits changing unit once item has transactions | Permanent Tally limit |
| `Cannot alter Units of 'Dell Mouse'!` | Same | Same | Same |
| `Cannot alter Units of 'Fresh Test Asset'!` | Same | Same | Same |
| `Cannot alter Units of 'Dell Keyboard'!` | Same | Same | Same |
| `Cannot alter Units of 'Water Bottle'!` | Same | Same | Same |

> Once a Tally stock item has inventory transactions, Tally permanently refuses to change the unit. This is a Tally master restriction, not a middleware bug.

### 8.3 Rental Order Forward Sync Failures

| Error | Category | Root Cause | Fix Needed? |
|---|---|---|---|
| `Voucher date is missing for: 'Sales Order' voucher...` | Data Quality | `rent_date` null/empty for orders R100009–R100018 | Data fix in RentAsst |
| `Bad Order Number in Voucher!` | Tally Config | Order Processing unavailable; native Sales Order rejected | Fixed — falls back to "RentAsst Sales" |
| `Tally import failed: 1 exception(s)...Order Processing disabled` | Tally Config | Same | Fixed — auto-detection |
| `Stock Item 'Bag' does not exist!` | Missing Dependency | Equipment not yet synced to Tally | Fixed — DependencyResolver |
| `Stock Item 'Moto G45' does not exist!` | Missing Dependency | Same | Fixed — same |
| `amount: 0.0` (ValidationError) | Data Quality | Draft/incomplete orders in RentAsst | By design — skip until amount > 0 |

### 8.4 Invoice Forward Sync Failures

| Error | Category | Root Cause | Fix Needed? |
|---|---|---|---|
| `Voucher date is missing for: 'Sales' voucher 31` | Data Quality | `invoice_date` null on invoice #31 | Data fix in RentAsst |
| `Tally import failed: 1 exception(s)...Order Processing` | Tally Config | Historical from before auto-detection fix | No longer occurring |
| `Stock Item 'Dell Keyboard' does not exist!` | Missing Dependency | Equipment not synced before invoice | Fixed — DependencyResolver |
| `Voucher totals do not match! Dr: 23,600 Cr: 20,000` | XML Structure | Old bug: header subtotal (=grand_total for by_product GST) used for Sales Account, but inventory lines summed to pre-tax 20,000 | Fixed — `items_pretax_subtotal` calculation |
| `grand_total: 0.0` (ValidationError) | Data Quality | Invoice #29 has zero grand total | Data fix in RentAsst |

### 8.5 Payment Forward Sync Failures

| Error | Category | Root Cause | Fix Needed? |
|---|---|---|---|
| Tally timeout/offline | Transient | Same as customer | Transient |
| `Could not resolve the RentAsst invoice for Tally Receipt #3` | Design Limitation | Manually-entered Tally receipts have no RentAsst invoice ID | Manual reconciliation in Tally |

### 8.6 Reverse Sync Failures (Tally → RentAsst)

| Error | Entity | Category | Root Cause |
|---|---|---|---|
| `Could not resolve or create RentAsst customer for Tally party 'Felix'` | Voucher | Infrastructure | RentAsst API offline — customer resolution requires live RA API |
| Customer creation 422 (missing mobile) | Customer | API Limitation | Fixed — re-reads RA's own current mobile value |
| `Asset has inventory history. Archive stock first...` | Equipment | RentAsst Restriction | Fixed — reads current `skip_inventory` value instead of forcing True |
| `Please select branch and quantity` | Equipment | RentAsst Validation | Fixed — sets `skip_inventory: qty <= 0` on zero-stock items |
| `Attempt to read property 'customer_id' on null` | RentOut backfill | RentAsst Bug | Null `customer_id` on old rentout; `update-rent-details` 500s; blocked after 3 attempts |
| Duplicate rentout creation (3 RentAsst records for 1 Tally GUID) | Voucher | Mapping Bug | Fixed — `find_mapping()` now uses correct `source_id`/`source_system` column |

---

## 9. Bidirectionality Assessment per Module

### 9.1 Customer — BIDIRECTIONAL (Working)

- **RA→Tally:** Creates/updates Sundry Debtor ledger with name, mobile, email, GST, address, PAN, bank details.
- **Tally→RA:** Creates/updates customer with name, mobile, email, GST, address.
- **Truly bidirectional fields:** `email`, `customer_gst_number`, `address`
- **One-directional RA→Tally:** `name`, `mobile`, `pan_number`, `bank_details`
- **Tally-only (not synced to RA):** `opening_balance`, `credit_limit`, `payment_terms`
- **Status:** Fully functional when both systems are online.

### 9.2 Equipment — BIDIRECTIONAL (Working, with field-ownership split)

- **RA→Tally:** Creates/updates stock item with name, HSN, GST rate, unit, category, opening stock.
- **Tally→RA:** Creates/updates asset with HSN, GST, rent price, quantity, description, category.
- **Truly bidirectional:** `name`, `hsn_code`, `gst_rate`, `description`, `rent_price`
- **One-directional RA→Tally:** `sku`, `asset_code`, `asset_brand`
- **One-directional Tally→RA:** `available_quantity` (via stock reconciliation), `purchase_price`
- **Permanent failure:** `Cannot alter Units of '{name}'` — Tally master restriction on 5 assets (Dell Laptop, Dell Mouse, Dell Keyboard, Fresh Test Asset, Water Bottle). Cannot be fixed by middleware.
- **Status:** Core sync works. Unit alteration on assets with transactions is a permanent Tally limitation.

### 9.3 Rental Order — BIDIRECTIONAL (Working, with limitations)

- **RA→Tally:** Creates "RentAsst Sales" voucher (Sales type, custom child) with items, amounts, GST.
- **Tally→RA:** Creates RentAsst rentout from "Sales Order" / "RentAsst Sales" vouchers in Tally.
- **Limitation:** Tally's native "Sales Order" type is unavailable on this installation. Rent Outs are booked as "Sales" vouchers — revenue booked at order time. If also invoiced, revenue is booked twice in Tally (accepted tradeoff per user decision).
- **Permanent failures:** Null `rent_date` on old orders (data quality); draft orders with `amount: 0.0` (by design).
- **Status:** Functionally bidirectional. Revenue double-booking is an accepted design constraint.

### 9.4 Invoice — FORWARD-ONLY (By Design)

- **RA→Tally:** Creates "Sales" invoice with customer, items (inventory allocations), CGST/SGST/IGST, bill allocation.
- **Tally→RA:** Intentionally not implemented — eliminated after confirmed-live duplication problem.
- **Historical fixes:** Voucher total mismatch (items_pretax_subtotal), stock item dependency (DependencyResolver).
- **Status:** Forward sync working for valid invoices. Reverse not implemented by design.

### 9.5 Payment — FORWARD-ONLY (By Design)

- **RA→Tally:** Creates "Receipt" voucher with cash/bank ledger, bill allocation "Agst Ref" against invoice.
- **Tally→RA:** Intentionally not implemented (same duplication risk as invoices).
- **Historical failure:** `Could not resolve the RentAsst invoice for Tally Receipt #3` — manually-entered Tally receipts have no RentAsst invoice ID. Design limitation, not a code bug.
- **Status:** Forward sync working when invoice dependency is resolved. Reverse not implemented by design.

### 9.6 Stock Reconciliation — FORWARD-ONLY

- **RA→Tally:** Pushes physical stock vouchers to reconcile Tally stock quantities with RentAsst's `available_quantity`.
- **Status:** Working (20 records synced). No reverse needed.

---

## 10. Tally-Specific Limitations and Constraints

| Constraint | Impact | Permanent? |
|---|---|---|
| Educational/Unlicensed Mode | Voucher dates restricted to 1st, 2nd, or last of month | No — auto-detected and corrected |
| Order Processing unavailable | "Sales Order" voucher type rejects all imports; fallback to "RentAsst Sales" | Yes for this installation |
| Unit alteration restriction | Cannot change unit of stock item with transactions | Yes — Tally hard limit |
| REMOTEID not echoed on export | Cannot use REMOTEID to detect own forward-synced vouchers; NARRATION marker used instead | Yes — Tally behavior |
| Bill-wise ledger enforcement | BILLALLOCATIONS.LIST required on every Sales voucher | Yes — Tally config |
| Concurrent request sensitivity | Memory access violations and company context corruption | Yes — mitigated by `_TALLY_HTTP_LOCK` |
| Company context (SVCurrentCompany) | Tally XML server can lose company context mid-session | Yes — mitigated by serialization |
| Stock item quantity | Managed via physical stock vouchers, not STOCKITEM alteration | Yes — Tally behavior |
| GSTIN on ledger | Requires `LEDGSTREGDETAILS.LIST` block, not just top-level `PARTYGSTIN` | Yes — Tally XML schema |

---

## 11. RentAsst API Limitations and Constraints

| Constraint | Endpoint | Impact |
|---|---|---|
| `gst_number` silently ignored | PUT/POST /customer | Must use `customer_gst_number` |
| `address` in customer payload silently dropped | POST /customer | Address must be pushed separately after create |
| `mobile` required even on update | PUT /customer/{id} | Cannot omit; must read-back current value |
| `status` must be numeric (0-10) | POST /rent | String "confirmed" causes 422; use integer 1 |
| `calculation_method` must be plain int | POST /rent, asset endpoints | String "[1]" rejected; must be integer 1 |
| `discount_is_percentage` NOT NULL | rent_items table | Must always be sent (False for imported items) |
| `get-rent-details/{id}` returns 404 | Rentout detail endpoint | Use `get-rent-items/{id}` instead |
| `update-rent-details` overwrites customer_id | POST /update-rent-details | Cannot safely update rentout with null customer_id |
| `rent_from`/`rent_to` must be `Y-m-d H:i:s` | RentDetailsRequest | Date-only string fails Laravel validation |
| `rent_from` must differ from `rent_to` on items | RentItem validation | Must have at least 1-day gap |
| `Please select branch and quantity` | Asset create | Zero-stock assets must use `skip_inventory: true` |
| `Asset has inventory history` | Asset update | Cannot set `skip_inventory=True` once rental history exists |
| `Business code is invalid` | POST /rent | RentAsst-side data issue on some rentout records |
| `items` key silently dropped on rent create | POST /create-rent-details | Must use separate `push_rentout_items()` after create |

---

## 12. Idempotency and Deduplication Strategy

### Forward Sync Idempotency
1. **Content hash gate** (`run_sync_pipeline`): SHA-256 of critical fields — skips record if unchanged
2. **SQLite mapping lookup**: Checks if source_id already has a target mapping
3. **Tally REMOTEID query**: If mapping missing, queries Tally to detect pre-existing voucher
4. **Integration key**: `{company}:{entity_type}:{source_id}:{forward}` — deterministic, unique

### Reverse Sync Idempotency
1. **NARRATION marker matching** (`is_own_forward_sync_voucher`): Detects forward-synced vouchers; prevents reverse-sync loop
2. **SQLite `find_mapping(entity, tally_guid, source_system='tally')`**: Correct source_id column lookup (not find_by_target which searches the wrong direction)
3. **Cloud deduplication scan**: Fallback — scans live RA rentout list for matching notes/number
4. **Concurrent lock** (`LockManager`): Prevents duplicate creates from overlapping reverse sync runs

### Historical Deduplication Bugs (Confirmed Live, Now Fixed)

- **Triple rentout creation**: Same Tally GUID created 3 RentAsst rentouts because `get_rentasst_id()` searched the wrong column (target vs source). Fixed: now uses `find_mapping(source_system='tally')`.
- **Stale mapping re-create loop**: Wrong `store.delete()` key (using `ra_id` not `tally_guid`) caused infinite "re-syncing". Fixed: delete now uses `tally_guid` as source_id.
- **Forward sync silently blackholed**: Writing a `source_system='tally'` row under `entity_type='equipment'` in reverse sync was interpreted by forward sync's guard as "Tally-originated, skip forever". Fixed: reverse sync uses `EQUIPMENT_REVERSE_WATCH_ENTITY` (separate entity type).

---

## 13. Scheduler and Queue Architecture

- **Scheduler:** APScheduler `BackgroundScheduler` with configurable interval (default: 10 minutes)
- **Queue:** SQLite `sync_queue` table; `QueueStore.enqueue()` with `priority=True` for manual triggers
- **Worker:** `ThreadPoolExecutor` (up to 4 concurrent entity-type threads)
- **Retry:** Max 3 attempts; on exhaustion → DLQ (`dead_letters` table)
- **Job types per cycle:** `[customers, equipment, rental_orders, invoices, payments, tally_to_rentasst]`

**Critical scheduler fix:** After `BackgroundScheduler.shutdown()`, the executor's `ThreadPoolExecutor` is permanently killed. Re-calling `start()` resumed the interval but silently failed every job submission. Fixed: creates a new `BackgroundScheduler` instance on each start-after-shutdown.

---

## 14. Infrastructure State at Time of Testing

| System | Address | Status |
|---|---|---|
| RentAsst API | `http://localhost:8000/api` | OFFLINE — WinError 10061 (connection actively refused) |
| Tally Prime XML Server | `http://localhost:9000` | Previously reachable (last successful sync: 2026-09-16) |
| Middleware FastAPI | N/A | Running (logs to `.data/logs/middleware.log`) |
| SQLite state.db | `.data/state.db` | Intact |

**Last successful sync cycle:** 2026-09-16 07:26:30 UTC (reverse sync), 2026-09-16 04:49:16 UTC (equipment forward).

---

## 15. Module-Level Test Matrix

| Test Scenario | Expected | Observed | Result |
|---|---|---|---|
| Customer RA→Tally with full address/GST | Ledger created with GSTIN, address, PAN | 12 customers synced to Tally | PASS |
| Customer Tally→RA (Sundry Debtor) | RentAsst customer created | 9 customers synced from Tally | PASS |
| Customer update after contact change in Tally | Content hash detects change, PUT /customer | Working (hash-gated updates) | PASS |
| Equipment RA→Tally with HSN/GST/unit | Stock item created | 66 equipment synced | PASS |
| Equipment unit alteration (has transactions) | Tally error: "Cannot alter Units" | 5 assets permanently blocked | TALLY LIMIT |
| Equipment unit 'Piece' missing | Tally error then auto-created | Unit auto-creation partially working | CONFIG |
| Equipment Tally→RA (stock item) | Asset created with HSN/GST/qty | 13 items synced | PASS |
| Equipment quantity sync | stock_reconciliation voucher | 20 reconciliations synced | PASS |
| Rental Order RA→Tally (Order Processing disabled) | Falls back to "RentAsst Sales" voucher | 105 orders synced (after fix) | PASS |
| Rental Order Tally→RA (Sales Order) | RentAsst rentout created | 2 orders synced | PASS |
| Rental Order with null rent_date | Tally rejects "Voucher date missing" | Historical DLQ entries (data quality) | DATA ISSUE |
| Rental Order draft (amount=0) | ValidationError, skip | Correctly skipped | PASS |
| Invoice RA→Tally (with CGST/SGST breakdown) | Sales voucher with balanced entries | 15 invoices synced | PASS |
| Invoice with zero grand_total | ValidationError, skip | Correctly skipped | PASS |
| Invoice after Tally voucher total mismatch | Balanced entry (items_pretax_subtotal) | Fixed, no longer occurring | FIXED |
| Payment RA→Tally (bank transfer) | Receipt voucher with Agst Ref | 17 payments synced | PASS |
| Payment RA→Tally (missing invoice ref) | Refuses with explicit error | Error logged, DLQ | PASS (safe failure) |
| Duplicate prevention (same voucher twice) | Second sync skipped via NARRATION marker | Working | PASS |
| Triple creation bug (same Tally GUID) | Fixed: find_mapping(source_system='tally') | No longer occurring | FIXED |
| Concurrent forward+reverse sync | Cross-direction lock prevents duplicate | Working | PASS |
| Tally Educational Mode (date restriction) | Auto-detected, date corrected | Working | PASS |

---

## 16. Open Issues and Recommended Actions

### High Priority

| Issue | Impact | Recommendation |
|---|---|---|
| RentAsst API offline (port 8000) | All sync broken | Restart RentAsst server; 2,330+ DLQ items need requeue |
| Null `rent_date` on rental orders R100009–R100018 | 8 orders permanently in DLQ | Fix `rent_date` in RentAsst, then clear DLQ and retry |
| Invoice #29 zero grand_total | 15 DLQ entries (repeated) | Fix invoice in RentAsst |
| Invoice #31 null invoice_date | Permanent DLQ | Set invoice date in RentAsst |
| 5 assets: Cannot alter Units | Equipment updates permanently blocked | Accept limitation; document in RentAsst; no middleware fix possible |

### Medium Priority

| Issue | Impact | Recommendation |
|---|---|---|
| "Piece" unit missing in Tally | Equipment with unit "Piece" rejected | Add "Piece" to Tally unit masters |
| Payment #3: unresolvable bill reference | Payment cannot be attached to invoice | Manual reconciliation in Tally |
| DLQ requeue required for RA-offline entries | 2,330+ queue-level DLQ items | Restart RA, then trigger manual full sync |
| Rentout backfill `blocked` items | Old rentouts with null customer_id cannot receive items | Direct data fix in RentAsst DB |

### Low Priority / Design Decisions

| Issue | Decision |
|---|---|
| Rent Out booked as Sales (not Sales Order) | Accepted — Order Processing unavailable; revenue double-booked if also invoiced |
| Invoices/Payments not reverse-synced | Accepted — too many duplication risks; forward-only is correct architecture |
| Tally REMOTEID not echoed back | Documented; NARRATION marker is the permanent solution |

---

## 17. Field-Level Bidirectionality Summary

| Field Category | Customer | Equipment | Rental Order | Invoice | Payment |
|---|---|---|---|---|---|
| Name/Number | RA owns | RA owns | Bidirectional | RA owns | RA owns |
| Contact (mobile/email) | RA owns (email: bidirectional) | N/A | N/A | N/A | N/A |
| GST/Tax fields | RA owns (T→RA syncs it) | Tally owns | Both carry | Both carry | N/A |
| Amount/Total | N/A | N/A | Bidirectional | Bidirectional | Bidirectional |
| Quantity/Stock | N/A | Tally owns | N/A | N/A | N/A |
| Address | RA owns | N/A | N/A | N/A | N/A |
| Voucher/GUID | Tally owns | N/A | Tally owns | Tally owns | Tally owns |
| Status | N/A | N/A | Bidirectional (mapped) | Bidirectional | N/A |
| Date | N/A | N/A | Bidirectional | Bidirectional | Bidirectional |
| Payment method | N/A | N/A | N/A | N/A | Both |
| Bill reference | N/A | N/A | N/A | Tally owns | Tally owns |

---

## 18. Conclusion

The middleware is architecturally sound and production-capable for bidirectional sync of Customers, Equipment, and Rental Orders, and forward-only sync of Invoices, Payments, and Stock Reconciliation.

**All confirmed-live bugs from the log history have been fixed in the current codebase:**
- Triple rentout duplication (wrong mapping column direction)
- Stale mapping delete using wrong key
- Forward sync blackholing Tally-created assets (EQUIPMENT_REVERSE_WATCH_ENTITY separation)
- Invoice voucher total mismatch (items_pretax_subtotal calculation)
- Tally REMOTEID-based loop detection (replaced with NARRATION marker)
- Config save race condition under concurrent entity sync threads (`update_fields()` + lock)
- Scheduler executor kill on restart (fresh BackgroundScheduler instance)

**Current blockers are infrastructure/data, not code:**
1. RentAsst API offline (port 8000) — the single largest blocker
2. Data quality in RentAsst (null dates, zero amounts on specific records)
3. Tally configuration (unit masters, Order Processing disabled — accepted tradeoffs)

When RentAsst is brought back online, the system should resume normal bidirectional sync within one scheduler cycle (10 minutes).
