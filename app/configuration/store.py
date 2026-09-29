import json
import os
import threading
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet
from ..models.domain import AppConfig
from ..security.masking import mask_secret

# Guards ConfigStore.update_fields()'s read-modify-write against concurrent callers —
# QueueWorker runs up to 4 entity-type sync passes at once (ThreadPoolExecutor in
# app/queue/worker.py), each constructing its own ConfigStore instance, so a plain
# per-instance lock wouldn't cover it. See update_fields()'s own docstring.
_SAVE_LOCK = threading.Lock()


class ConfigStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.key_path = os.path.join(self.data_dir, "secret.key")
        self.cfg_path = os.path.join(self.data_dir, "config.json.enc")
        os.makedirs(self.data_dir, exist_ok=True)

    def _get_fernet(self) -> Fernet:
        # Priority 1: Environment variable secret key
        env_key = os.environ.get("RENTAL_MIDDLEWARE_SECRET_KEY")
        if env_key:
            try:
                return Fernet(env_key.encode("utf-8"))
            except Exception as e:
                # Deliberately fail loudly rather than silently deriving a key from
                # whatever string was provided (e.g. a weak passphrase) — that would
                # accept low-entropy input as if it were a real key with no warning.
                raise ValueError(
                    "RENTAL_MIDDLEWARE_SECRET_KEY is set but is not a valid Fernet key "
                    "(must be a URL-safe base64-encoded 32-byte key, e.g. from "
                    "`Fernet.generate_key()`). Unset it to use the file-based key instead, "
                    "or provide a properly generated key."
                ) from e

        # Priority 2: Protected persistent key file
        if not os.path.exists(self.key_path):
            key = Fernet.generate_key()
            with open(self.key_path, "wb") as f:
                f.write(key)
            try:
                os.chmod(self.key_path, 0o600)
            except Exception:
                pass
        with open(self.key_path, "rb") as f:
            key = f.read()
        return Fernet(key)

    def load_safe(self) -> Optional[AppConfig]:
        from ..services.discovery_service import DiscoveryService
        cfg = None
        if not os.path.exists(self.cfg_path):
            auto_cfg = DiscoveryService.auto_discover_rentasst()
            self.save(auto_cfg)
            cfg = auto_cfg
        else:
            # A misconfigured secret key (ValueError from _get_fernet) is a real
            # operator error and must not be masked behind a silent fallback to
            # auto-discovered defaults — that would silently discard the user's real
            # saved config. Only genuine data problems (corrupt file, stale key) fall
            # back to auto-discovery.
            fernet = self._get_fernet()
            try:
                with open(self.cfg_path, "rb") as f:
                    enc = f.read()
                raw = fernet.decrypt(enc)
                data = json.loads(raw.decode("utf-8"))
                cfg = AppConfig(**data)
            except Exception:
                cfg = DiscoveryService.auto_discover_rentasst()

        if cfg:
            # Environment variable overrides
            env_ra_token = os.environ.get("RENTASST_API_TOKEN") or os.environ.get("RENTASST_TOKEN") or os.environ.get("RENTASST_API_KEY")
            env_ra_url = os.environ.get("RENTASST_URL") or os.environ.get("RENTASST_API_URL")
            env_ext_key = os.environ.get("EXTERNAL_API_KEY") or os.environ.get("TALLY_API_KEY")
            env_ext_url = os.environ.get("EXTERNAL_URL") or os.environ.get("TALLY_HOST")

            if env_ra_token:
                cfg.rentasst_api_key = env_ra_token
            if env_ra_url:
                cfg.rentasst_url = env_ra_url.rstrip("/")
            if env_ext_key:
                cfg.external_api_key = env_ext_key
            if env_ext_url:
                cfg.external_url = env_ext_url.rstrip("/")

        return cfg

    def require(self) -> AppConfig:
        cfg = self.load_safe()
        if not cfg:
            raise ValueError("Configuration not initialized yet")
        if not cfg.rentasst_url:
            raise ValueError("RentAsst API URL is required")
        return cfg

    def save(self, cfg: AppConfig) -> None:
        fernet = self._get_fernet()
        data = cfg.model_dump()
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        enc = fernet.encrypt(raw)
        with open(self.cfg_path, "wb") as f:
            f.write(enc)

    def update_fields(self, **fields: Any) -> AppConfig:
        """
        Atomically applies a partial update on top of the LATEST persisted config,
        not whatever in-memory AppConfig object a long-running caller loaded earlier.

        SyncService.execute_sync() loads one AppConfig at the start of a sync pass
        that can run for minutes over a large batch, then (in a few auto-detection
        cases — tally_edu_mode, tally_order_processing_available) calls cfg_store.
        save(cfg) at the end to persist a learned setting. QueueWorker runs up to 4
        entity-type sync passes concurrently (ThreadPoolExecutor in
        app/queue/worker.py), each with its own independently-loaded snapshot — a
        plain save(cfg) there blindly writes the ENTIRE stale snapshot, silently
        reverting any field a DIFFERENT concurrent pass had already learned and
        persisted moments earlier. Confirmed live: tally_order_processing_available
        flip-flopped back to unset across consecutive sync cycles this way, so a
        Tally company already confirmed (this same cycle) to reject the native
        "Sales Order" voucher type kept re-attempting it and failing identically on
        the very next retry instead of the learned setting sticking. Re-reading
        fresh under a lock right before the merge+write closes that window — callers
        needing this should mutate exactly the field(s) they own through here rather
        than through save(cfg) with a long-held snapshot.
        """
        with _SAVE_LOCK:
            fresh = self.load_safe() or AppConfig()
            for key, value in fields.items():
                setattr(fresh, key, value)
            self.save(fresh)
            return fresh

    def get_masked_config(self, cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
        target_cfg = cfg or self.load_safe()
        if not target_cfg:
            return {}
        data = target_cfg.model_dump()
        if data.get("rentasst_api_key"):
            data["rentasst_api_key"] = mask_secret(data["rentasst_api_key"])
        if data.get("external_api_key"):
            data["external_api_key"] = mask_secret(data["external_api_key"])
        return data
