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
            endpoints = ["health", "user/profile", "user", "customer", "customers", ""]
            base = client.base_url.rstrip("/")
            last_code = None
            for ep in endpoints:
                url = f"{base}/{ep}".rstrip("/")
                try:
                    r = client.session.get(url, headers=client.headers, timeout=15, verify=cfg.verify_ssl)
                    if r.status_code in (200, 201, 204):
                        return {"status": "success", "message": "RentAsst API connection successful"}
                    elif r.status_code in (401, 403):
                        return {"status": "error", "message": f"Connected to RentAsst API, but authentication failed (Status {r.status_code}). Please check your API key / login credentials."}
                    last_code = r.status_code
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                    continue
                except Exception:
                    continue

            # Fallback: check root server URL (e.g. http://localhost:8000)
            try:
                root_url = base.replace("/api", "")
                r = client.session.get(root_url, timeout=10, verify=cfg.verify_ssl)
                if r.status_code in (200, 204, 301, 302, 404):
                    return {"status": "success", "message": "RentAsst server is reachable and active."}
            except Exception:
                pass

            if last_code:
                return {"status": "error", "message": f"RentAsst API at {client.base_url} returned status code {last_code}"}
            return {"status": "error", "message": f"Could not connect to RentAsst API at {cfg.rentasst_url}. Ensure RentalApi server is running."}
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
