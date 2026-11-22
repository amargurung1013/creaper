from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from docker.errors import DockerException

from crepear.cli import calculate_finops
from crepear.models import ZombieResource, parse_duration
from crepear.providers.docker_provider import DockerProvider


def _make_fake_container(
    short_id: str,
    name: str,
    days_old: int,
    finished_days_old: int | None = None,
    size_rw: int = 10 * 1024 * 1024,  # 10 MB default
    labels: dict | None = None,
) -> MagicMock:
    """Helper to build a fake Docker container object."""
    created_dt = datetime.now(timezone.utc) - timedelta(days=days_old)
    fin_days = days_old if finished_days_old is None else finished_days_old
    finished_dt = datetime.now(timezone.utc) - timedelta(days=fin_days)

    container = MagicMock()
    container.short_id = short_id
    container.name = name
    container.labels = labels or {}
    container.attrs = {
        "Created": created_dt.isoformat().replace("+00:00", "Z"),
        "State": {"FinishedAt": finished_dt.isoformat().replace("+00:00", "Z")},
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


def test_audit_uses_finished_at_not_created_time():
    mock_client = MagicMock()
    # Created 90 days ago, but only exited 1 day ago -> should NOT be flagged by 7d retention
    mock_client.containers.list.return_value = [
        _make_fake_container("longrun1", "recently-stopped-db", days_old=90, finished_days_old=1),
    ]
    mock_client.images.list.return_value = []

    provider = DockerProvider(client=mock_client)
    zombies = provider.audit(older_than=timedelta(days=7))

    assert zombies == []


def test_clean_deletes_containers_and_images():
    mock_client = MagicMock()
    fake_container_obj = MagicMock()
    fake_container_obj.attrs = {
        "State": {"Status": "exited", "Running": False, "FinishedAt": "0001-01-01T00:00:00Z"},
        "Created": datetime.now(timezone.utc).isoformat(),
    }
    mock_client.containers.get.return_value = fake_container_obj
    fake_image_obj = MagicMock()
    fake_image_obj.attrs = {"RepoTags": []}
    mock_client.images.get.return_value = fake_image_obj

    provider = DockerProvider(client=mock_client)
    now = datetime.now(timezone.utc)
    resources = [
        ZombieResource("c1", "old-app", "container", 1024, now, "docker"),
        ZombieResource("img1", "<dangling>", "image", 2048, now, "docker"),
    ]

    deleted = provider.clean(resources)

    assert deleted.deleted == ["c1", "img1"]
    assert deleted.failed == {}
    mock_client.containers.get.assert_called_once_with("c1")
    fake_container_obj.remove.assert_called_once()
    mock_client.images.remove.assert_called_once_with("img1")


def test_calculate_finops_math():
    now = datetime.now(timezone.utc)
    one_gb = 1024 * 1024 * 1024
    zombies = [ZombieResource("img1", "<dangling>", "image", one_gb, now, "docker")]

    metrics = calculate_finops(zombies, cost_per_gb_month=0.08, provider="aws")

    assert metrics["count"] == 1
    assert metrics["total_gb"] == 1.0
    assert metrics["est_monthly_savings_usd"] == 0.08


def test_clean_continues_on_individual_failure():
    mock_client = MagicMock()
    bad_container = MagicMock()
    bad_container.attrs = {
        "State": {"Status": "exited", "Running": False, "FinishedAt": "0001-01-01T00:00:00Z"},
        "Created": datetime.now(timezone.utc).isoformat(),
    }
    bad_container.remove.side_effect = DockerException("container is in use")
    mock_client.containers.get.return_value = bad_container
    image = MagicMock()
    image.attrs = {"RepoTags": []}
    mock_client.images.get.return_value = image

    provider = DockerProvider(client=mock_client)
    now = datetime.now(timezone.utc)
    resources = [
        ZombieResource("c1", "locked-app", "container", 1024, now, "docker"),
        ZombieResource("img1", "<dangling>", "image", 2048, now, "docker"),
    ]

    result = provider.clean(resources)

    assert result.deleted == ["img1"]
    assert "c1" in result.failed
    assert "container is in use" in result.failed["c1"]


def test_container_zero_writable_layer_is_reported_as_estimate():
    client = MagicMock()
    client.containers.list.return_value = [_make_fake_container("c1", "empty", 10, size_rw=0)]
    client.images.list.return_value = []
    resource = DockerProvider(client=client).audit()[0]
    assert resource.size_bytes == 0
    assert resource.size_is_estimate is True
    assert "writable layer" in resource.storage_note


def test_shared_image_layers_are_not_counted_twice():
    client = MagicMock()
    first = _make_fake_image("img1", 10, size=100)
    second = _make_fake_image("img2", 10, size=200)
    first.attrs["RootFS"] = {"Layers": ["layer-a", "layer-shared"]}
    second.attrs["RootFS"] = {"Layers": ["layer-shared", "layer-b"]}
    client.containers.list.return_value = []
    client.images.list.return_value = [first, second]
    resources = DockerProvider(client=client).audit()
    assert [resource.size_bytes for resource in resources] == [100, 0]
    assert "shared" in resources[1].storage_note


def test_unique_images_keep_approximate_sizes():
    client = MagicMock()
    first = _make_fake_image("img1", 10, size=100)
    second = _make_fake_image("img2", 10, size=200)
    first.attrs["RootFS"] = {"Layers": ["layer-a"]}
    second.attrs["RootFS"] = {"Layers": ["layer-b"]}
    client.containers.list.return_value = []
    client.images.list.return_value = [first, second]
    resources = DockerProvider(client=client).audit()
    assert [resource.size_bytes for resource in resources] == [100, 200]


def test_invalid_image_size_metadata_is_zero_and_explicit():
    client = MagicMock()
    image = _make_fake_image("img1", 10, size="unknown")
    client.containers.list.return_value = []
    client.images.list.return_value = [image]
    resource = DockerProvider(client=client).audit()[0]
    assert resource.size_bytes == 0
    assert "missing or invalid" in resource.storage_note


@pytest.mark.parametrize("price", [-1.0, float("nan"), float("inf"), float("-inf")])
def test_calculate_finops_rejects_invalid_pricing(price):
    with pytest.raises(ValueError):
        calculate_finops([], price)


def test_calculate_finops_accepts_zero_and_empty_resources():
    metrics = calculate_finops([], 0.0)
    assert metrics["total_gb"] == 0.0
    assert metrics["est_monthly_savings_usd"] == 0.0


def _clean_container(status="exited", finished_days_old=30, labels=None):
    container = _make_fake_container(
        "c1", "app", days_old=finished_days_old, finished_days_old=finished_days_old, labels=labels
    )
    container.attrs["State"]["Status"] = status
    container.attrs["State"]["Running"] = status == "running"
    return container


def test_clean_revalidates_and_deletes_stopped_container():
    client = MagicMock()
    client.containers.get.return_value = _clean_container()
    result = DockerProvider(client=client).clean(
        [ZombieResource("c1", "app", "container", 0, datetime.now(timezone.utc), "docker")]
    )
    assert result.deleted == ["c1"]
    client.containers.get.return_value.remove.assert_called_once()


@pytest.mark.parametrize(
    "resource, setup, expected",
    [
        (
            ZombieResource("c1", "app", "container", 0, datetime.now(timezone.utc), "aws"),
            "container",
            "Docker provider",
        ),
        (
            ZombieResource("x", "x", "volume", 0, datetime.now(timezone.utc), "docker"),
            "none",
            "unsupported",
        ),
    ],
)
def test_clean_rejects_invalid_provider_and_resource_type(resource, setup, expected):
    result = DockerProvider(client=MagicMock()).clean([resource])
    assert resource.id in result.failed
    assert expected.lower() in result.failed[resource.id].lower()


def test_clean_rejects_running_excluded_and_too_new_containers():
    client = MagicMock()
    provider = DockerProvider(client=client)
    resources = [
        ZombieResource("running", "running", "container", 0, datetime.now(timezone.utc), "docker"),
        ZombieResource(
            "excluded", "excluded", "container", 0, datetime.now(timezone.utc), "docker"
        ),
        ZombieResource("new", "new", "container", 0, datetime.now(timezone.utc), "docker"),
    ]
    client.containers.get.side_effect = [
        _clean_container(status="running"),
        _clean_container(labels={"crepear.ignore": "true"}),
        _clean_container(finished_days_old=1),
    ]
    result = provider.clean(resources, older_than=timedelta(days=7))
    assert set(result.failed) == {"running", "excluded", "new"}
    client.containers.get.return_value.remove.assert_not_called()


def test_clean_handles_missing_container_and_api_failure_individually():
    client = MagicMock()
    client.containers.get.side_effect = [
        DockerException("not found"),
        DockerException("daemon failed"),
    ]
    resources = [
        ZombieResource("missing", "missing", "container", 0, datetime.now(timezone.utc), "docker"),
        ZombieResource("broken", "broken", "container", 0, datetime.now(timezone.utc), "docker"),
    ]
    result = DockerProvider(client=client).clean(resources)
    assert set(result.failed) == {"missing", "broken"}
    assert "not found" in result.failed["missing"]


def test_clean_revalidates_dangling_image():
    client = MagicMock()
    image = MagicMock()
    image.attrs = {"RepoTags": []}
    client.images.get.return_value = image
    result = DockerProvider(client=client).clean(
        [ZombieResource("img1", "image", "image", 0, datetime.now(timezone.utc), "docker")]
    )
    assert result.deleted == ["img1"]
    client.images.remove.assert_called_once_with("img1")


def test_clean_rejects_image_that_is_no_longer_dangling():
    client = MagicMock()
    image = MagicMock()
    image.attrs = {"RepoTags": ["example:latest"]}
    client.images.get.return_value = image
    result = DockerProvider(client=client).clean(
        [ZombieResource("img1", "image", "image", 0, datetime.now(timezone.utc), "docker")]
    )
    assert "no longer dangling" in result.failed["img1"]
    client.images.remove.assert_not_called()


def test_clean_records_image_deletion_failure():
    client = MagicMock()
    image = MagicMock()
    image.attrs = {"RepoTags": []}
    client.images.get.return_value = image
    client.images.remove.side_effect = DockerException("image is in use")
    result = DockerProvider(client=client).clean(
        [ZombieResource("img1", "image", "image", 0, datetime.now(timezone.utc), "docker")]
    )
    assert result.deleted == []
    assert "image is in use" in result.failed["img1"]


def test_parse_timestamp_handles_timezone_naive_and_rejects_malformed_values():
    provider = DockerProvider(client=MagicMock())
    assert provider._parse_timestamp("2026-01-01T00:00:00").tzinfo == timezone.utc
    assert provider._parse_timestamp("2026-01-01T00:00:00Z").tzinfo == timezone.utc
    with pytest.raises(ValueError, match="timestamp"):
        provider._parse_timestamp("")
    with pytest.raises(ValueError, match="Invalid Docker timestamp"):
        provider._parse_timestamp("not-a-timestamp")


def test_audit_skips_one_malformed_timestamp_and_keeps_other_resources():
    client = MagicMock()
    malformed = _make_fake_container("bad", "bad", 10)
    malformed.attrs["State"]["FinishedAt"] = "bad"
    valid = _make_fake_container("good", "good", 10)
    client.containers.list.return_value = [malformed, valid]
    client.images.list.return_value = []
    assert [resource.id for resource in DockerProvider(client=client).audit()] == ["good"]


def test_image_layer_sizes_account_for_shared_and_unique_layers():
    client = MagicMock()
    first = _make_fake_image("img1", 10, size=999)
    second = _make_fake_image("img2", 10, size=999)
    first.attrs["RootFS"] = {"Layers": ["a", "b", "c"], "LayerSizes": [10, 20, 30]}
    second.attrs["RootFS"] = {"Layers": ["b", "c", "d"], "LayerSizes": [20, 30, 40]}
    client.containers.list.return_value = []
    client.images.list.return_value = [first, second]
    resources = DockerProvider(client=client).audit()
    assert [resource.size_bytes for resource in resources] == [60, 40]


def test_image_with_all_shared_layers_has_no_incremental_estimate():
    client = MagicMock()
    first = _make_fake_image("img1", 10, size=100)
    second = _make_fake_image("img2", 10, size=200)
    first.attrs["RootFS"] = {"Layers": ["a", "b"], "LayerSizes": [40, 60]}
    second.attrs["RootFS"] = {"Layers": ["a", "b"], "LayerSizes": [40, 60]}
    client.containers.list.return_value = []
    client.images.list.return_value = [first, second]
    resources = DockerProvider(client=client).audit()
    assert [resource.size_bytes for resource in resources] == [100, 0]


def test_empty_image_metadata_is_safe():
    client = MagicMock()
    image = _make_fake_image("empty", 10, size=0)
    image.attrs["RootFS"] = {"Layers": []}
    client.containers.list.return_value = []
    client.images.list.return_value = [image]
    resource = DockerProvider(client=client).audit()[0]
    assert resource.size_bytes == 0
    assert resource.size_is_estimate is True
