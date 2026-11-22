from datetime import datetime, timedelta, timezone

import boto3

from crepear.models import ZombieResource
from crepear.providers.base import BaseProvider

DEFAULT_EXCLUDE_LABEL = "crepear.ignore=true"


class AWSProvider(BaseProvider):
    def __init__(self, region: str = "us-east-1", client=None):
        # Allow passing a mocked boto3 client in tests; default to real EC2 client.
        self.client = client or boto3.client("ec2", region_name=region)

    def _is_excluded(self, tags: dict[str, str], exclude_rules: list[str]) -> bool:
        for rule in exclude_rules:
            if "=" in rule:
                key, val = rule.split("=", 1)
                if tags.get(key) == val:
                    return True
            elif rule in tags:
                return True
        return False

    def _is_old_enough(self, created_at: datetime, older_than: timedelta | None) -> bool:
        if older_than is None:
            return True
        cutoff = datetime.now(timezone.utc) - older_than
        return created_at <= cutoff

    def audit(
        self,
        older_than: timedelta | None = None,
        exclude_labels: list[str] | None = None,
    ) -> list[ZombieResource]:
        rules = [DEFAULT_EXCLUDE_LABEL] + (exclude_labels or [])
        zombies: list[ZombieResource] = []

        # Only fetch "available" (unattached) EBS volumes—never touch "in-use" volumes.
        response = self.client.describe_volumes(
            Filters=[{"Name": "status", "Values": ["available"]}]
        )

        for vol in response.get("Volumes", []):
            # AWS returns tags as [{"Key": "k", "Value": "v"}]; convert to a plain dict.
            tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
            created_at = vol["CreateTime"]

            if self._is_excluded(tags, rules) or not self._is_old_enough(created_at, older_than):
                continue

            # AWS returns EBS size in GiB; convert to bytes to match ZombieResource.
            size_bytes = vol["Size"] * 1024 * 1024 * 1024
            vol_id = vol["VolumeId"]

            zombies.append(
                ZombieResource(
                    id=vol_id,
                    name=tags.get("Name", vol_id),
                    resource_type="ebs_volume",
                    size_bytes=size_bytes,
                    created_at=created_at,
                    provider="aws",
                    labels=tags,
                )
            )

        return zombies

    def clean(self, resources: list[ZombieResource]) -> list[str]:
        deleted_ids: list[str] = []
        for res in resources:
            if res.resource_type == "ebs_volume":
                self.client.delete_volume(VolumeId=res.id)
                deleted_ids.append(res.id)
        return deleted_ids
