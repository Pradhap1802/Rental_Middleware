import requests
from typing import Dict, Any
from ..configuration.store import ConfigStore
from ..clients.rentasst_client import RentAsstClient
from ..connectors.factory import ConnectorFactory
from ..connectors.base_connector import BaseConnector


class TestService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.config_store = ConfigStore(data_dir)

    def test_rentasst(self) -> Dict[str, Any]:
        cfg = self.config_store.require()
        client = RentAsstClient(cfg)
        try:
            r = requests.get(f"{client.base_url}/health", headers=client.headers, timeout=5, verify=cfg.verify_ssl)
            if r.status_code in (200, 204):
                return {"status": "success", "message": "RentAsst API connection successful"}
            return {"status": "error", "message": f"RentAsst API at {client.base_url} returned status code {r.status_code}"}
        except Exception as e:
            return {"status": "error", "message": f"Could not connect to RentAsst API at {cfg.rentasst_url}. Ensure RentalApi server is running. (Error: {str(e)})"}
        finally:
            client.close()

    def test_external(self) -> Dict[str, Any]:
        cfg = self.config_store.require()
        connector: BaseConnector = ConnectorFactory.create_connector(cfg)
        try:
            if connector.health_check():
                return {"status": "success", "message": "External system connection successful"}
            return {"status": "error", "message": "Could not connect to external system"}
        finally:
            connector.disconnect()
