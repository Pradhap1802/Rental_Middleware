import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .configuration.store import ConfigStore
from .scheduler.manager import SyncScheduler
from .queue.worker import QueueWorker
from .services.sync_service import SyncService
from .dashboard import dashboard_router
from .api import all_routers

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".data"))
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

    # Load configuration and start background scheduler if auto-sync is enabled
    cfg_store = ConfigStore(DATA_DIR)
    cfg = cfg_store.load_safe()
    if cfg and cfg.auto_sync_enabled:
        scheduler.start(cfg.sync_interval_minutes)

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
