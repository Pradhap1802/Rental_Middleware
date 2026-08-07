import json
import os
from typing import Optional
from cryptography.fernet import Fernet
from ..models.domain import AppConfig


class ConfigStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.key_path = os.path.join(self.data_dir, "secret.key")
        self.cfg_path = os.path.join(self.data_dir, "config.json.enc")
        os.makedirs(self.data_dir, exist_ok=True)

    def _get_fernet(self) -> Fernet:
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
        if not os.path.exists(self.cfg_path):
            return None
        try:
            fernet = self._get_fernet()
            with open(self.cfg_path, "rb") as f:
                enc = f.read()
            raw = fernet.decrypt(enc)
            data = json.loads(raw.decode("utf-8"))
            return AppConfig(**data)
        except Exception:
            return None

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
