import os
import secrets


def get_or_create_api_key(data_dir: str) -> str:
    """
    Returns the middleware's own local API key, generating and persisting one on
    first run. This exists so every /api/* route requires a shared secret — even
    bound to 127.0.0.1, any other process or user on the same machine could otherwise
    read/write RentAsst and Tally credentials, trigger syncs, or restore a backup.
    """
    key_path = os.path.join(data_dir, "api.key")
    if os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing

    key = secrets.token_urlsafe(32)
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return key
