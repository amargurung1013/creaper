from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import boto3
from moto import mock_aws

from crepear.providers.aws_provider import AWSProvider


@mock_aws
def test_aws_audit_requires_opt_in_tag_and_respects_exclusions():
    ec2 = boto3.client("ec2", region_name="us-east-1")

    # 1. Opt-in zombie volume (SHOULD be caught)
    zombie_vol = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=50,
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": "orphan-vol"},
                    {"Key": "crepear.eligible", "Value": "true"},
                ],
            }
        ],
    )
    # 2. Untagged backup volume (SHOULD be skipped — no opt-in tag)
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=200,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": "manual-backup"}]}
        ],
    )
    # 3. Both opt-in AND ignore tags (SHOULD be skipped — ignore wins)
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=100,
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "crepear.eligible", "Value": "true"},
                    {"Key": "crepear.ignore", "Value": "true"},
                ],
            }
        ],
    )
    # 4. Opt-in + custom --exclude-label env=prod (SHOULD be skipped)
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=80,
        VolumeType="gp3",
        TagSpecifications=[
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "crepear.eligible", "Value": "true"},
                    {"Key": "env", "Value": "prod"},
                ],
            }
        ],
    )

    provider = AWSProvider(region="us-east-1", client=ec2)
    zombies = provider.audit(exclude_labels=["env=prod"])

    assert len(zombies) == 1
    assert zombies[0].id == zombie_vol["VolumeId"]
    assert zombies[0].name == "orphan-vol"
    assert zombies[0].size_bytes == 50 * 1024 * 1024 * 1024


@mock_aws
def test_aws_audit_respects_older_than():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=20,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "crepear.eligible", "Value": "true"}]}
        ],
    )

    provider = AWSProvider(region="us-east-1", client=ec2)
    zombies = provider.audit(older_than=timedelta(days=7))

    assert zombies == []


@mock_aws
def test_aws_clean_deletes_volume():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vol = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=10,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "crepear.eligible", "Value": "true"}]}
        ],
    )
    vol_id = vol["VolumeId"]

    provider = AWSProvider(region="us-east-1", client=ec2)
    zombies = provider.audit()
    deleted_ids = provider.clean(zombies)

    assert deleted_ids.deleted == [vol_id]
    assert deleted_ids.failed == {}
    assert ec2.describe_volumes()["Volumes"] == []


def test_aws_audit_paginates_multiple_pages():
    now = datetime.now(timezone.utc)
    mock_client = MagicMock()
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator

    # Simulate AWS returning 2 separate pages of volumes
    mock_paginator.paginate.return_value = [
        {
            "Volumes": [
                {
                    "VolumeId": "vol-page1",
                    "Size": 10,
                    "CreateTime": now,
                    "Tags": [{"Key": "crepear.eligible", "Value": "true"}],
                }
            ]
        },
        {
            "Volumes": [
                {
                    "VolumeId": "vol-page2",
                    "Size": 20,
                    "CreateTime": now,
                    "Tags": [{"Key": "crepear.eligible", "Value": "true"}],
                }
            ]
        },
    ]

    provider = AWSProvider(region="us-east-1", client=mock_client)
    zombies = provider.audit()

    assert [z.id for z in zombies] == ["vol-page1", "vol-page2"]
    mock_client.get_paginator.assert_called_once_with("describe_volumes")
