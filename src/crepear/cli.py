import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from crepear.models import ZombieResource, parse_duration
from crepear.providers.docker_provider import DockerProvider

app = typer.Typer(help="FinOps infrastructure auditing and zombie resource cleanup CLI.")
console = Console()


def calculate_finops(zombies: list[ZombieResource], cost_per_gb_month: float) -> dict:
    total_bytes = sum(z.size_bytes for z in zombies)
    total_mb = round(total_bytes / (1024 * 1024), 2)
    total_gb = round(total_bytes / (1024 * 1024 * 1024), 4)
    est_monthly_savings = round(total_gb * cost_per_gb_month, 4)
    return {
        "count": len(zombies),
        "total_mb": total_mb,
        "total_gb": total_gb,
        "est_monthly_savings_usd": est_monthly_savings,
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
    console.print(
        f"\n[bold]Reclaimable:[/bold] {metrics['total_mb']} MB ({metrics['total_gb']} GB) | "
        f"[bold]Projected Cloud Savings:[/bold] ${metrics['est_monthly_savings_usd']}/mo"
    )


@app.command()
def main(
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
            help="Exclude resources with this label (can be used multiple times).",
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
    # 1. Validate --older-than flag
    retention = None
    if older_than:
        try:
            retention = parse_duration(older_than)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=1)

    # 2. Run the audit (read-only)
    provider = DockerProvider()
    zombies = provider.audit(older_than=retention, exclude_labels=exclude_label)
    metrics = calculate_finops(zombies, cost_per_gb)

    # 3. JSON output mode (for pipelines, cron, jq)
    if json_output:
        payload = {
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
                }
                for z in zombies
            ],
        }
        # Use print() instead of console.print() so rich doesn't wrap lines or inject color codes
        print(json.dumps(payload, indent=2))
        if not clean or not zombies:
            return

    # 4. Human-readable table mode
    if not json_output:
        if not zombies:
            console.print(
                "[bold green]No zombie resources found. Your environment is clean.[/bold green]"
            )
            return
        render_table(zombies, metrics, dry_run=not clean)

    # 5. Safe-by-default exit
    if not clean:
        if not json_output:
            console.print(
                "\n[yellow]Dry-run complete. Re-run with --clean to delete these resources.[/yellow]"
            )
        return

    # 6. Destructive action with confirmation guardrail
    if not force:
        confirmed = typer.confirm(
            f"Are you sure you want to permanently delete {len(zombies)} resource(s)?"
        )
        if not confirmed:
            console.print("[yellow]Aborted. Nothing was deleted.[/yellow]")
            raise typer.Exit(code=0)

    deleted_ids = provider.clean(zombies)
    if not json_output:
        console.print(f"[bold green]Deleted {len(deleted_ids)} resource(s).[/bold green]")
