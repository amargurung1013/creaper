from abc import ABC, abstractmethod
from datetime import timedelta

from crepear.models import CleanupResult, ZombieResource


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
    def clean(self, resources: list[ZombieResource]) -> CleanupResult:
        """Delete the given resources and report succeeded and failed deletions."""
