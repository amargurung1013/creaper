# Crepear

Crepear is a safe-by-default Python CLI for auditing stale infrastructure and estimating storage that could be reclaimed. It supports stopped Docker containers, dangling Docker images, and unattached AWS EBS volumes.

## Why it exists

Unused resources accumulate quietly. Crepear finds candidates without changing anything, applies age and exclusion rules, reports storage estimates, and only deletes resources after an explicit cleanup request. AWS deletion is opt-in: volumes must have `crepear.eligible=true`.

## Features

- Docker and AWS providers behind one provider interface.
- Age filtering with `7d`, `24h`, and `30m` syntax.
- Built-in and user-supplied exclusion labels/tags.
- Dry-run by default; `--clean` enables deletion.
- Interactive confirmation for terminal cleanup and `--force` for automation.
- JSON output for CI and scripts.
- Per-resource cleanup success and failure reporting.
- Conservative Docker storage estimates and AWS savings estimates.

## Supported providers

| Provider | Resources | Deletion authorization |
| --- | --- | --- |
| Docker | exited containers, dangling images | resources returned by the audit |
| AWS | available EBS volumes | `crepear.eligible=true`, with no exclusion tag |

## Architecture

`cli.py` validates arguments, selects a provider, audits, calculates metrics, and optionally cleans. `BaseProvider` defines the audit/cleanup contract. `DockerProvider` and `AWSProvider` translate provider APIs into `ZombieResource` objects. `CleanupResult` keeps successful and failed deletes separate.

```text
CLI -> BaseProvider -> DockerProvider / AWSProvider -> provider API
  \-> metrics and terminal or JSON reporting
```

## Project layout

```text
src/crepear/
  cli.py
  models.py
  providers/base.py
  providers/docker_provider.py
  providers/aws_provider.py
tests/
  test_cli.py
  test_docker_provider.py
  test_aws_provider.py
```

## Technology stack

Python 3.10+, Typer, Docker SDK for Python, boto3, Rich, pytest, Moto, and Ruff. The package is built with setuptools and uses a `src` layout.

## Installation

```bash
uv venv
uv pip install -e ".[dev]"
```

Or install the runtime package with `pip install -e .`. Docker usage requires access to a running Docker daemon.

## Docker usage

```bash
crepear --provider docker
crepear --provider docker --older-than 7d
crepear --provider docker --exclude-label env=prod
crepear --provider docker --clean
crepear --provider docker --clean --force
```

Docker `SizeRw` is only a container writable-layer size. Image `Size` is a virtual size and can include shared layers. Crepear labels these values as approximations and avoids counting an identified shared layer twice; exact reclaimable storage depends on Docker's storage driver and metadata.

## AWS configuration

Configure credentials using the AWS CLI, environment variables, or an IAM role:

```bash
aws configure
crepear --provider aws --region us-east-1
```

Required IAM permissions are:

```text
ec2:DescribeVolumes
ec2:DeleteVolume
```

Grant access only to the intended account and regions. AWS cleanup fetches each volume again immediately before deletion and rechecks its state and tags.

## Tags and exclusions

AWS cleanup requires `crepear.eligible=true`. The built-in exclusion is `crepear.ignore=true`. Add more rules with repeated `--exclude-label` options, such as `--exclude-label env=prod` or `--exclude-label backup=true`. An exclusion wins over eligibility.

## Audit and cleanup examples

```bash
crepear --provider aws --older-than 30d --cost-per-gb 0.08
crepear --provider aws --clean --force --cost-per-gb 0.08
crepear --provider docker --json
crepear --provider aws --clean --force --json
```

Without `--clean`, the command is a dry run. Terminal cleanup asks for confirmation unless `--force` is supplied. JSON cleanup requires `--force` so that it never emits an interactive prompt into a pipeline.

## JSON output

The JSON response is emitted only after cleanup has completed when requested:

```json
{
  "provider": "aws",
  "dry_run": false,
  "metrics": {"count": 1, "total_mb": 1024.0, "total_gb": 1.0, "est_monthly_savings_usd": 0.08, "storage_estimate": false},
  "resources": [{"id": "vol-0123456789", "name": "data", "type": "ebs_volume", "size_mb": 1024.0, "created_at": "2026-01-01T00:00:00+00:00", "provider": "aws", "labels": {"crepear.eligible": "true"}, "size_is_estimate": false, "storage_note": null}],
  "cleanup": {"deleted": ["vol-0123456789"], "failed": {}}
}
```

Diagnostics go to stderr where applicable. Resource entries include IDs, metadata, tags, timestamps, and storage-estimate notes.

## Cost estimation limitations

`--cost-per-gb` must be finite and non-negative. AWS savings are estimates only: actual prices depend on region, volume type, tier, discounts, and usage. The default is an indicative AWS gp3 value, not a billing guarantee. Docker uses local storage estimates and does not apply AWS pricing assumptions; its financial savings value is not a cloud bill forecast.

## Tests and CI

```bash
make test
make lint
```

GitHub Actions should run the same pytest and Ruff checks on supported Python versions. Tests use mocks for provider API boundaries and Moto for AWS behavior; they verify business rules, including per-resource cleanup failures.

## Known limitations

- Docker's public API does not always expose per-layer byte sizes, so exact reclaimable bytes cannot be guaranteed.
- AWS savings do not model every EBS volume type, region, snapshot, or discount.
- The current AWS provider audits EBS volumes only.
- Credentials and Docker daemon availability are external prerequisites.
