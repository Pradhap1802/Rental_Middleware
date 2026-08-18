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
            endpoints = ["admin/check-required-version", "categories", "health"]
            last_err = None
            for ep in endpoints:
                try:
                    r = client.session.get(f"{client.base_url}/{ep}", headers=client.headers, timeout=12, verify=cfg.verify_ssl)
                    if r.status_code in (200, 204, 401, 403):
                        return {"status": "success", "message": "RentAsst API connection successful"}
                except Exception as ex:
                    last_err = ex
                    continue
            if last_err:
                raise last_err
            return {"status": "error", "message": f"Could not reach RentAsst API at {client.base_url}"}
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
