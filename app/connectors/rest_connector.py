from .base_connector import BaseConnector
from ..models.domain import AppConfig

class RestConnector(BaseConnector):
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        from ..clients.external_client import ExternalClient
        self.client = ExternalClient(cfg)

    def connect(self) -> bool:
        return self.health_check()

    def disconnect(self) -> None:
        self.client.close()

    def health_check(self) -> bool:
        return self.client.ping()
