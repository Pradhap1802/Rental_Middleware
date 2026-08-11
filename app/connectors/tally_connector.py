import functools
from typing import Dict, Any, Optional
from .base_connector import BaseConnector, ConnectorResponse
from ..clients.external_client import ExternalClient
from ..models.domain import AppConfig


def generate_tally_xml_envelope(action: str, entity_type: str, voucher_type: str, company_name: str = "") -> str:
    """Template generator for Tally XML envelopes with dynamic target company injection."""
    company_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY></STATICVARIABLES>" if company_name else ""
    return (
        f'<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>'
        f'<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>'
        f'{company_var}'
        f'</REQUESTDESC><REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">'
        f'<!-- {action} {entity_type} {voucher_type} -->'
        f'</TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>'
    )



class TallyConnector(BaseConnector):
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.client = ExternalClient(cfg)

    def connect(self) -> bool:
        return self.health_check()

    def disconnect(self) -> None:
        self.client.close()

    def health_check(self) -> bool:
        return self.client.ping()

    def sync(self, entity_type: str, payload: Dict[str, Any]) -> ConnectorResponse:
        try:
            # Delegate HTTP communication to ExternalClient while utilizing LRU XML envelope helpers
            envelope = generate_tally_xml_envelope("SYNC", entity_type, "Voucher")
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
            return ConnectorResponse(success=True, external_id=str(ext_id), data={"xml_meta": envelope, "payload": payload})
        except Exception as e:
            return ConnectorResponse(success=False, error=str(e), data=payload)

    def update(self, entity_type: str, external_id: str, payload: Dict[str, Any]) -> ConnectorResponse:
        return self.sync(entity_type, payload)

    def delete(self, entity_type: str, external_id: str) -> ConnectorResponse:
        return ConnectorResponse(success=True, external_id=external_id)
