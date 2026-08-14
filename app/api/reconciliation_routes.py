import json
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from ..mapping.store import MappingStore
from ..reconciliation.engine import ReconciliationEngine

reconciliation_router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


def get_engine(request: Request) -> ReconciliationEngine:
    store = getattr(request.app.state, "mapping_store", None)
    if not store:
        db_path = getattr(request.app.state, "db_path", "data/state.db")
        store = MappingStore(db_path)
    return ReconciliationEngine(store)


@reconciliation_router.post("/run", response_model=Dict[str, Any])
def trigger_reconciliation(request: Request):
    """Triggers a read-only reconciliation pass comparing RentAsst and Tally records."""
    engine = get_engine(request)
    
    # Safely fetch active clients if present
    ra_client = getattr(request.app.state, "ra_client", None)
    ext_client = getattr(request.app.state, "ext_client", None)

    ra_cust, t_cust, ra_inv, t_inv, ra_pay, t_pay = [], [], [], [], [], []

    if ra_client and hasattr(ra_client, "fetch_customers"):
        try:
            ra_cust = ra_client.fetch_customers() or []
        except Exception:
            pass
        try:
            ra_inv = ra_client.fetch_invoices() or []
        except Exception:
            pass
        try:
            ra_pay = ra_client.fetch_payments() or []
        except Exception:
            pass

    if ext_client and hasattr(ext_client, "tally"):
        try:
            t_cust = ext_client.tally.fetch_companies() or []
        except Exception:
            pass

    result = engine.run_reconciliation(
        ra_customers=ra_cust,
        tally_customers=t_cust,
        ra_invoices=ra_inv,
        tally_invoices=t_inv,
        ra_payments=ra_pay,
        tally_payments=t_pay,
    )
    return result


@reconciliation_router.get("/summary", response_model=Dict[str, Any])
def get_reconciliation_summary(request: Request):
    """Returns macro financial totals and count comparison from the most recent reconciliation run."""
    engine = get_engine(request)
    with engine.store.db.get_connection() as c:
        cur = c.execute("SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return {
                "status": "NO_RUNS",
                "macro_totals": engine.compute_macro_totals([], [], [], []),
                "total_discrepancies": 0,
            }
        
        summary_data = json.loads(row["summary_json"]) if row["summary_json"] else {}
        return {
            "run_id": row["id"],
            "run_at": row["run_at"],
            "status": row["status"],
            "macro_totals": summary_data.get("macro_totals"),
            "total_discrepancies": summary_data.get("discrepancies_count", 0),
        }


@reconciliation_router.get("/discrepancies", response_model=Dict[str, Any])
def list_discrepancies(
    request: Request,
    entity_type: Optional[str] = Query(None, description="Filter by entity_type (customer, invoice, payment, equipment)"),
    mismatch_type: Optional[str] = Query(None, description="Filter by mismatch_type (MISSING_IN_TALLY, MISSING_IN_RENTASST, AMOUNT_MISMATCH, DATE_MISMATCH)"),
    run_id: Optional[int] = Query(None, description="Filter by specific reconciliation run ID"),
):
    """Lists itemized discrepancies from reconciliation runs."""
    engine = get_engine(request)
    query = "SELECT * FROM reconciliation_discrepancies WHERE 1=1"
    params = []

    if run_id:
        query += " AND run_id=?"
        params.append(run_id)
    if entity_type:
        query += " AND entity_type=?"
        params.append(entity_type)
    if mismatch_type:
        query += " AND mismatch_type=?"
        params.append(mismatch_type)

    query += " ORDER BY id DESC LIMIT 100"

    with engine.store.db.get_connection() as c:
        cur = c.execute(query, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]

    return {"total": len(rows), "discrepancies": rows}


@reconciliation_router.get("/runs", response_model=Dict[str, Any])
def list_reconciliation_runs(request: Request, limit: int = Query(20, le=100)):
    """Lists past reconciliation audit runs."""
    engine = get_engine(request)
    with engine.store.db.get_connection() as c:
        cur = c.execute("SELECT id, entity_type, run_at, status, summary_json FROM reconciliation_runs ORDER BY id DESC LIMIT ?", (limit,))
        runs = []
        for r in cur.fetchall():
            rd = dict(r)
            if rd.get("summary_json"):
                try:
                    rd["summary"] = json.loads(rd["summary_json"])
                except Exception:
                    pass
            runs.append(rd)
    return {"total": len(runs), "runs": runs}
