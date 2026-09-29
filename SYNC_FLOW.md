# Forward & Reverse Synchronization Pipelines

This document details the step-by-step forward sync execution hierarchy, dependency resolution, pre-flight math validation, idempotency checks, and reverse sync pipeline.

---

## Forward Sync Execution Hierarchy

The middleware enforces a strict dependency chain for forward synchronization (RentAsst $\to$ Tally Prime):

$$\text{Customer} \longrightarrow \text{Equipment} \longrightarrow \text{Rental Order} \longrightarrow \text{Invoice} \longrightarrow \text{Payment}$$

### Step-by-Step Forward Sync Workflow

1. **Job Enqueue & Batch Chunking**:
   - Entities fetched from RentAsst REST API are chunked into batch sizes (default 100).
   - In-memory TTL cache prefetches existing parent dependency mappings (`prefetch_mappings`).

2. **Record-Level Concurrency Lock**:
   - Acquires distributed lock for `lock_key = company_id:entity_type:forward:record_id`.
   - If locked by another worker, job skips concurrent execution cleanly.

3. **Pre-Flight Data & Math Validation (Task 10)**:
   - Evaluates required fields, date formats, and numeric types.
   - For Invoices, evaluates mathematical integrity:
     $$\text{subtotal} + \text{tax\_amount} + \text{additional\_charges} - \text{discount\_amount} = \text{grand\_total}$$
   - Invalid math or schemas immediately route job to Dead-Letter Queue (DLQ) without calling Tally XML.

4. **Dependency Resolution (Task 11)**:
   - For `Invoice` sync, verifies `Customer` mapping exists in `mapping` table.
   - For `Payment` sync, verifies `Invoice` mapping exists in `mapping` table.
   - If parent mapping is missing, job state transitions to `WAITING_FOR_DEPENDENCY` and schedules delayed retry (60s).

5. **Deterministic Idempotency Check**:
   - Generates integration key: `company_id:entity_type:forward:source_id`.
   - Computes SHA-256 content hash of payload (`compute_payload_hash`).
   - If mapping exists and `last_synced_hash == payload_hash`:
     - Calls `check_exists_in_tally(entity_type, identifier)`.
     - If record exists in Tally, sync is skipped cleanly.
     - If record was deleted in Tally, middleware automatically resynchronizes.

6. **Target System Timeout Recovery**:
   - If local mapping is missing (e.g. following database restore or network timeout on prior attempt), queries Tally Prime XML server (`check_target_system_record_exists`).
   - If record exists in Tally, adopts existing Tally ID without creating a duplicate voucher.

7. **Tally XML Generation & Transmission**:
   - Builds structured Tally XML request envelope (`connectors/tally/xml_builder.py`).
   - Sends HTTP POST request to Tally Prime XML Server (`http://localhost:9000`).

8. **Tally Response Validation & Error Extraction (Task 9)**:
   - Parses response XML using `connectors/tally/parser.py`.
   - Checks `<CREATED>`, `<ALTERED>`, `<EXISTS>`, `<ERRORS>`, and `<LINEERROR>`.
   - Never treats HTTP 200 as automatic accounting success.
   - Updates `mapping` table ONLY AFTER confirmed success response.

---

## Reverse Sync Pipeline (Tally $\to$ RentAsst)

Reverse sync mirrors customers, equipment, and rental orders (Sales Order vouchers) from
Tally into RentAsst. Invoices and payments are RentAsst-native and are never
reverse-synced — they're created later, only via forward sync, referencing the rental
order's Tally identity established here.

1. **Voucher/Ledger Ingestion**:
   - Fetches vouchers and ledgers from Tally Prime using `ALTERID` checkpointing (`TallyFetcher.fetch_vouchers(last_alter_id)`).
   - Sales Order vouchers become RentAsst rentouts; LEDGER masters become RentAsst customers/equipment.

2. **Reverse Field Ownership Filtering**:
   - Filters the rental_order payload according to field ownership policy (`app/sync/ownership.py`)
     before pushing — e.g. `tally_voucher_id`/`tally_master_id`/`tally_guid` stay Tally-authoritative.

3. **RentAsst REST API Push**:
   - Sends the transformed payload to RentAsst REST API (`RentAsstClient.push_rentout()` /
     `push_customer()` / equipment equivalents).
   - Persists the reverse mapping (`company_id:entity_type:reverse:tally_guid`) ONLY AFTER a confirmed HTTP response.
