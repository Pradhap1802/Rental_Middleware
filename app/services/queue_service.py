from typing import Dict, Any, List
from ..queue.queue_store import QueueStore


class QueueService:
    def __init__(self, data_dir: str):
        self.queue_store = QueueStore(f"{data_dir}/state.db")

    def get_metrics(self) -> Dict[str, int]:
        return self.queue_store.get_metrics()

    def retry_failed(self) -> int:
        return self.queue_store.retry_failed_jobs()

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.queue_store.list_recent_jobs(limit=limit)
