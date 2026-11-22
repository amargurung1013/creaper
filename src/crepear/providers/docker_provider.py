from datetime import datetime, timedelta, timezone

import docker
from docker.errors import DockerException

from crepear.models import CleanupResult, ZombieResource
from crepear.providers.base import BaseProvider

DEFAULT_EXCLUDE_LABEL = "crepear.ignore=true"


class DockerProvider(BaseProvider):
    def __init__(self, client: docker.DockerClient | None = None):
        # Allow passing a fake client during tests; default to real local Docker.
        self.client = client or docker.from_env()

    def _parse_timestamp(self, ts: str) -> datetime:
        """Handle Docker's ISO-8601 timestamps (which often include 'Z' and nanoseconds)."""
        clean_ts = ts.replace("Z", "+00:00")
        if "." in clean_ts:
            main, rest = clean_ts.split(".", 1)
            # Keep only 6 microsecond digits so Python's fromisoformat doesn't crash
            frac = rest[:6]
            tz = rest[rest.find("+") :] if "+" in rest else "+00:00"
            clean_ts = f"{main}.{frac}{tz}"
        return datetime.fromisoformat(clean_ts)

    def _is_excluded(self, resource_labels: dict[str, str], exclude_rules: list[str]) -> bool:
        """Check if a resource has any label matching our exclusion rules."""
        for rule in exclude_rules:
            if "=" in rule:
                key, val = rule.split("=", 1)
                if resource_labels.get(key) == val:
                    return True
            elif rule in resource_labels:
                return True
        return False

    def _is_old_enough(self, created_at: datetime, older_than: timedelta | None) -> bool:
        if older_than is None:
            return True
        cutoff = datetime.now(timezone.utc) - older_than
        return created_at <= cutoff

    def _get_container_inactive_time(self, container) -> datetime:
        """Return when the container actually exited, falling back to Created."""
        finished_at = container.attrs.get("State", {}).get("FinishedAt")
        if finished_at and not finished_at.startswith("0001-01-01"):
            return self._parse_timestamp(finished_at)
        return self._parse_timestamp(container.attrs["Created"])

    def _image_size_estimate(self, image, seen_layers: set[str]) -> tuple[int, str]:
        """Return a conservative image estimate and explain its limits.

        Docker exposes virtual image size, not a generally reliable per-layer
        reclaimable size. If layer identities are available, an image sharing
        any layer with an earlier image is reported as zero incremental bytes
        rather than double-counted.
        """
        raw_size = image.attrs.get("Size")
        size = raw_size if isinstance(raw_size, int) and raw_size >= 0 else 0
        rootfs = image.attrs.get("RootFS") or {}
        layers = rootfs.get("Layers", [])
        layer_ids = {layer for layer in layers if isinstance(layer, str)}
        if layer_ids and layer_ids & seen_layers:
            seen_layers.update(layer_ids)
            return 0, "approximation: shared image layers were not counted twice"
        seen_layers.update(layer_ids)
        if not isinstance(raw_size, int) or raw_size < 0:
            return 0, "approximation: Docker image size metadata was missing or invalid"
        return size, "approximation: Docker image Size is virtual and may include shared layers"

    def audit(
        self,
        older_than: timedelta | None = None,
        exclude_labels: list[str] | None = None,
    ) -> list[ZombieResource]:
        rules = [DEFAULT_EXCLUDE_LABEL] + (exclude_labels or [])
        zombies: list[ZombieResource] = []

        # 1. Stopped / Exited Containers
        for container in self.client.containers.list(all=True, filters={"status": "exited"}):
            labels = container.labels or {}
            inactive_since = self._get_container_inactive_time(container)

            if self._is_excluded(labels, rules) or not self._is_old_enough(
                inactive_since, older_than
            ):
                continue

            zombies.append(
                ZombieResource(
                    id=container.short_id,
                    name=container.name,
                    resource_type="container",
                    size_bytes=(
                        container.attrs.get("SizeRw", 0)
                        if isinstance(container.attrs.get("SizeRw", 0), int)
                        and container.attrs.get("SizeRw", 0) >= 0
                        else 0
                    ),
                    created_at=inactive_since,
                    provider="docker",
                    labels=labels,
                    size_is_estimate=True,
                    storage_note=(
                        "Docker SizeRw is only the container writable layer, not total storage"
                        if isinstance(container.attrs.get("SizeRw", 0), int)
                        and container.attrs.get("SizeRw", 0) >= 0
                        else "approximation: Docker SizeRw metadata was missing or invalid"
                    ),
                )
            )

        # 2. Dangling Images (<none>:<none>)
        seen_layers: set[str] = set()
        for image in self.client.images.list(filters={"dangling": True}):
            labels = image.labels or {}
            created_at = self._parse_timestamp(image.attrs["Created"])

            if self._is_excluded(labels, rules) or not self._is_old_enough(created_at, older_than):
                continue

            size_bytes, storage_note = self._image_size_estimate(image, seen_layers)
            zombies.append(
                ZombieResource(
                    id=image.short_id.replace("sha256:", ""),
                    name="<dangling>",
                    resource_type="image",
                    size_bytes=size_bytes,
                    created_at=created_at,
                    provider="docker",
                    labels=labels,
                    size_is_estimate=True,
                    storage_note=storage_note,
                )
            )

        return zombies

    def clean(self, resources: list[ZombieResource]) -> CleanupResult:
        result = CleanupResult()
        for res in resources:
            try:
                if res.resource_type == "container":
                    self.client.containers.get(res.id).remove()
                    result.deleted.append(res.id)
                elif res.resource_type == "image":
                    self.client.images.remove(res.id)
                    result.deleted.append(res.id)
            except DockerException as e:
                result.failed[res.id] = str(e)
        return result
