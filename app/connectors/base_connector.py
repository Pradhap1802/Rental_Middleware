from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class ConnectorResponse:
    def __init__(
        self,
        success: bool,
        external_id: Optional[str] = None,
        error: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.success = success
        self.external_id = external_id
        self.error = error
        self.data = data or {}


class BaseConnector(ABC):
    @abstractmethod
    def connect(self) -> bool:
        """Establishes connection to target system."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Closes target system connection/session."""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """Verifies active connectivity to target system."""
        pass

    @abstractmethod
    def sync(self, entity_type: str, payload: Dict[str, Any]) -> ConnectorResponse:
        """Pushes entity payload to target system and returns standardized response."""
        pass

    @abstractmethod
    def update(self, entity_type: str, external_id: str, payload: Dict[str, Any]) -> ConnectorResponse:
        """Updates existing entity on target system."""
        pass

    @abstractmethod
    def delete(self, entity_type: str, external_id: str) -> ConnectorResponse:
        """Removes or voids entity on target system."""
        pass
