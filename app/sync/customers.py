import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline, filter_by_date_range

logger = logging.getLogger(__name__)


def _normalize_ledger_name(name: Optional[str]) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _customer_completeness_score(item: Dict[str, Any]) -> Tuple[bool, bool, bool, bool]:
    """
    Ranks a RentAsst customer record by how complete its Tally-relevant contact data
    is — higher (more True values) is better. Used by _dedupe_by_tally_name to pick a
    winner when two RentAsst customers share the same name.
    """
    has_address = bool(item.get("address") or item.get("addresses"))
    has_real_mobile = len("".join(ch for ch in str(item.get("mobile") or "") if ch.isdigit())) >= 10
    has_gst = bool(item.get("customer_gst_number") or item.get("gst_number"))
    has_email = bool(item.get("email"))
    return (has_address, has_real_mobile, has_gst, has_email)


def _dedupe_by_tally_name(items: List[Dict[str, Any]], store: MappingStore) -> List[Dict[str, Any]]:
    """
    Tally ledgers are matched and altered purely by NAME (TallyClient.check_exists /
    sync_customer) — RentAsst has no equivalent uniqueness constraint and can hold
    multiple customer records under the identical name (confirmed live: reverse sync's
    own "does this name already exist in RentAsst" check can miss an existing customer
    — e.g. against a same-named Tally ledger it hadn't matched yet — and create a
    second RentAsst customer under a name that's already in use).

    Forward-syncing more than one such record pushes them all to the SAME physical
    Tally ledger — whichever one this pipeline happens to process last silently
    overwrites whatever the other one just wrote, with no error, no dead letter, and no
    indication a real customer's mobile/address was ever touched. Confirmed live: a
    customer's real mobile and address were repeatedly clobbered back to a synthetic
    placeholder mobile and a blank address by a same-named duplicate customer created by
    reverse sync, on every single sync cycle — the ledger XML build itself was never the
    problem, only one of the two colliding records ever "won" the shared ledger.

    Keeps only the most complete record per normalized name (has an address, a real
    10+ digit mobile, GST, email — in that priority order) and dead-letters the rest
    with a message identifying the exact duplicate, so the real problem (two RentAsst
    customers sharing one name) gets fixed at the source — rename or merge one of
    them — instead of the sync silently losing data forever.
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        key = _normalize_ledger_name(item.get("name") or item.get("business_name"))
        if not key:
            continue
        by_name.setdefault(key, []).append(item)

    result: List[Dict[str, Any]] = []
    for group in by_name.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        ranked = sorted(group, key=_customer_completeness_score, reverse=True)
        winner, losers = ranked[0], ranked[1:]
        result.append(winner)

        for loser in losers:
            loser_id = str(loser.get("id"))
            store.add_dead_letter(
                entity_type="customer",
                source_id=loser_id,
                error=(
                    f"Skipped: RentAsst customer #{loser_id} ('{loser.get('name')}') shares its name "
                    f"with customer #{winner.get('id')} — Tally ledgers are matched by name alone, so "
                    "syncing both would push them to the same ledger and whichever ran last would "
                    "silently overwrite the other's mobile/address. Rename or merge one of these two "
                    "RentAsst customers, then this will sync normally."
                ),
                payload=json.dumps(loser),
                error_type="DuplicateNameConflict",
            )
    return result


def sync_customers(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    def _fetch():
        items = filter_by_date_range(rentasst_client.fetch_customers(), from_date, to_date)
        return _dedupe_by_tally_name(items, store)

    return run_sync_pipeline(
        entity_type="customer",
        fetch_func=_fetch,
        sync_func=external_client.sync_customer,
        store=store,
        external_client=external_client,
    )
