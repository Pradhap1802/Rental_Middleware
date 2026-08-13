from typing import Dict, Any, Optional
from .base_connector import BaseConnector, ConnectorResponse
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

    def sync(self, entity_type: str, payload: Dict[str, Any]) -> ConnectorResponse:
        try:
            if entity_type == "customer":
                ext_id = self.client.sync_customer(payload)
            elif entity_type == "equipment":
                ext_id = self.client.sync_equipment(payload)
            elif entity_type == "rental_order":
                ext_id = self.client.sync_rental_order(payload)
            elif entity_type == "invoice":
                ext_id = self.client.sync_invoice(payload)
            elif entity_type == "payment":
                ext_id = self.client.sync_payment(payload)
            else:
                ext_id = self.client.sync_customer(payload)
            return ConnectorResponse(success=True, external_id=str(ext_id), data=payload)
        except Exception as e:
            return ConnectorResponse(success=False, error=str(e), data=payload)

    def update(self, entity_type: str, external_id: str, payload: Dict[str, Any]) -> ConnectorResponse:
        return self.sync(entity_type, payload)

    def delete(self, entity_type: str, external_id: str) -> ConnectorResponse:
        return ConnectorResponse(success=True, external_id=external_id)
