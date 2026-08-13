# Error Handling, Retries & Dead-Letter Queue (DLQ)

This document details error classification, exponential backoff retry calculations, `WAITING_FOR_DEPENDENCY` state mechanics, Dead-Letter Queueing, and startup crash recovery.

---

## 1. Error Classification

The middleware distinguishes between **retryable transient errors** and **non-retryable permanent errors**:

### Retryable Transient Errors
- Socket timeout / Connection refusal from Tally Prime XML server.
- HTTP 502 / 503 / 504 Service Unavailable from RentAsst REST API.
- Temporary database lock contention (`sqlite3.OperationalError: database is locked`).
- Missing parent dependency mapping (`WAITING_FOR_DEPENDENCY`).

**Behavior**: Job is retained in `sync_queue` with status `RETRYING` or `WAITING_FOR_DEPENDENCY` and assigned exponential backoff delay.

### Non-Retryable Permanent Errors
- Invoice math validation failure (`subtotal + tax != grand_total`).
- Missing required fields (e.g. missing `customer_id` or `order_date`).
- Invalid XML schema payload.
- Max retries exceeded (`attempt_count >= max_attempts`).

**Behavior**: Job status transitions to `DLQ` and entry is written to `dead_letters` table.

---

## 2. Exponential Backoff Calculation

Retry delays are calculated using exponential backoff with full jitter to prevent thundering herd problems:

$$\text{delay} = \min\left(\text{max\_delay},\, \text{base\_delay} \times 2^{\text{attempt}} + \text{uniform\_jitter}\right)$$

Where:
- $\text{base\_delay} = 10\text{ seconds}$
- $\text{max\_delay} = 3600\text{ seconds}$ (1 hour)
- $\text{max\_attempts} = 3$ (Default)

---

## 3. Dependency Waiter State (`WAITING_FOR_DEPENDENCY`)

When an `Invoice` is enqueued before its parent `Customer` mapping exists, or a `Payment` is enqueued before its parent `Invoice` mapping exists:

1. `DependencyResolver` detects missing parent mapping.
2. Job is NOT sent to DLQ.
3. Job status is updated to `WAITING_FOR_DEPENDENCY`.
4. `next_retry_at` is set to `CURRENT_TIMESTAMP + 60s`.
5. Once parent mapping is synced by worker pool, subsequent retry succeeds seamlessly.

---

## 4. Dead-Letter Queue (DLQ) Lifecycle

Permanent failures are written to the `dead_letters` table:

```json
{
  "dl_id": 42,
  "job_id": 105,
  "entity_type": "invoice",
  "source_id": "INV-MATH-ERR",
  "error_type": "ValidationError",
  "error_message": "Invoice math validation failure: subtotal (500) + tax (90) != grand_total (9999)",
  "status": "OPEN"
}
```

### DLQ Management via API & Dashboard
- **View DLQ**: `GET /api/deadletter`
- **Requeue Item**: `POST /api/deadletter/requeue/{dl_id}`
  - Parses payload, sets job status back to `PENDING` with priority, and updates DLQ status to `RESOLVED`.
- **Bulk Requeue**: `POST /api/deadletter/requeue-all`

---

## 5. Worker Startup Crash Recovery (Task 16)

If the middleware process terminates unexpectedly (e.g. power failure, Windows reboot, Python process kill):

1. On application startup, `QueueWorker` and `QueueStore` run `recover_crashed_jobs(stale_threshold_seconds=300)`.
2. Identifies jobs stuck in `PROCESSING` status for longer than 5 minutes.
3. Purges stale expired locks from `sync_locks`.
4. Safely transitions stuck jobs back to `RETRYING` (if `attempt_count < max_attempts`) or `DLQ` (if max attempts reached).
5. Guarantees zero orphaned jobs or lost items after system restart.
