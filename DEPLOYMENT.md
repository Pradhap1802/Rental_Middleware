# Production Deployment & Administration Guide

This document covers Windows Service installation, secret management & credential encryption, health monitoring endpoints, and database backup procedures.

---

## 1. Windows Service Installation

### Recommended: `Install.bat`

For a client machine, build the standalone package first (see Section 5), then run **`Install.bat`** as Administrator from that package. It calls `scripts/install_service.ps1`, which:
- Registers the compiled `RentalMiddleware.exe` directly with the Service Control Manager (it correctly implements the SCM dispatch protocol — no extra setup needed).
- Falls back to running from source (`service.py` via the project's venv) only if no compiled exe is present, using pywin32's own service installer rather than a generic service registration, since a plain `python.exe` process cannot correctly respond to SCM on its own.
- Configures automatic restart on failure.

Use `Uninstall.bat` to remove the service.

### Alternative: NSSM

If you prefer NSSM (Non-Sucking Service Manager) as a general-purpose process supervisor instead of the bundled installer, it can wrap `run.py` directly:

```powershell
# Create Service
C:\tools\nssm\nssm.exe install RentAsstTallyMiddleware "C:\path\to\Rental_Middleware\venv\Scripts\python.exe" "run.py"

# Set Working Directory
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware AppDirectory "C:\path\to\Rental_Middleware"

# Configure Auto-Start & Recovery
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware Start SERVICE_AUTO_START
C:\tools\nssm\nssm.exe set RentAsstTallyMiddleware AppExit Default Restart

# Start Service
Start-Service RentAsstTallyMiddleware
```

Don't run both installation methods against the same machine at once — pick one.

---

## 2. Security & Secret Management (Task 18)

- **Encryption at Rest**: Configuration secrets (API keys, passwords, bearer tokens) are stored in `.data/config.json.enc` encrypted using Fernet AES-256 (`app/security/encryption.py`).
- **Environment Overrides**: Secret keys can be supplied via environment variables:
  - `RENTASST_API_KEY`: RentAsst Tenant API Token
  - `RENTASST_URL`: RentAsst API Endpoint URL
  - `EXTERNAL_URL`: Tally Prime XML Server Target URL (e.g. `http://localhost:9000`)
- **Credential Masking**: All API keys, passwords, and bearer tokens are automatically masked in log files and web dashboard responses (`app/security/masking.py`).
- **Fresh `.data/` per install**: Every deployment must generate its own `.data/secret.key` and `.data/config.json.enc` — `ConfigStore` creates these automatically on first run if absent. Never copy `.data/` from a development machine or another client's install onto a new one; that would carry over a real bearer token/API key tied to a different RentAsst account. The standalone exe package (Section 5) never bundles `.data/`, so a package build is always a clean slate.
- **Tally company name is case-sensitive**: the middleware's configured `tally_company_name` must exactly match the company name as it appears in Tally (including case) — a mismatch fails every sync with `Could not set 'SVCurrentCompany' to '<name>'` in the logs.

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

---

## 5. Windows Standalone Executable Packaging

You can build the standalone Windows executable package (`RentalMiddleware.exe`) using the included build tool:

```cmd
python build.py
```

### Output

- **Binary Output**: `RentalMiddleware.exe`
- **Output Package**: `dist/RentAsstMiddleware_Windows_v1.0.0.zip`


