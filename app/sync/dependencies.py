from typing import Dict, Any, Tuple, Optional
from ..mapping.store import MappingStore


class MissingDependencyException(Exception):
    """
    Exception raised when a required parent dependency mapping (e.g. Customer for Invoice, Invoice for Payment)
    has not yet completed synchronization. Causes job to transition to WAITING_FOR_DEPENDENCY state.
    """
    def __init__(self, message: str, missing_entity: Optional[str] = None, missing_id: Optional[str] = None):
        super().__init__(message)
        self.missing_entity = missing_entity
        self.missing_id = missing_id


class DependencyResolver:
    """
    Dependency resolver enforcing sync execution hierarchy:
    Customer -> Equipment -> Rental Order -> Invoice -> Payment
    """

    @staticmethod
    def check_dependencies(
        entity_type: str,
        data: Dict[str, Any],
        store: MappingStore,
        source_company_id: str = "default",
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        Checks if required parent mappings exist in SQLite mapping store.
        Returns (has_deps: bool, missing_reason: Optional[str], missing_entity: Optional[str], missing_id: Optional[str]).
        """
        ent = (entity_type or "").strip().lower()

        # 1. Invoice Dependency Check (Requires Customer mapping)
        if ent in ("invoice", "invoices"):
            cust_id = data.get("customer_id") or (data.get("customer") or {}).get("id")
            if cust_id:
                cust_str = str(cust_id).strip()
                has_cust = store.find_mapping("customer", cust_str, source_company_id=source_company_id) or store.get_rentasst_id("customer", cust_str)
                if not has_cust:
                    cust_name = (data.get("customer") or {}).get("name") or data.get("customer_name")
                    if cust_name and store.get_rentasst_id("customer", cust_name):
                        has_cust = True

                if not has_cust:
                    reason = f"Missing Customer dependency mapping (Customer ID: '{cust_str}') for Invoice sync"
                    return False, reason, "customer", cust_str

        # 2. Payment Dependency Check (Requires Invoice or Customer mapping)
        elif ent in ("payment", "payments"):
            inv_id = data.get("invoice_id")
            cust_id = data.get("customer_id")

            if inv_id:
                inv_str = str(inv_id).strip()
                has_inv = (
                    store.find_mapping("invoice", inv_str, source_company_id=source_company_id)
                    or store.find_mapping("rental_orders", inv_str, source_company_id=source_company_id)
                    or store.get_rentasst_id("invoice", inv_str)
                )
                if not has_inv:
                    reason = f"Missing Invoice dependency mapping (Invoice ID: '{inv_str}') for Payment sync"
                    return False, reason, "invoice", inv_str

            elif cust_id:
                cust_str = str(cust_id).strip()
                has_cust = store.find_mapping("customer", cust_str, source_company_id=source_company_id) or store.get_rentasst_id("customer", cust_str)
                if not has_cust:
                    reason = f"Missing Customer dependency mapping (Customer ID: '{cust_str}') for Payment sync"
                    return False, reason, "customer", cust_str

        # 3. Rental Order Dependency Check (Requires Customer mapping)
        elif ent in ("rental_order", "rental_orders"):
            cust_id = data.get("customer_id") or (data.get("customer") or {}).get("id")
            if cust_id:
                cust_str = str(cust_id).strip()
                has_cust = store.find_mapping("customer", cust_str, source_company_id=source_company_id) or store.get_rentasst_id("customer", cust_str)
                if not has_cust:
                    cust_name = data.get("customer_name") or (data.get("customer") or {}).get("name")
                    if cust_name and store.get_rentasst_id("customer", cust_name):
                        has_cust = True

                if not has_cust:
                    reason = f"Missing Customer dependency mapping (Customer ID: '{cust_str}') for Rental Order sync"
                    return False, reason, "customer", cust_str

        # 4. Equipment Dependency Check (Requires Asset Unit mapping if custom unit specified)
        elif ent in ("equipment", "product", "products", "asset", "assets"):
            unit_name = ""
            unit_id = ""
            if isinstance(data.get("asset_unit"), dict):
                unit_name = (data["asset_unit"].get("name") or "").strip()
                unit_id = str(data["asset_unit"].get("id") or "").strip()
            elif data.get("asset_unit_name"):
                unit_name = str(data.get("asset_unit_name")).split("(")[0].strip()
            elif data.get("unit"):
                unit_name = str(data.get("unit")).strip()

            if unit_name and unit_name.lower() not in ("nos", "numbers", "pcs", "piece", "pieces", "units", "unit", "primary", ""):
                has_unit = (
                    (unit_id and store.find_mapping("units", unit_id, source_company_id=source_company_id))
                    or store.find_mapping("units", unit_name, source_company_id=source_company_id)
                    or store.get_rentasst_id("units", unit_name)
                    or store.get_rentasst_id("unit", unit_name)
                    or (unit_id and store.get_rentasst_id("units", unit_id))
                )
                if not has_unit:
                    reason = f"Missing Asset Unit dependency mapping (Unit: '{unit_name}') for Equipment sync"
                    return False, reason, "units", unit_name


        return True, None, None, None

