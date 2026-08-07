# RentAsst Standalone Middleware Service

## Overview
Standalone Python FastAPI middleware service for **RentAsst** (`RentalApi` Laravel system). Integrates RentAsst with external accounting platforms, Tally Prime, payment gateways, and external ERPs without any dependency on Odoo.

## Features
- **Encrypted Local Config**: Stores API tokens and endpoints securely in `.data/config.json.enc` using Fernet symmetric encryption.
- **Local SQLite State Engine**: `state.db` maintains idempotent ID mappings (`rentasst_id` <-> `external_id`), checkpoints, and dead-letter queues.
- **Entity Sync Modules**: Modular handlers for Customers, Equipment/Products, Rental Orders/Contracts, Invoices, and Payments.
- **Embedded Web Dashboard**: Accessible at `http://localhost:8088` for connection testing, settings configuration, manual sync triggers, and real-time logs.
- **Background Scheduler**: Powered by `APScheduler` for automated recurring sync jobs.

## Quick Start (Development)

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Run Server**:
   ```bash
   python run.py
   ```
   Or via `start.bat` / `start.ps1`.

3. **Access Dashboard**:
   Open browser at [http://localhost:8088](http://localhost:8088).

## Endpoints

- `GET /`: Embedded Dashboard UI
- `GET /api/config` & `POST /api/config`: Load / Save encrypted configuration
- `POST /api/test/rentasst`: Test connection to RentAsst API
- `POST /api/test/external`: Test connection to External System
- `POST /api/sync/customers`: Sync customers
- `POST /api/sync/equipment`: Sync rental equipment
- `POST /api/sync/rental_orders`: Sync rental contracts/orders
- `POST /api/sync/invoices`: Sync billing & invoices
- `POST /api/sync/payments`: Sync payments & security deposits
- `GET /api/deadletters`: Inspect dead letter sync errors
