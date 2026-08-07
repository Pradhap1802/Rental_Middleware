from .config_routes import router as config_router
from .test_routes import router as test_router
from .sync_routes import router as sync_router
from .deadletter_routes import router as deadletter_router
from .queue_routes import router as queue_router
from .scheduler_routes import router as scheduler_router
from .status_routes import router as status_router
from .log_routes import router as log_router
from .backup_routes import router as backup_router

all_routers = [
    config_router,
    test_router,
    sync_router,
    deadletter_router,
    queue_router,
    scheduler_router,
    status_router,
    log_router,
    backup_router,
]

__all__ = ["all_routers"]
