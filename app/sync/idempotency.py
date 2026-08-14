import logging
from typing import Optional, Any
from ..logging.logger import log_event

logger = logging.getLogger(__name__)


def generate_integration_key(
    source_company: str,
    entity_type: str,
    source_id: str,
    sync_direction: str = "forward",
) -> str:
    """
    Generates a deterministic, unique integration key formatted as:
    {source_company}:{entity_type}:{source_id}:{sync_direction}
    """
    company = (source_company or "default").strip().lower()
    ent = (entity_type or "").strip().lower()
    sid = str(source_id or "").strip()
    direction = (sync_direction or "forward").strip().lower()
    return f"{company}:{ent}:{sid}:{direction}"


def check_target_system_record_exists(
    entity_type: str,
    identifier: str,
    sync_direction: str = "forward",
    external_client: Optional[Any] = None,
    ra_client: Optional[Any] = None,
) -> bool:
    """
    Queries the target system (Tally Prime for forward sync, RentAsst Cloud for reverse sync)
    to verify if a record was already created during a previous execution or timed-out HTTP request.
    """
    if not identifier:
        return False

    ent = (entity_type or "").lower().strip()
    direction = (sync_direction or "forward").lower().strip()

    if direction == "forward":
        # Target system is Tally Prime / External ERP
        if external_client and hasattr(external_client, "check_exists_in_tally"):
            try:
                if external_client.ping():
                    exists = external_client.check_exists_in_tally(ent, identifier)
                    if exists:
                        log_event(
                            "Idempotency",
                            f"Target system check (Tally): Record '{identifier}' for entity '{ent}' already exists in target system.",
                        )
                        return True
            except Exception as e:
                log_event("Idempotency", f"Error checking target system (Tally): {e}", level=logging.WARNING)
    elif direction == "reverse":
        # Target system is RentAsst Cloud API
        if ra_client and hasattr(ra_client, "check_exists_in_rentasst"):
            try:
                if ra_client.ping():
                    exists = ra_client.check_exists_in_rentasst(ent, identifier)
                    if exists:
                        log_event(
                            "Idempotency",
                            f"Target system check (RentAsst): Record '{identifier}' for entity '{ent}' already exists in target system.",
                        )
                        return True
            except Exception as e:
                log_event("Idempotency", f"Error checking target system (RentAsst): {e}", level=logging.WARNING)

    return False
