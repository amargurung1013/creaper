from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from crepear.models import CleanupResult, ZombieResource
from crepear.providers.base import BaseProvider

DEFAULT_EXCLUDE_LABEL = "crepear.ignore=true"
DEFAULT_REQUIRED_TAG = "crepear.eligible=true"


class AWSProvider(BaseProvider):
    def __init__(
        self,
        region: str = "us-east-1",
        client=None,
        required_tag: str | None = DEFAULT_REQUIRED_TAG,
    ):
        self.client = client or boto3.client("ec2", region_name=region)
        self.required_tag = required_tag

    def _matches_rule(self, tags: dict[str, str], rule: str) -> bool:
        if "=" in rule:
            key, val = rule.split("=", 1)
            return tags.get(key) == val
        return rule in tags

    def _is_excluded(self, tags: dict[str, str], exclude_rules: list[str]) -> bool:
        return any(self._matches_rule(tags, rule) for rule in exclude_rules)

    def _has_required_tag(self, tags: dict[str, str]) -> bool:
        if not self.required_tag:
            return True
        return self._matches_rule(tags, self.required_tag)

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

        paginator = self.client.get_paginator("describe_volumes")
        pages = paginator.paginate(Filters=[{"Name": "status", "Values": ["available"]}])

        for page in pages:
            for vol in page.get("Volumes", []):
                tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
                created_at = vol["CreateTime"]

                if (
                    not self._has_required_tag(tags)
                    or self._is_excluded(tags, rules)
                    or not self._is_old_enough(created_at, older_than)
                ):
                    continue

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

    def clean(
        self,
        resources: list[ZombieResource],
        older_than: timedelta | None = None,
        exclude_labels: list[str] | None = None,
    ) -> CleanupResult:
        """Safely delete resources after revalidating each one with AWS.

        Audit results are observations, not authorization. Every volume is fetched
        again because its state, tags, or existence may have changed since audit.
        """
        result = CleanupResult()
        exclusion_rules = [DEFAULT_EXCLUDE_LABEL] + (exclude_labels or [])
        for res in resources:
            if res.provider != "aws":
                result.failed[res.id] = "resource does not belong to the AWS provider"
                continue
            if res.resource_type != "ebs_volume":
                result.failed[res.id] = f"unsupported AWS resource type: {res.resource_type}"
                continue

            try:
                response = self.client.describe_volumes(VolumeIds=[res.id])
                volumes = response.get("Volumes", [])
                if not volumes:
                    result.failed[res.id] = "volume no longer exists"
                    continue

                volume = volumes[0]
                tags = {
                    tag["Key"]: tag["Value"]
                    for tag in volume.get("Tags", [])
                    if "Key" in tag and "Value" in tag
                }
                if volume.get("State") != "available":
                    result.failed[res.id] = (
                        f"volume is not available (current state: {volume.get('State', 'unknown')})"
                    )
                    continue
                required_tag = self.required_tag or DEFAULT_REQUIRED_TAG
                if not self._matches_rule(tags, required_tag):
                    result.failed[res.id] = "volume is not eligible for deletion"
                    continue
                if self._is_excluded(tags, exclusion_rules):
                    result.failed[res.id] = "volume has an exclusion tag"
                    continue

                self.client.delete_volume(VolumeId=res.id)
                result.deleted.append(res.id)
            except (ClientError, BotoCoreError) as e:
                result.failed[res.id] = str(e)
        return result
