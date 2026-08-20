from abc import ABC, abstractmethod


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
