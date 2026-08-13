# Production Deployment & Administration Guide

This document covers Windows Service installation (`nssm`), secret management & credential encryption, health monitoring endpoints, and database backup procedures.

---

## 1. Windows Service Installation via NSSM

To run the middleware as a resilient background Windows Service that starts automatically on boot:

### Step 1: Download NSSM
Download NSSM (Non-Sucking Service Manager) and place `nssm.exe` in `C:\tools\nssm\`.

### Step 2: Install Windows Service
Open PowerShell as Administrator:

```powershell
# Create Service
C:\tools\nssm\nssm.exe install RentAsstTallyMiddleware "C:\Users\PradhapM\Music\Rental_Middleware\venv\Scripts\python.exe" "run.py"

# Set Working Directory
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware AppDirectory "C:\Users\PradhapM\Music\Rental_Middleware"

# Configure Auto-Start & Recovery
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware Start SERVICE_AUTO_START
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware AppExit Default Restart

# Start Service
Start-Service RentAsstTallyMiddleware
```

---

## 2. Security & Secret Management (Task 18)

- **Encryption at Rest**: Configuration secrets (API keys, passwords, bearer tokens) are stored in `.data/config.json.enc` encrypted using Fernet AES-256 (`app/security/encryption.py`).
- **Environment Overrides**: Secret keys can be supplied via environment variables:
  - `RENTASST_API_KEY`: RentAsst Tenant API Token
  - `RENTASST_URL`: RentAsst API Endpoint URL
  - `EXTERNAL_URL`: Tally Prime XML Server Target URL (e.g. `http://localhost:9000`)
- **Credential Masking**: All API keys, passwords, and bearer tokens are automatically masked in log files and web dashboard responses (`app/security/masking.py`).

---

## 3. Production Health Endpoints (Task 20)

Integrate with Kubernetes or load balancer health probes:

- **`/health/live` (Liveness Probe)**: Returns HTTP 200 `{"status": "UP"}` if application process is running.
- **`/health/ready` (Readiness Probe)**: Returns HTTP 200 `{"status": "READY"}` if Database and Worker pool are functional (returns HTTP 503 if unreachable).
- **`/health` (Comprehensive Probe)**: Probes Database, RentAsst API, Tally Prime XML server, Worker state, and Scheduler status with zero credential exposure.

---

## 4. Production Database Backup Strategy (Task 25)

- **Daily Scheduled Backup**: Background job automatically creates verified SQLite backups every 24 hours in `.data/backups/`.
- **Backup Verification**: Validates SQLite magic header bytes and executes `PRAGMA quick_check;`.
- **Pre-Restore Safety Snapshot**: Before restoring a database snapshot (`POST /api/backups/restore/{filename}`), a safety snapshot (`state_prerestore_<timestamp>.db`) is created automatically.
- **Retention**: Purges backups older than 30 days or exceeding 10 files.
