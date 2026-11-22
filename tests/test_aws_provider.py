from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from crepear.models import ZombieResource
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


def _resource(resource_type="ebs_volume", provider="aws", resource_id="vol-1"):
    return ZombieResource(
        resource_id,
        resource_id,
        resource_type,
        1,
        datetime.now(timezone.utc),
        provider,
    )


def _aws_error(code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": "test failure"}}, "DescribeVolumes")


def test_aws_clean_revalidates_and_deletes_eligible_volume():
    client = MagicMock()
    client.describe_volumes.return_value = {
        "Volumes": [
            {
                "VolumeId": "vol-1",
                "State": "available",
                "Tags": [{"Key": "crepear.eligible", "Value": "true"}],
            }
        ]
    }
    result = AWSProvider(client=client).clean([_resource()])
    assert result.deleted == ["vol-1"]
    assert result.failed == {}
    client.delete_volume.assert_called_once_with(VolumeId="vol-1")


def test_aws_clean_rejects_ineligible_attached_excluded_and_missing_volumes():
    client = MagicMock()
    responses = [
        {"Volumes": [{"State": "available", "Tags": []}]},
        {"Volumes": [{"State": "in-use", "Tags": [{"Key": "crepear.eligible", "Value": "true"}]}]},
        {
            "Volumes": [
                {
                    "State": "available",
                    "Tags": [
                        {"Key": "crepear.eligible", "Value": "true"},
                        {"Key": "crepear.ignore", "Value": "true"},
                    ],
                }
            ]
        },
        {"Volumes": []},
    ]
    client.describe_volumes.side_effect = responses
    resources = [_resource(resource_id=f"vol-{i}") for i in range(4)]
    result = AWSProvider(client=client).clean(resources)
    assert result.deleted == []
    assert set(result.failed) == {f"vol-{i}" for i in range(4)}
    client.delete_volume.assert_not_called()


def test_aws_clean_rejects_invalid_resource_and_provider():
    client = MagicMock()
    result = AWSProvider(client=client).clean(
        [
            _resource(resource_type="instance"),
            _resource(provider="docker", resource_id="container-1"),
        ]
    )
    assert set(result.failed) == {"vol-1", "container-1"}
    client.describe_volumes.assert_not_called()


def test_aws_clean_records_missing_and_api_failures_without_stopping():
    client = MagicMock()
    client.describe_volumes.side_effect = [_aws_error(), {"Volumes": []}]
    result = AWSProvider(client=client).clean([_resource(), _resource(resource_id="vol-2")])
    assert set(result.failed) == {"vol-1", "vol-2"}
    assert "test failure" in result.failed["vol-1"]
