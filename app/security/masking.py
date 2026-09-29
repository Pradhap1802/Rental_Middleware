import re
from typing import Any, Optional

SENSITIVE_KEYS = {
    "password", "token", "api_key", "secret", "authorization", "auth",
    "bearer", "db_password", "tally_password", "external_api_key",
    "rentasst_api_token", "private_key", "access_token"
}


def mask_secret(val: Optional[str], show_chars: int = 4) -> str:
    """
    Masks a sensitive string (e.g. 'sk_live_1234567890abcdef' -> 'sk_l****cdef' or '****').
    Returns masked representation.
    """
    if not val or not isinstance(val, str):
        return ""
    txt = val.strip()
    if not txt:
        return ""
    if len(txt) <= (show_chars * 2):
        return "****"
    return f"{txt[:show_chars]}****{txt[-show_chars:]}"


def mask_payload_secrets(data: Any) -> Any:
    """
    Recursively traverses a dictionary or list, masking values associated with sensitive keys.
    """
    if isinstance(data, dict):
        masked_dict = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in SENSITIVE_KEYS):
                masked_dict[k] = mask_secret(str(v)) if v else v
            else:
                masked_dict[k] = mask_payload_secrets(v)
        return masked_dict
    elif isinstance(data, list):
        return [mask_payload_secrets(item) for item in data]
    return data


def mask_log_message(msg: str) -> str:
    """
    Applies regex patterns to mask Bearer tokens, passwords, and API keys in raw log strings.
    """
    if not msg or not isinstance(msg, str):
        return msg

    # Mask Bearer tokens: "Bearer <token>" -> "Bearer ****"
    msg = re.sub(r"Bearer\s+([A-Za-z0-9._~+/-]+)", r"Bearer ****", msg, flags=re.IGNORECASE)

    # Mask key-value patterns: password="xyz" or password is xyz or api_key='abc'
    msg = re.sub(
        r"(password|token|api_key|secret|authorization)\s*(?:[:=]|\bis\b)\s*['\"]?([^'\"\s&,]+)['\"]?",
        r"\1=****",
        msg,
        flags=re.IGNORECASE,
    )

    return msg
