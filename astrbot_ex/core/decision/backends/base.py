"""B04/B07 synchronous backend boundary; the service owns async scheduling."""
from abc import ABC, abstractmethod

from ..models import BackendDecision, DecisionSnapshot


class DecisionBackend(ABC):
    @property
    def execution_allowed(self) -> bool:
        """Trusted construction capability, not a model/wire authorization."""
        return True

    @abstractmethod
    def decide(self, snapshot: DecisionSnapshot) -> BackendDecision:
        """Choose only snapshot options. Never execute or authorize actions."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Cooperatively release an outstanding decide and owned resources."""
        raise NotImplementedError
