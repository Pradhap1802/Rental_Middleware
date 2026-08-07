import uuid
import platform
import logging

logger = logging.getLogger(__name__)


def get_device_fingerprint() -> str:
    """Generates a hardware/host unique identifier for license binding."""
    host_info = f"{platform.node()}-{platform.machine()}-{platform.processor()}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, host_info))


def validate_license(license_key: str) -> bool:
    """Placeholder license validator hook for RentAsst Enterprise licenses."""
    if not license_key:
        return True # Default open mode for local deployment
    # Check format or key validation endpoint
    return len(license_key) >= 8
