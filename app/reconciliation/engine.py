import json
from datetime import datetime, timezone
from typing import Dict, Any, List
from ..mapping.store import MappingStore
from ..logging.logger import log_event


class ReconciliationEngine:
    """
    Dedicated Read-Only Reconciliation Engine for RentAsst ↔ Tally.
    Compares Customers, Equipment, Rental Orders, Invoices, and Payments.
    
    Identifies mismatches without modifying accounting data:
    - MISSING_IN_RENTASST
    - MISSING_IN_TALLY
    - ID_MISMATCH
    - AMOUNT_MISMATCH
    - DATE_MISMATCH
    - TAX_MISMATCH
    - CUSTOMER_MISMATCH
    - VOUCHER_MISMATCH
    """
    def __init__(self, store: MappingStore):
        self.store = store

    @staticmethod
    def _clean_str(val: Any) -> str:
        if val is None:
            return ""
        return str(val).strip()

    @staticmethod
    def _clean_float(val: Any) -> float:
        try:
            return round(float(val), 2)
        except Exception:
            return 0.0

    def compute_macro_totals(
        self,
        ra_invoices: List[Dict[str, Any]],
        tally_invoices: List[Dict[str, Any]],
        ra_payments: List[Dict[str, Any]],
        tally_payments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Calculates macro financial aggregate comparisons for Invoices and Payments."""
        # Invoice totals
        ra_inv_count = len(ra_invoices)
        tally_inv_count = len(tally_invoices)
        ra_inv_total = sum(self._clean_float(i.get("grand_total") or i.get("total_amount") or i.get("amount")) for i in ra_invoices)
        tally_inv_total = sum(self._clean_float(i.get("amount") or i.get("grand_total")) for i in tally_invoices)

        ra_inv_tax = sum(self._clean_float(i.get("tax_amount") or i.get("tax")) for i in ra_invoices)
        tally_inv_tax = sum(self._clean_float(i.get("tax_amount") or i.get("tax")) for i in tally_invoices)

        # Payment totals
        ra_pay_count = len(ra_payments)
        tally_pay_count = len(tally_payments)
        ra_pay_total = sum(self._clean_float(p.get("amount") or p.get("paid_amount")) for p in ra_payments)
        tally_pay_total = sum(self._clean_float(p.get("amount") or p.get("paid_amount")) for p in tally_payments)

        return {
            "invoices": {
                "rentasst_count": ra_inv_count,
                "tally_count": tally_inv_count,
                "count_diff": abs(ra_inv_count - tally_inv_count),
                "rentasst_amount_total": round(ra_inv_total, 2),
                "tally_amount_total": round(tally_inv_total, 2),
                "amount_diff": round(abs(ra_inv_total - tally_inv_total), 2),
                "rentasst_tax_total": round(ra_inv_tax, 2),
                "tally_tax_total": round(tally_inv_tax, 2),
                "tax_diff": round(abs(ra_inv_tax - tally_inv_tax), 2),
            },
            "payments": {
                "rentasst_count": ra_pay_count,
                "tally_count": tally_pay_count,
                "count_diff": abs(ra_pay_count - tally_pay_count),
                "rentasst_amount_total": round(ra_pay_total, 2),
                "tally_amount_total": round(tally_pay_total, 2),
                "amount_diff": round(abs(ra_pay_total - tally_pay_total), 2),
            },
        }

    def reconcile_customers(
        self,
        ra_customers: List[Dict[str, Any]],
        tally_customers: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        discrepancies = []
        tally_map = {self._clean_str(c.get("name")).lower(): c for c in tally_customers if c.get("name")}
        ra_map = {self._clean_str(c.get("name") or c.get("business_name")).lower(): c for c in ra_customers}

        # Check RA in Tally
        for name_lower, ra_c in ra_map.items():
            cid = str(ra_c.get("id"))
            if name_lower not in tally_map:
                discrepancies.append({
                    "entity_type": "customer",
                    "entity_id": cid,
                    "mismatch_type": "MISSING_IN_TALLY",
                    "rentasst_value": ra_c.get("name") or ra_c.get("business_name"),
                    "tally_value": None,
                    "details": f"Customer '{ra_c.get('name')}' exists in RentAsst but not in Tally",
                })

        # Check Tally in RA
        for name_lower, t_c in tally_map.items():
            t_name = t_c.get("name")
            if name_lower not in ra_map:
                discrepancies.append({
                    "entity_type": "customer",
                    "entity_id": t_name,
                    "mismatch_type": "MISSING_IN_RENTASST",
                    "rentasst_value": None,
                    "tally_value": t_name,
                    "details": f"Customer ledger '{t_name}' exists in Tally but not in RentAsst",
                })

        return discrepancies

    def reconcile_invoices(
        self,
        ra_invoices: List[Dict[str, Any]],
        tally_invoices: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        discrepancies = []
        tally_num_map = {self._clean_str(v.get("voucher_number") or v.get("number")).lower(): v for v in tally_invoices}
        ra_num_map = {self._clean_str(i.get("number") or i.get("invoice_number")).lower(): i for i in ra_invoices}

        for num_lower, ra_inv in ra_num_map.items():
            inv_id = str(ra_inv.get("id"))
            if num_lower not in tally_num_map:
                discrepancies.append({
                    "entity_type": "invoice",
                    "entity_id": inv_id,
                    "mismatch_type": "MISSING_IN_TALLY",
                    "rentasst_value": f"Invoice #{ra_inv.get('number')}",
                    "tally_value": None,
                    "details": f"Invoice #{ra_inv.get('number')} exists in RentAsst but not in Tally",
                })
            else:
                t_inv = tally_num_map[num_lower]
                # Compare Amount
                ra_amt = self._clean_float(ra_inv.get("grand_total") or ra_inv.get("total_amount") or ra_inv.get("amount"))
                t_amt = self._clean_float(t_inv.get("amount") or t_inv.get("grand_total"))
                if abs(ra_amt - t_amt) > 0.05:
                    discrepancies.append({
                        "entity_type": "invoice",
                        "entity_id": inv_id,
                        "mismatch_type": "AMOUNT_MISMATCH",
                        "rentasst_value": f"{ra_amt:.2f}",
                        "tally_value": f"{t_amt:.2f}",
                        "details": f"Invoice #{ra_inv.get('number')} amount mismatch: RentAsst {ra_amt:.2f} vs Tally {t_amt:.2f}",
                    })

                # Compare Date
                ra_date = self._clean_str(ra_inv.get("invoice_date") or ra_inv.get("date"))[:10]
                t_date = self._clean_str(t_inv.get("date") or t_inv.get("invoice_date"))[:10]
                if ra_date and t_date and ra_date.replace("-", "") != t_date.replace("-", ""):
                    discrepancies.append({
                        "entity_type": "invoice",
                        "entity_id": inv_id,
                        "mismatch_type": "DATE_MISMATCH",
                        "rentasst_value": ra_date,
                        "tally_value": t_date,
                        "details": f"Invoice #{ra_inv.get('number')} date mismatch: RentAsst {ra_date} vs Tally {t_date}",
                    })

        for num_lower, t_inv in tally_num_map.items():
            if num_lower not in ra_num_map:
                v_no = t_inv.get("voucher_number") or t_inv.get("number")
                discrepancies.append({
                    "entity_type": "invoice",
                    "entity_id": str(v_no),
                    "mismatch_type": "MISSING_IN_RENTASST",
                    "rentasst_value": None,
                    "tally_value": f"Tally Voucher #{v_no}",
                    "details": f"Sales Voucher #{v_no} exists in Tally but not in RentAsst",
                })

        return discrepancies

    def reconcile_payments(
        self,
        ra_payments: List[Dict[str, Any]],
        tally_payments: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        discrepancies = []
        tally_map = {
            self._clean_str(v.get("voucher_number") or v.get("number")).lower(): v
            for v in tally_payments if (v.get("voucher_number") or v.get("number"))
        }
        ra_map = {
            self._clean_str(p.get("reference_id") or p.get("number") or p.get("receipt_number")).lower(): p
            for p in ra_payments if (p.get("reference_id") or p.get("number") or p.get("receipt_number"))
        }

        for ref_lower, ra_p in ra_map.items():
            pid = str(ra_p.get("id"))
            ref_no = ra_p.get("reference_id") or ra_p.get("number") or ra_p.get("receipt_number")
            if ref_lower not in tally_map:
                discrepancies.append({
                    "entity_type": "payment",
                    "entity_id": pid,
                    "mismatch_type": "MISSING_IN_TALLY",
                    "rentasst_value": f"Payment #{ref_no}",
                    "tally_value": None,
                    "details": f"Payment #{ref_no} exists in RentAsst but not in Tally",
                })
            else:
                t_p = tally_map[ref_lower]
                ra_amt = self._clean_float(ra_p.get("amount") or ra_p.get("paid_amount"))
                t_amt = self._clean_float(t_p.get("amount"))
                if abs(ra_amt - t_amt) > 0.05:
                    discrepancies.append({
                        "entity_type": "payment",
                        "entity_id": pid,
                        "mismatch_type": "AMOUNT_MISMATCH",
                        "rentasst_value": f"{ra_amt:.2f}",
                        "tally_value": f"{t_amt:.2f}",
                        "details": f"Payment #{ref_no} amount mismatch: RentAsst {ra_amt:.2f} vs Tally {t_amt:.2f}",
                    })

        for ref_lower, t_p in tally_map.items():
            if ref_lower not in ra_map:
                v_no = t_p.get("voucher_number") or t_p.get("number")
                discrepancies.append({
                    "entity_type": "payment",
                    "entity_id": str(v_no),
                    "mismatch_type": "MISSING_IN_RENTASST",
                    "rentasst_value": None,
                    "tally_value": f"Tally Receipt #{v_no}",
                    "details": f"Receipt Voucher #{v_no} exists in Tally but not in RentAsst",
                })

        return discrepancies

    def reconcile_equipment(
        self,
        ra_equipment: List[Dict[str, Any]],
        tally_equipment: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        discrepancies = []
        tally_map = {self._clean_str(e.get("name")).lower(): e for e in tally_equipment if e.get("name")}
        ra_map = {self._clean_str(e.get("name")).lower(): e for e in ra_equipment if e.get("name")}

        for name_lower, ra_e in ra_map.items():
            eid = str(ra_e.get("id"))
            if name_lower not in tally_map:
                discrepancies.append({
                    "entity_type": "equipment",
                    "entity_id": eid,
                    "mismatch_type": "MISSING_IN_TALLY",
                    "rentasst_value": ra_e.get("name"),
                    "tally_value": None,
                    "details": f"Equipment '{ra_e.get('name')}' exists in RentAsst but not in Tally",
                })

        for name_lower, t_e in tally_map.items():
            t_name = t_e.get("name")
            if name_lower not in ra_map:
                discrepancies.append({
                    "entity_type": "equipment",
                    "entity_id": t_name,
                    "mismatch_type": "MISSING_IN_RENTASST",
                    "rentasst_value": None,
                    "tally_value": t_name,
                    "details": f"Stock item '{t_name}' exists in Tally but not in RentAsst",
                })

        return discrepancies

    def reconcile_rental_orders(
        self,
        ra_rental_orders: List[Dict[str, Any]],
        tally_rental_orders: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        discrepancies = []
        tally_map = {
            self._clean_str(v.get("voucher_number") or v.get("number")).lower(): v
            for v in tally_rental_orders if (v.get("voucher_number") or v.get("number"))
        }
        ra_map = {
            self._clean_str(o.get("number") or o.get("rent_code") or o.get("rent_number")).lower(): o
            for o in ra_rental_orders if (o.get("number") or o.get("rent_code") or o.get("rent_number"))
        }

        for num_lower, ra_o in ra_map.items():
            oid = str(ra_o.get("id"))
            order_no = ra_o.get("number") or ra_o.get("rent_code") or ra_o.get("rent_number")
            if num_lower not in tally_map:
                discrepancies.append({
                    "entity_type": "rental_order",
                    "entity_id": oid,
                    "mismatch_type": "MISSING_IN_TALLY",
                    "rentasst_value": f"Rent Out #{order_no}",
                    "tally_value": None,
                    "details": f"Rent Out #{order_no} exists in RentAsst but not in Tally",
                })

        for num_lower, t_o in tally_map.items():
            if num_lower not in ra_map:
                v_no = t_o.get("voucher_number") or t_o.get("number")
                discrepancies.append({
                    "entity_type": "rental_order",
                    "entity_id": str(v_no),
                    "mismatch_type": "MISSING_IN_RENTASST",
                    "rentasst_value": None,
                    "tally_value": f"Tally Sales Order #{v_no}",
                    "details": f"Sales Order #{v_no} exists in Tally but not in RentAsst",
                })

        return discrepancies

    def run_reconciliation(
        self,
        ra_customers: List[Dict[str, Any]] = None,
        tally_customers: List[Dict[str, Any]] = None,
        ra_invoices: List[Dict[str, Any]] = None,
        tally_invoices: List[Dict[str, Any]] = None,
        ra_payments: List[Dict[str, Any]] = None,
        tally_payments: List[Dict[str, Any]] = None,
        ra_equipment: List[Dict[str, Any]] = None,
        tally_equipment: List[Dict[str, Any]] = None,
        ra_rental_orders: List[Dict[str, Any]] = None,
        tally_rental_orders: List[Dict[str, Any]] = None,
        company_id: str = "default",
    ) -> Dict[str, Any]:
        """
        Executes a read-only reconciliation pass.
        Calculates macro financial totals and records discrepancies in SQLite database.
        """
        ra_cust = ra_customers or []
        t_cust = tally_customers or []
        ra_inv = ra_invoices or []
        t_inv = tally_invoices or []
        ra_pay = ra_payments or []
        t_pay = tally_payments or []
        ra_equip = ra_equipment or []
        t_equip = tally_equipment or []
        ra_rental = ra_rental_orders or []
        t_rental = tally_rental_orders or []

        macro_totals = self.compute_macro_totals(ra_inv, t_inv, ra_pay, t_pay)
        cust_discrepancies = self.reconcile_customers(ra_cust, t_cust)
        inv_discrepancies = self.reconcile_invoices(ra_inv, t_inv)
        pay_discrepancies = self.reconcile_payments(ra_pay, t_pay)
        equip_discrepancies = self.reconcile_equipment(ra_equip, t_equip)
        rental_discrepancies = self.reconcile_rental_orders(ra_rental, t_rental)

        all_discrepancies = (
            cust_discrepancies + inv_discrepancies + pay_discrepancies + equip_discrepancies + rental_discrepancies
        )

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Save run record and discrepancies to DB
        with self.store.db.get_connection() as c:
            summary = {
                "macro_totals": macro_totals,
                "discrepancies_count": len(all_discrepancies),
            }
            cur_run = c.execute(
                """
                INSERT INTO reconciliation_runs (entity_type, run_at, status, summary_json)
                VALUES ('all', ?, 'COMPLETED', ?)
                """,
                (now_iso, json.dumps(summary)),
            )
            run_id = cur_run.lastrowid

            for disc in all_discrepancies:
                c.execute(
                    """
                    INSERT INTO reconciliation_discrepancies (
                        run_id, entity_type, entity_id, mismatch_type,
                        rentasst_value, tally_value, details, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        disc["entity_type"],
                        str(disc.get("entity_id") or ""),
                        disc["mismatch_type"],
                        disc.get("rentasst_value"),
                        disc.get("tally_value"),
                        disc.get("details"),
                        now_iso,
                    ),
                )

        log_event(
            "Reconciliation",
            f"Read-only reconciliation run #{run_id} completed: {len(all_discrepancies)} discrepancies found",
            metadata=summary,
        )

        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "run_at": now_iso,
            "macro_totals": macro_totals,
            "total_discrepancies": len(all_discrepancies),
            "discrepancies": all_discrepancies,
        }
