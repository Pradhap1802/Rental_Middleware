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
            if not cfg.rentasst_api_key or "*" in cfg.rentasst_api_key:
                cfg.rentasst_api_key = existing.rentasst_api_key
            if not cfg.rentasst_url:
                cfg.rentasst_url = existing.rentasst_url
            if not cfg.rentasst_tenant_id and existing.rentasst_tenant_id:
                cfg.rentasst_tenant_id = existing.rentasst_tenant_id
            if not cfg.external_api_key or "*" in cfg.external_api_key:
                cfg.external_api_key = existing.external_api_key
            if not cfg.external_url and existing.external_url:
                cfg.external_url = existing.external_url
            if not cfg.tally_company_name and existing.tally_company_name:
                cfg.tally_company_name = existing.tally_company_name
        self.config_store.save(cfg)
        return cfg
