import concurrent.futures
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
            urls = [f"{base}/{ep}".rstrip("/") for ep in endpoints]

            # Probe every candidate endpoint concurrently instead of sequentially -
            # only one is actually correct for a given deployment, so waiting out
            # each candidate's full timeout in turn wastes most of the wall-clock time.
            outcome = None
            last_code = None
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls), 4))
            futures = {executor.submit(client.session.get, u, headers=client.headers, timeout=8, verify=cfg.verify_ssl): u for u in urls}
            try:
                for fut in concurrent.futures.as_completed(futures, timeout=10):
                    try:
                        r = fut.result()
                    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                        continue
                    except Exception:
                        continue
                    if r.status_code in (200, 201, 204):
                        outcome = ("success", "RentAsst API connection successful")
                        break
                    elif r.status_code in (401, 403):
                        outcome = ("error", f"Connected to RentAsst API, but authentication failed (Status {r.status_code}). Please check your API key / login credentials.")
                        break
                    last_code = r.status_code
            except concurrent.futures.TimeoutError:
                pass
            finally:
                executor.shutdown(wait=False)

            if outcome:
                status, message = outcome
                return {"status": status, "message": message}

            # Fallback: check root server URL (e.g. http://localhost:8000)
            try:
                root_url = base.replace("/api", "")
                r = client.session.get(root_url, timeout=8, verify=cfg.verify_ssl)
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
