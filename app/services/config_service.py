from ..configuration.store import ConfigStore
from ..models.domain import AppConfig


class ConfigService:
    def __init__(self, data_dir: str):
        self.config_store = ConfigStore(data_dir)

    def get_config(self) -> AppConfig:
        cfg = self.config_store.load_safe()
        if not cfg:
            return AppConfig()
        return cfg

    def save_config(self, cfg: AppConfig) -> AppConfig:
        existing = self.config_store.load_safe()
        if existing:
            if cfg.rentasst_api_key and "*" in cfg.rentasst_api_key:
                cfg.rentasst_api_key = existing.rentasst_api_key
            if cfg.external_api_key and "*" in cfg.external_api_key:
                cfg.external_api_key = existing.external_api_key
        self.config_store.save(cfg)
        return cfg
