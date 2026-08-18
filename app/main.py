import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .configuration.store import ConfigStore
from .scheduler.manager import SyncScheduler
from .queue.worker import QueueWorker
from .services.sync_service import SyncService
from .mapping.store import MappingStore
from .clients.rentasst_client import RentAsstClient
from .clients.external_client import ExternalClient
from .dashboard import dashboard_router
from .api import all_routers

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DATA_DIR = os.path.join(BASE_DIR, ".data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "state.db")


# Shared instance singletons
sync_service = SyncService(DATA_DIR)
scheduler = SyncScheduler(DATA_DIR)
worker = QueueWorker(DATA_DIR, sync_executor=sync_service.execute_sync, max_workers=4)
mapping_store = MappingStore(DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    app.state.data_dir = DATA_DIR
    app.state.scheduler = scheduler
    app.state.worker = worker
    app.state.db_path = DB_PATH
    app.state.mapping_store = mapping_store
    app.state.db = mapping_store.db

    # Start background Queue Worker
    worker.start()

    # Load configuration, wire up connectivity-check clients, and start the
    # background scheduler if auto-sync is enabled
    cfg_store = ConfigStore(DATA_DIR)
    cfg = cfg_store.load_safe()
    if cfg:
        app.state.ra_client = RentAsstClient(cfg)
        app.state.ext_client = ExternalClient(cfg)
        if cfg.auto_sync_enabled:
            scheduler.start(cfg.sync_interval_minutes)

    yield

    # Application shutdown
    scheduler.stop()
    worker.stop()
    for attr in ("ra_client", "ext_client"):
        client = getattr(app.state, attr, None)
        if client:
            client.close()


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
