from .base_connector import BaseConnector
from .tally_connector import TallyConnector
from .rest_connector import RestConnector
from ..models.domain import AppConfig


class ConnectorFactory:
    @staticmethod
    def create_connector(cfg: AppConfig) -> BaseConnector:
        system_type = (cfg.external_system_type or "tally").lower()
        if system_type == "tally":
            return TallyConnector(cfg)
        return RestConnector(cfg)
