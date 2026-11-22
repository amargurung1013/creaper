from datetime import timedelta

import boto3
from moto import mock_aws

from crepear.providers.aws_provider import AWSProvider


@mock_aws
def test_aws_audit_filters_protected_and_custom_tags():
    ec2 = boto3.client("ec2", region_name="us-east-1")

    # 1. Zombie volume (should be caught)
    zombie_vol = ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=50,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "Name", "Value": "orphan-vol"}]}
        ],
    )
    # 2. Protected volume via default crepear.ignore=true (should be skipped)
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=100,
        VolumeType="gp3",
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "crepear.ignore", "Value": "true"}]}
        ],
    )
    # 3. Protected volume via custom --exclude-label env=prod (should be skipped)
    ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=80,
        VolumeType="gp3",
        TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": "env", "Value": "prod"}]}],
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
    ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp3")

    provider = AWSProvider(region="us-east-1", client=ec2)
    # Volume was just created, so asking for > 7 days old should return nothing
    zombies = provider.audit(older_than=timedelta(days=7))

    assert zombies == []


@mock_aws
def test_aws_clean_deletes_volume():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vol = ec2.create_volume(AvailabilityZone="us-east-1a", Size=10, VolumeType="gp3")
    vol_id = vol["VolumeId"]

    provider = AWSProvider(region="us-east-1", client=ec2)
    zombies = provider.audit()
    deleted_ids = provider.clean(zombies)

    assert deleted_ids == [vol_id]
    # Confirm the volume is actually gone in mocked AWS
    remaining = ec2.describe_volumes()["Volumes"]
    assert remaining == []
