import json
from datetime import datetime, timezone

from typer.testing import CliRunner

from crepear.cli import app
from crepear.models import CleanupResult, ZombieResource


class FakeProvider:
    def __init__(self, resources, cleanup=None):
        self.resources = resources
        self.cleanup = cleanup or CleanupResult()

    def audit(self, **kwargs):
        return self.resources

    def clean(self, resources):
        return self.cleanup


def _resource():
    return ZombieResource(
        "vol-1",
        "data",
        "ebs_volume",
        1024**3,
        datetime.now(timezone.utc),
        "aws",
        {"crepear.eligible": "true"},
    )


def test_json_audit_contains_resources_and_empty_cleanup(monkeypatch):
    monkeypatch.setattr("crepear.cli.get_provider", lambda *_: FakeProvider([_resource()]))
    result = CliRunner().invoke(app, ["--provider", "aws", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resources"][0]["id"] == "vol-1"
    assert payload["cleanup"] == {"deleted": [], "failed": {}}
    assert payload["dry_run"] is True


def test_json_cleanup_includes_successes_and_failures(monkeypatch):
    cleanup = CleanupResult(deleted=["vol-1"], failed={"vol-2": "gone"})
    monkeypatch.setattr("crepear.cli.get_provider", lambda *_: FakeProvider([_resource()], cleanup))
    result = CliRunner().invoke(app, ["--provider", "aws", "--clean", "--force", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["cleanup"] == {"deleted": ["vol-1"], "failed": {"vol-2": "gone"}}


def test_json_clean_requires_force_without_prompt(monkeypatch):
    monkeypatch.setattr("crepear.cli.get_provider", lambda *_: FakeProvider([_resource()]))
    result = CliRunner().invoke(app, ["--provider", "aws", "--clean", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "__authorization__" in payload["cleanup"]["failed"]


def test_force_without_clean_is_invalid():
    result = CliRunner().invoke(app, ["--force"])
    assert result.exit_code == 2
    assert "only be used" in result.stderr
