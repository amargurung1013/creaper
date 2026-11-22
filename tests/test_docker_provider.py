from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from crepear.cli import calculate_finops
from crepear.models import ZombieResource, parse_duration
from crepear.providers.docker_provider import DockerProvider


def _make_fake_container(
    short_id: str,
    name: str,
    days_old: int,
    size_rw: int = 10 * 1024 * 1024,  # 10 MB default
    labels: dict | None = None,
) -> MagicMock:
    """Helper to build a fake Docker container object."""
    created_dt = datetime.now(timezone.utc) - timedelta(days=days_old)
    container = MagicMock()
    container.short_id = short_id
    container.name = name
    container.labels = labels or {}
    container.attrs = {
        "Created": created_dt.isoformat().replace("+00:00", "Z"),
        "SizeRw": size_rw,
    }
    return container


def _make_fake_image(short_id: str, days_old: int, size: int = 50 * 1024 * 1024) -> MagicMock:
    """Helper to build a fake dangling Docker image."""
    created_dt = datetime.now(timezone.utc) - timedelta(days=days_old)
    image = MagicMock()
    image.short_id = f"sha256:{short_id}"
    image.labels = {}
    image.attrs = {
        "Created": created_dt.isoformat().replace("+00:00", "Z"),
        "Size": size,
    }
    return image


def test_parse_duration_valid():
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("30m") == timedelta(minutes=30)


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("7weeks")


def test_audit_respects_older_than_and_exclusions():
    mock_client = MagicMock()
    mock_client.containers.list.return_value = [
        _make_fake_container("old1", "old-app", days_old=15),
        _make_fake_container("new1", "recent-app", days_old=2),
        _make_fake_container(
            "prot1", "protected-db", days_old=30, labels={"crepear.ignore": "true"}
        ),
        _make_fake_container("cust1", "custom-excl", days_old=30, labels={"env": "prod"}),
    ]
    mock_client.images.list.return_value = [
        _make_fake_image("img1", days_old=20),
    ]

    provider = DockerProvider(client=mock_client)
    zombies = provider.audit(older_than=timedelta(days=7), exclude_labels=["env=prod"])

    found_ids = [z.id for z in zombies]
    assert found_ids == ["old1", "img1"]


def test_clean_deletes_containers_and_images():
    mock_client = MagicMock()
    fake_container_obj = MagicMock()
    mock_client.containers.get.return_value = fake_container_obj

    provider = DockerProvider(client=mock_client)
    now = datetime.now(timezone.utc)
    resources = [
        ZombieResource("c1", "old-app", "container", 1024, now, "docker"),
        ZombieResource("img1", "<dangling>", "image", 2048, now, "docker"),
    ]

    deleted = provider.clean(resources)

    assert deleted == ["c1", "img1"]
    mock_client.containers.get.assert_called_once_with("c1")
    fake_container_obj.remove.assert_called_once()
    mock_client.images.remove.assert_called_once_with("img1")


def test_calculate_finops_math():
    now = datetime.now(timezone.utc)
    one_gb = 1024 * 1024 * 1024
    zombies = [ZombieResource("img1", "<dangling>", "image", one_gb, now, "docker")]

    metrics = calculate_finops(zombies, cost_per_gb_month=0.08)

    assert metrics["count"] == 1
    assert metrics["total_gb"] == 1.0
    assert metrics["est_monthly_savings_usd"] == 0.08
