# SQLite Database Schema Specification

The middleware state database is stored locally at `.data/state.db`. It consists of 8 tables managing entity mapping persistence, queue lifecycle states, distributed locks, dead letters, audit history, reconciliation reports, and bidirectional conflicts.

---

## 1. Table `mapping`
Stores persistent cross-system identifier mappings between RentAsst and Tally Prime.

```sql
CREATE TABLE IF NOT EXISTS mapping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    source_system TEXT DEFAULT 'rentasst',
    source_company_id TEXT DEFAULT 'default',
    source_id TEXT NOT NULL,
    target_system TEXT DEFAULT 'tally',
    target_company_id TEXT DEFAULT 'default',
    target_id TEXT NOT NULL,
    integration_key TEXT UNIQUE,
    last_synced_hash TEXT,
    last_source_modified_at TEXT,
    last_target_modified_at TEXT,
    last_synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sync_version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'synced',
    rentasst_id TEXT,
    external_id TEXT,
    tally_guid TEXT,
    last_hash TEXT,
    last_sync TEXT,
    last_attempt TEXT,
    UNIQUE(entity_type, rentasst_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_integration_key ON mapping (integration_key) WHERE integration_key IS NOT NULL AND integration_key != '';
CREATE INDEX IF NOT EXISTS idx_mapping_source_lookup ON mapping (source_system, source_company_id, entity_type, source_id);
CREATE INDEX IF NOT EXISTS idx_mapping_target_lookup ON mapping (target_system, target_company_id, entity_type, target_id);
```

---

## 2. Table `sync_queue`
Manages asynchronous job queue states and retry schedules.

```sql
CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT DEFAULT '',
    company_id TEXT DEFAULT 'default',
    direction TEXT DEFAULT 'forward',
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING', -- PENDING, PROCESSING, SUCCESS, PARTIAL_SUCCESS, FAILED, RETRYING, WAITING_FOR_DEPENDENCY, DLQ, CANCELLED
    attempt_count INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    started_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    error_message TEXT,
    scheduled_at TEXT,
    next_retry_at TEXT,
    next_attempt_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sync_queue_lookup ON sync_queue (company_id, entity_type, status);
CREATE INDEX IF NOT EXISTS idx_sync_queue_schedule ON sync_queue (status, scheduled_at, next_retry_at);
```

---

## 3. Table `sync_locks`
Thread-safe and process-safe distributed record locks.

```sql
CREATE TABLE IF NOT EXISTS sync_locks (
    lock_key TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_locks_expires ON sync_locks (expires_at);
```

---

## 4. Table `dead_letters`
Permanent failure queue (DLQ) storing un-syncable records for human inspection and requeueing.

```sql
CREATE TABLE IF NOT EXISTS dead_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    source_id TEXT,
    rentasst_id TEXT,
    company_id TEXT DEFAULT 'default',
    source_system TEXT DEFAULT 'rentasst',
    target_system TEXT DEFAULT 'tally',
    payload TEXT,
    error_type TEXT,
    error_message TEXT,
    error TEXT,
    stack_trace TEXT,
    attempt_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'OPEN', -- OPEN, RESOLVED, IGNORED
    first_failed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_failed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 5. Table `sync_history`
Complete audit trail log of all synchronization attempts.

```sql
CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    rentasst_id TEXT NOT NULL,
    external_id TEXT,
    tally_guid TEXT,
    status TEXT NOT NULL,
    details TEXT,
    attempt_time TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 6. Table `reconciliation_runs`
Header record for read-only reconciliation audit runs.

```sql
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL,
    total_discrepancies INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

---

## 7. Table `reconciliation_discrepancies`
Itemized discrepancy log from reconciliation runs.

```sql
CREATE TABLE IF NOT EXISTS reconciliation_discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,
    rentasst_id TEXT,
    tally_id TEXT,
    mismatch_type TEXT NOT NULL, -- MISSING_IN_RENTASST, MISSING_IN_TALLY, ID_MISMATCH, AMOUNT_MISMATCH, DATE_MISMATCH, TAX_MISMATCH
    rentasst_value TEXT,
    tally_value TEXT,
    details TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES reconciliation_runs(id)
);
```

---

## 8. Table `sync_conflicts`
Bidirectional modification conflicts detected during sync.

```sql
CREATE TABLE IF NOT EXISTS sync_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    rentasst_value TEXT,
    tally_value TEXT,
    rentasst_modified_at TEXT,
    tally_modified_at TEXT,
    status TEXT DEFAULT 'OPEN', -- OPEN, RESOLVED
    resolution TEXT, -- RENTASST, TALLY, MANUAL
    resolved_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```
