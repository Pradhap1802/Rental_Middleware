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
        self.config_store.save(cfg)
        return cfg
