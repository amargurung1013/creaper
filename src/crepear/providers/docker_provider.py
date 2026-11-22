import logging
import re
from datetime import datetime, timedelta, timezone

import docker
from docker.errors import DockerException

from crepear.models import CleanupResult, ZombieResource
from crepear.providers.base import BaseProvider

DEFAULT_EXCLUDE_LABEL = "crepear.ignore=true"
LOGGER = logging.getLogger(__name__)


class DockerProvider(BaseProvider):
    def __init__(self, client: docker.DockerClient | None = None):
        # Allow passing a fake client during tests; default to real local Docker.
        self.client = client or docker.from_env()

    def _parse_timestamp(self, ts: str | None) -> datetime:
        """Parse Docker timestamps and reject malformed values explicitly.

        Docker normally returns UTC timestamps. A timezone-naive value is
        treated as UTC because Docker's API defines these timestamps in UTC;
        invalid or missing values raise ``ValueError`` for the caller to handle.
        """
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError("Docker timestamp is missing or empty")

        clean_ts = ts.strip()
        if clean_ts.endswith("Z"):
            clean_ts = f"{clean_ts[:-1]}+00:00"
        clean_ts = re.sub(r"(\.\d{6})\d+", r"\1", clean_ts)
        try:
            parsed = datetime.fromisoformat(clean_ts)
        except ValueError as exc:
            raise ValueError(f"Invalid Docker timestamp: {ts!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
        state = container.attrs.get("State") or {}
        finished_at = state.get("FinishedAt")
        if (
            finished_at
            and isinstance(finished_at, str)
            and not finished_at.startswith("0001-01-01")
        ):
            return self._parse_timestamp(finished_at)
        if finished_at and not isinstance(finished_at, str):
            raise ValueError("Docker FinishedAt timestamp is not a string")
        return self._parse_timestamp(container.attrs.get("Created"))

    def _audit_timestamp(self, resource_id: str, timestamp_factory) -> datetime | None:
        try:
            return timestamp_factory()
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning("Skipping Docker resource %s: %s", resource_id, exc)
            return None

    def _layer_metadata(self, image) -> tuple[list[str], list[int] | None]:
        rootfs = image.attrs.get("RootFS") or {}
        layers = rootfs.get("Layers")
        if not isinstance(layers, list) or not all(isinstance(layer, str) for layer in layers):
            return [], None
        raw_sizes = rootfs.get("LayerSizes", image.attrs.get("LayerSizes"))
        if not isinstance(raw_sizes, list) or len(raw_sizes) != len(layers):
            return layers, None
        if not all(isinstance(size, int) and size >= 0 for size in raw_sizes):
            return layers, None
        return layers, raw_sizes

    def _image_size_estimate(self, image, seen_layers: set[str]) -> tuple[int, str]:
        """Return a conservative image estimate and explain its limits.

        Docker exposes virtual image size, not a generally reliable per-layer
        reclaimable size. If layer identities are available, an image sharing
        any layer with an earlier image is reported as zero incremental bytes
        rather than double-counted.
        """
        raw_size = image.attrs.get("Size")
        layers, layer_sizes = self._layer_metadata(image)
        if layer_sizes is not None:
            unique_bytes = sum(
                size
                for layer, size in zip(layers, layer_sizes, strict=True)
                if layer not in seen_layers
            )
            seen_layers.update(layers)
            return unique_bytes, (
                "approximation: unique layer sizes were used; Docker storage-driver overhead "
                "and compression may differ"
            )

        layer_ids = set(layers)
        has_shared_layers = bool(layer_ids & seen_layers)
        seen_layers.update(layer_ids)
        if not isinstance(raw_size, int) or raw_size < 0:
            return 0, "approximation: Docker image size metadata was missing or invalid"
        if has_shared_layers:
            return 0, (
                "approximation unavailable: shared and unique layer byte sizes were not "
                "provided by Docker"
            )
        return (
            raw_size,
            "approximation: Docker image Size is virtual, not guaranteed reclaimable storage",
        )

    def _is_dangling_image(self, image) -> bool:
        tags = image.attrs.get("RepoTags")
        if tags is None:
            tags = getattr(image, "tags", [])
        if not isinstance(tags, list):
            return False
        return not tags or all(tag == "<none>:<none>" for tag in tags)

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
            inactive_since = self._audit_timestamp(
                container.short_id,
                lambda container=container: self._get_container_inactive_time(container),
            )
            if inactive_since is None:
                continue

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
            created_at = self._audit_timestamp(
                image.short_id,
                lambda image=image: self._parse_timestamp(image.attrs.get("Created")),
            )
            if created_at is None:
                continue

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

    def clean(
        self,
        resources: list[ZombieResource],
        older_than: timedelta | None = None,
        exclude_labels: list[str] | None = None,
    ) -> CleanupResult:
        result = CleanupResult()
        rules = [DEFAULT_EXCLUDE_LABEL] + (exclude_labels or [])
        for res in resources:
            if res.provider != "docker":
                result.failed[res.id] = "resource does not belong to the Docker provider"
                continue
            if res.resource_type not in {"container", "image"}:
                result.failed[res.id] = f"unsupported Docker resource type: {res.resource_type}"
                continue
            try:
                if res.resource_type == "container":
                    container = self.client.containers.get(res.id)
                    state = container.attrs.get("State") or {}
                    status = state.get("Status", getattr(container, "status", None))
                    if status not in {"created", "dead", "exited"} or state.get("Running") is True:
                        result.failed[res.id] = "container is not stopped"
                        continue
                    labels = container.labels or container.attrs.get("Config", {}).get("Labels", {})
                    if self._is_excluded(labels, rules):
                        result.failed[res.id] = "container has an exclusion label"
                        continue
                    inactive_since = self._get_container_inactive_time(container)
                    if not self._is_old_enough(inactive_since, older_than):
                        result.failed[res.id] = "container no longer satisfies the age threshold"
                        continue
                    container.remove()
                    result.deleted.append(res.id)
                elif res.resource_type == "image":
                    image = self.client.images.get(res.id)
                    if not self._is_dangling_image(image):
                        result.failed[res.id] = "image is no longer dangling"
                        continue
                    self.client.images.remove(res.id)
                    result.deleted.append(res.id)
            except (DockerException, KeyError, TypeError, ValueError) as e:
                result.failed[res.id] = str(e)
        return result
