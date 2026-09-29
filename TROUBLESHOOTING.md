# Operational Troubleshooting Guide

This guide provides diagnostic steps for common operational issues, connection failures, DLQ requeueing, and stale lock resolution.

---

## 1. Tally Connection Refused (`http://localhost:9000`)

### Symptoms
- Log output: `Tally XML Server connection refused on 127.0.0.1:9000`.
- Dashboard status: Tally Prime Server shows `DOWN`.

### Diagnostic & Resolution Steps
1. **Verify Tally Prime is Running**: Open Tally Prime application on target machine.
2. **Check Tally HTTP Server Setting**:
   - In Tally Prime, press `F1: Help` $\to$ `Settings` $\to$ `Connectivity`.
   - Ensure **Client/Server Configuration** is set to `Both` or `Server`.
   - Ensure **Port** is set to `9000` (or matching `EXTERNAL_URL` setting).
3. **Test Tally Server Port via PowerShell**:
   ```powershell
   Test-NetConnection -ComputerName localhost -Port 9000
   ```
4. **Test Tally XML Ping Endpoint**:
   ```powershell
   curl -X POST http://localhost:9000 -Data '<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>Company List</ID></HEADER><BODY><DESC/></BODY></ENVELOPE>'
   ```

---

## 2. Dead-Letter Queue (DLQ) Items & Requeueing

### Symptoms
- Dashboard shows Failed/DLQ jobs $> 0$.
- Log output: `Payload validation failed for invoice #...`.

### Diagnostic & Resolution Steps
1. **List DLQ Items via API**:
   ```powershell
   curl http://localhost:8000/api/deadletter
   ```
2. **Inspect Error Message**:
   - If error is `Invoice math validation failure`: Update the source invoice in RentAsst Cloud ERP to ensure `subtotal + tax = grand_total`.
3. **Requeue Item**:
   ```powershell
   curl -X POST http://localhost:8000/api/deadletter/requeue/105
   ```

---

## 3. Stale Processing Locks (`sync_locks`)

### Symptoms
- Log output: `Item 'default:customer:forward:999' is currently locked by another active worker. Skipping concurrent execution.`

### Diagnostic & Resolution Steps
1. **Automatic Purge**: Expired locks automatically purge after 300 seconds (5 minutes).
2. **Manual Startup Purge**: Restarting the middleware service executes `recover_crashed_jobs()` and purges all expired locks.

---

## 4. Credential Masking in Logs

### Verification
All passwords, API keys, and bearer tokens are automatically masked across all log files:
- Raw log example: `[INFO] Connecting to RentAsst with Token: ********-****-****-****-********a1z5`
- API key response example: `{"rentasst_api_key": "********a1z5"}`
