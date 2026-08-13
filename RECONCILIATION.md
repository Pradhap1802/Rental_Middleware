# Reconciliation Engine & Conflict Resolution

This document details the read-only reconciliation engine, macro total auditing, discrepancy classifications, and bidirectional conflict resolution policy.

---

## Read-Only Reconciliation Engine (Task 15)

The reconciliation engine ([app/reconciliation/engine.py](file:///c:/Users/PradhapM/Music/Rental_Middleware/app/reconciliation/engine.py)) performs read-only comparisons between RentAsst Cloud ERP datasets and Tally Prime accounting records.

### Key Principles
- **Read-Only Default**: Reconciliation audits NEVER modify accounting data automatically.
- **Cross-Entity Scope**: Audits Customers, Equipment, Rental Orders, Invoices, and Payments.
- **Macro Totals & Audit Metrics**: Compares total record counts, gross amounts, and tax totals across both systems.

---

## Discrepancy Types & Classification

When `ReconciliationEngine.run_reconciliation()` executes, it detects and logs the following discrepancy types in `reconciliation_discrepancies`:

| Discrepancy Type | Description |
| :--- | :--- |
| `MISSING_IN_RENTASST` | Voucher exists in Tally Prime but has no corresponding record in RentAsst Cloud ERP. |
| `MISSING_IN_TALLY` | Invoice or Payment exists in RentAsst Cloud ERP but has no corresponding voucher in Tally. |
| `ID_MISMATCH` | Record mapped to an invalid or non-existent external ID. |
| `AMOUNT_MISMATCH` | Grand total in RentAsst differs from Voucher amount in Tally (e.g. RentAsst = ₹5,900 vs Tally = ₹5,000). |
| `DATE_MISMATCH` | Invoice date in RentAsst differs from Voucher date in Tally. |
| `TAX_MISMATCH` | Tax total breakdown in RentAsst differs from Ledger tax total in Tally. |

---

## Bidirectional Field Ownership Policy (Task 12)

To prevent uncontrolled overwrites during bidirectional sync, the middleware enforces explicit field ownership:

| Entity Field | Authoritative System | Direction |
| :--- | :--- | :--- |
| Customer Name | **RentAsst** | Forward (RentAsst $\to$ Tally) |
| Customer Mobile | **RentAsst** | Forward (RentAsst $\to$ Tally) |
| Customer GSTIN | **RentAsst** | Forward (RentAsst $\to$ Tally) |
| Accounting Opening Balance | **Tally** | Reverse (Tally $\to$ RentAsst) |
| Ledger Closing Balance | **Tally** | Reverse (Tally $\to$ RentAsst) |
| Voucher Number | **Tally** | Reverse (Tally $\to$ RentAsst) |
| Invoice Subtotal / Tax / Total | **RentAsst** | Forward (RentAsst $\to$ Tally) |

---

## Conflict Detection & Human Resolution (Task 13)

When both RentAsst and Tally update the same record field since the last successful sync:

1. `ConflictDetector` logs a conflict in `sync_conflicts` table (`status='OPEN'`).
2. Does NOT silently overwrite either value.
3. Exposes Conflict Resolution API and Dashboard Interface:
   - **View Open Conflicts**: `GET /api/conflicts`
   - **Resolve Conflict**: `POST /api/conflicts/resolve`
     - Parameters: `conflict_id`, `resolution` (`RENTASST` or `TALLY`).
     - Applies chosen value to target system and marks conflict `RESOLVED`.
