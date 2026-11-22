import json
import math
from typing import Annotated

import typer
from botocore.exceptions import ClientError, NoCredentialsError
from docker.errors import DockerException
from rich.console import Console
from rich.table import Table

from crepear.models import ZombieResource, parse_duration
from crepear.providers.aws_provider import AWSProvider
from crepear.providers.base import BaseProvider
from crepear.providers.docker_provider import DockerProvider

app = typer.Typer(help="FinOps infrastructure auditing and zombie resource cleanup CLI.")
console = Console()


def get_provider(provider_name: str, region: str) -> BaseProvider:
    """Factory function to instantiate the requested infrastructure provider."""
    name = provider_name.strip().lower()
    if name == "docker":
        return DockerProvider()
    if name == "aws":
        return AWSProvider(region=region)
    raise ValueError(f"Unsupported provider '{provider_name}'. Choose 'docker' or 'aws'.")


def calculate_finops(
    zombies: list[ZombieResource], cost_per_gb_month: float, provider: str = "aws"
) -> dict:
    if not math.isfinite(cost_per_gb_month) or cost_per_gb_month < 0:
        raise ValueError("cost per GB must be a finite, non-negative number")
    total_bytes = sum(z.size_bytes for z in zombies)
    total_mb = round(total_bytes / (1024 * 1024), 2)
    total_gb = round(total_bytes / (1024 * 1024 * 1024), 4)
    est_monthly_savings = (
        max(0.0, round(total_gb * cost_per_gb_month, 4)) if provider == "aws" else 0.0
    )
    return {
        "count": len(zombies),
        "total_mb": total_mb,
        "total_gb": total_gb,
        "est_monthly_savings_usd": est_monthly_savings,
        "savings_type": "aws_estimate" if provider == "aws" else "not_applicable",
        "storage_estimate": any(z.size_is_estimate for z in zombies),
    }


def render_table(zombies: list[ZombieResource], metrics: dict, dry_run: bool) -> None:
    mode = (
        "[bold yellow]DRY-RUN (Safe Mode)[/bold yellow]"
        if dry_run
        else "[bold red]CLEAN MODE[/bold red]"
    )
    table = Table(title=f"crepear FinOps Audit — {mode}")

    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name", style="white")
    table.add_column("Type", style="magenta")
    table.add_column("Size (MB)", justify="right", style="green")
    table.add_column("Created (UTC)", style="dim")

    for z in zombies:
        table.add_row(
            z.id,
            z.name,
            z.resource_type,
            f"{z.size_mb:.2f}",
            z.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)
    savings_label = (
        "Estimated AWS Savings"
        if any(z.provider == "aws" for z in zombies)
        else "Docker Storage Only (no cloud savings calculated)"
    )
    console.print(
        f"\n[bold]Reclaimable:[/bold] {metrics['total_mb']} MB ({metrics['total_gb']} GB) | "
        f"[bold]{savings_label}:[/bold] ${metrics['est_monthly_savings_usd']}/mo"
    )


@app.command()
def main(
    provider: Annotated[
        str,
        typer.Option("--provider", help="Infrastructure provider to audit ('docker' or 'aws')."),
    ] = "docker",
    region: Annotated[
        str,
        typer.Option("--region", help="AWS region (only used when --provider aws)."),
    ] = "us-east-1",
    clean: Annotated[
        bool, typer.Option("--clean", help="Actually delete zombie resources.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Skip confirmation prompt when using --clean.")
    ] = False,
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than", help="Only include resources older than this (e.g., 7d, 24h, 30m)."
        ),
    ] = None,
    exclude_label: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-label",
            help="Exclude resources with this label/tag (can be used multiple times).",
        ),
    ] = None,
    cost_per_gb: Annotated[
        float,
        typer.Option(
            "--cost-per-gb",
            help="Projected cloud storage cost per GB/month (default: AWS gp3 $0.08).",
        ),
    ] = 0.08,
    json_output: Annotated[
        bool, typer.Option("--json", help="Output raw JSON for CI/CD or scripts.")
    ] = False,
) -> None:
    def error(message: str) -> None:
        typer.echo(message, err=True)

    if force and not clean:
        error("Error: --force can only be used together with --clean.")
        raise typer.Exit(code=2)

    try:
        metrics = calculate_finops([], cost_per_gb, provider=provider.strip().lower())
    except ValueError as e:
        error(f"Error: {e}")
        raise typer.Exit(code=2) from None

    # 1. Validate --older-than flag
    retention = None
    if older_than:
        try:
            retention = parse_duration(older_than)
        except ValueError as e:
            error(f"Error: {e}")
            raise typer.Exit(code=1) from None

    # 2. Initialize selected provider
    try:
        active_provider = get_provider(provider, region)
    except ValueError as e:
        error(f"Error: {e}")
        raise typer.Exit(code=1) from None

    # 3. Run the audit (read-only)
    try:
        zombies = active_provider.audit(older_than=retention, exclude_labels=exclude_label)
    except NoCredentialsError:
        error(
            "AWS Error: No credentials found. "
            "Run `aws configure` or set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
        )
        raise typer.Exit(code=1) from None
    except ClientError as e:
        error(f"AWS API Error: {e}")
        raise typer.Exit(code=1) from None
    except DockerException as e:
        error(f"Docker Error: Could not connect to Docker daemon. ({e})")
        raise typer.Exit(code=1) from None

    metrics = calculate_finops(zombies, cost_per_gb, provider=provider.strip().lower())

    payload = {
        "provider": provider.strip().lower(),
        "dry_run": not clean,
        "metrics": metrics,
        "resources": [
            {
                "id": z.id,
                "name": z.name,
                "type": z.resource_type,
                "size_mb": z.size_mb,
                "created_at": z.created_at.isoformat(),
                "provider": z.provider,
                "labels": z.labels,
                "size_is_estimate": z.size_is_estimate,
                "storage_note": z.storage_note,
            }
            for z in zombies
        ],
        "cleanup": {"deleted": [], "failed": {}},
    }

    # JSON output must be one complete document and must never prompt.
    if json_output and clean and not force:
        payload["cleanup"]["failed"]["__authorization__"] = (
            "--clean with --json requires --force; no resources were deleted"
        )
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=1)

    if json_output and (not clean or not zombies):
        typer.echo(json.dumps(payload, indent=2))
        return

    # 5. Human-readable table mode
    if not json_output:
        if not zombies:
            console.print(
                "[bold green]No zombie resources found. Your environment is clean.[/bold green]"
            )
            return
        render_table(zombies, metrics, dry_run=not clean)

    # 6. Safe-by-default exit
    if not clean:
        if not json_output:
            console.print(
                "\n[yellow]Dry-run complete. Re-run with --clean to delete these resources.[/yellow]"
            )
        return

    # 7. Destructive action with confirmation guardrail
    if not force:
        confirmed = typer.confirm(
            f"Are you sure you want to permanently delete {len(zombies)} resource(s)?"
        )
        if not confirmed:
            console.print("[yellow]Aborted. Nothing was deleted.[/yellow]")
            raise typer.Exit(code=0)

    if isinstance(active_provider, AWSProvider):
        result = active_provider.clean(zombies, exclude_labels=exclude_label)
    else:
        result = active_provider.clean(zombies, older_than=retention, exclude_labels=exclude_label)
    payload["cleanup"] = {"deleted": result.deleted, "failed": result.failed}
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        if result.failed:
            raise typer.Exit(code=1)
        return
    if not json_output:
        console.print(f"[bold green]Deleted {len(result.deleted)} resource(s).[/bold green]")
        if result.failed:
            console.print(
                f"[bold red]Failed to delete {len(result.failed)} resource(s):[/bold red]"
            )
            for res_id, err in result.failed.items():
                console.print(f"  - [cyan]{res_id}[/cyan]: {err}")
    if result.failed:
        raise typer.Exit(code=1)
