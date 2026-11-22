import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class ZombieResource:
    """Standardized representation of any unused resource across providers."""

    id: str
    name: str
    resource_type: str  # e.g., "container", "image", "ebs_volume"
    size_bytes: int
    created_at: datetime
    provider: str  # e.g., "docker", "aws"
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


def parse_duration(duration_str: str) -> timedelta:
    """Parse strings like '7d', '24h', or '30m' into a timedelta."""
    match = re.fullmatch(r"(\d+)([dhm])", duration_str.strip().lower())
    if not match:
        raise ValueError(
            f"Invalid duration '{duration_str}'. Use formats like '7d', '24h', or '30m'."
        )
    value, unit = int(match.group(1)), match.group(2)
    if unit == "d":
        return timedelta(days=value)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(minutes=value)
