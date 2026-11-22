from abc import ABC, abstractmethod
from datetime import timedelta

from crepear.models import ZombieResource


class BaseProvider(ABC):
    """Contract that every infrastructure provider (Docker, AWS, GCP) must follow."""

    @abstractmethod
    def audit(
        self,
        older_than: timedelta | None = None,
        exclude_labels: list[str] | None = None,
    ) -> list[ZombieResource]:
        """Find zombie resources without deleting anything."""

    @abstractmethod
    def clean(self, resources: list[ZombieResource]) -> list[str]:
        """Delete the given resources and return a list of deleted IDs."""
