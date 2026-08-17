import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .configuration.store import ConfigStore
from .scheduler.manager import SyncScheduler
from .queue.worker import QueueWorker
from .services.sync_service import SyncService
from .logging.logger import log_event
from .dashboard import dashboard_router
from .api import all_routers

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DATA_DIR = os.path.join(BASE_DIR, ".data")
os.makedirs(DATA_DIR, exist_ok=True)


# Shared instance singletons
sync_service = SyncService(DATA_DIR)
scheduler = SyncScheduler(DATA_DIR)
worker = QueueWorker(DATA_DIR, sync_executor=sync_service.execute_sync, max_workers=4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    app.state.data_dir = DATA_DIR
    app.state.scheduler = scheduler
    app.state.worker = worker

    # Start background Queue Worker
    worker.start()

    # Load configuration and determine sync interval
    cfg_store = ConfigStore(DATA_DIR)
    cfg = cfg_store.load_safe()
    interval = cfg.sync_interval_minutes if cfg and cfg.sync_interval_minutes else 10

    # Always start the polling scheduler (10-minute default)
    scheduler.start(interval)
    log_event("Startup", f"Auto-sync polling started: syncing Customers, Equipment, Rental Orders, Invoices, Payments every {interval} minutes.")

    # Fire an immediate first sync so data syncs right away on boot
    try:
        scheduler.trigger_manual_sync()
        log_event("Startup", "Immediate first forward sync triggered on startup.")
    except Exception as e:
        log_event("Startup", f"Initial sync trigger warning: {e}")

    yield

    # Application shutdown
    scheduler.stop()
    worker.stop()


app = FastAPI(
    title="RentAsst Middleware Service",
    version="1.0.0",
    description="High-performance, modular integration gateway for RentAsst, Tally Prime, and external ERPs.",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
def value_error_handler(request, exc: ValueError):
    return JSONResponse(status_code=400, content={"status": "error", "message": str(exc)})


# Mount Dashboard UI Router
app.include_router(dashboard_router)

# Mount Modular API Routers
for r in all_routers:
    app.include_router(r)
