"""JobPilot command-line interface.

    jobpilot init        # create your config/profile/answer files
    jobpilot discover    # pull jobs from enabled public sources
    jobpilot match       # score jobs against your preferences (no AI)
    jobpilot review      # inspect / approve jobs waiting on you
    jobpilot apply       # fill (and optionally submit) applications
    jobpilot run         # discover + match in one shot
    jobpilot status      # counts by stage
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from jobpilot import __version__, storage
from jobpilot.config import (
    _PKG_ROOT, load_preferences, using_example_config,
)
from jobpilot.matching import evaluate
from jobpilot.models import Job

app = typer.Typer(add_completion=False, help="No-AI, config-driven job application bot.")
console = Console()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _row_to_job(row) -> Job:
    return Job(
        source=row["source"], company=row["company"], title=row["title"],
        url=row["url"], location=row["location"] or "", remote=bool(row["remote"]),
        description=row["description"] or "", ats=row["ats"] or "",
        employment_type=row["employment_type"] or "",
        salary_min=row["salary_min"], salary_max=row["salary_max"],
        posted_at=row["posted_at"], job_id=row["job_id"],
    )


@app.command()
def init(force: bool = typer.Option(False, help="Overwrite existing files.")) -> None:
    """Copy the example config/profile/answers into the current directory."""
    pairs = [
        ("config.example.yaml", "config.yaml"),
        ("profile.example.yaml", "profile.yaml"),
        ("answers.example.yaml", "answers.yaml"),
    ]
    cwd = Path.cwd()
    for example, target in pairs:
        src = _PKG_ROOT / example
        dst = cwd / target
        if dst.exists() and not force:
            console.print(f"[yellow]skip[/yellow] {target} (exists — use --force)")
            continue
        if not src.exists():
            console.print(f"[red]missing bundled example:[/red] {example}")
            continue
        shutil.copy(src, dst)
        console.print(f"[green]created[/green] {target}")
    console.print(
        "\nEdit [bold]config.yaml[/bold] (preferences), [bold]profile.yaml[/bold] "
        "(your data + resume path), and [bold]answers.yaml[/bold] (question answers).\n"
        "Then: [cyan]jobpilot run[/cyan] → [cyan]jobpilot apply[/cyan]"
    )


@app.command()
def discover() -> None:
    """Pull jobs from every enabled public source into the database."""
    from jobpilot.discovery import run_discovery
    _warn_example()
    prefs = load_preferences()
    jobs = run_discovery(prefs)
    inserted = storage.upsert_jobs(jobs)
    console.print(f"[green]Discovered {len(jobs)} jobs, {inserted} new.[/green]")


@app.command()
def match() -> None:
    """Score every new job against your preferences (deterministic, no AI)."""
    _warn_example()
    prefs = load_preferences()
    rows = storage.new_jobs()
    if not rows:
        console.print("[yellow]No new jobs to score. Run `jobpilot discover`.[/yellow]")
        return
    queued = skipped = 0
    for row in rows:
        job = _row_to_job(row)
        score, reasons, passed = evaluate(job, prefs)
        if passed and score >= prefs.min_score:
            storage.set_score(job.job_id, score, reasons, "queued")
            queued += 1
        else:
            if passed:
                reasons.append(f"below min_score ({score} < {prefs.min_score})")
            storage.set_score(job.job_id, score, reasons, "skipped")
            skipped += 1
    console.print(
        f"[green]Scored {len(rows)} jobs:[/green] {queued} queued, {skipped} skipped."
    )


@app.command()
def review(
    approve: str = typer.Option(None, help="job_id to approve for submission."),
    reject: str = typer.Option(None, help="job_id to reject/skip."),
    approve_all: bool = typer.Option(False, help="Approve every job in review."),
    limit: int = typer.Option(30, help="How many review-queue rows to list."),
) -> None:
    """List jobs awaiting your decision, or approve/reject them."""
    if approve:
        n = storage.set_status(approve, "approved")
        console.print(f"[green]Approved[/green] {approve}" if n else "job_id not found")
        return
    if reject:
        n = storage.set_status(reject, "skipped")
        console.print(f"[green]Rejected[/green] {reject}" if n else "job_id not found")
        return
    if approve_all:
        rows = storage.rows_by_status("needs_review")
        for r in rows:
            storage.set_status(r["job_id"], "approved")
        console.print(f"[green]Approved {len(rows)} jobs.[/green]")
        return

    rows = storage.rows_by_status("needs_review")[:limit]
    if not rows:
        console.print("[green]Nothing waiting for review.[/green]")
        return
    table = Table(title="Jobs needing review", show_lines=False)
    table.add_column("job_id", style="dim")
    table.add_column("score", justify="right")
    table.add_column("title")
    table.add_column("company")
    table.add_column("why", overflow="fold")
    for r in rows:
        table.add_row(r["job_id"], str(r["score"]), r["title"][:40],
                      r["company"][:20], (r["apply_error"] or "")[:50])
    console.print(table)
    console.print("\nApprove one: [cyan]jobpilot review --approve <job_id>[/cyan]  "
                  "| all: [cyan]jobpilot review --approve-all[/cyan]")


@app.command()
def apply(
    dry_run: bool = typer.Option(False, help="Fill forms but never click Submit."),
    limit: int = typer.Option(None, help="Max applications this run."),
) -> None:
    """Fill (and, if configured, submit) applications for queued/approved jobs."""
    _warn_example()
    from jobpilot.apply.runner import run
    run(dry_run=dry_run, limit=limit)


@app.command()
def run(
    apply_after: bool = typer.Option(False, "--apply", help="Also run apply afterwards."),
) -> None:
    """Discover then match. Add --apply to chain the apply step too."""
    discover()
    match()
    if apply_after:
        from jobpilot.apply.runner import run as run_apply
        run_apply()


@app.command()
def status() -> None:
    """Show job counts by stage."""
    s = storage.stats()
    table = Table(title="JobPilot status")
    table.add_column("stage")
    table.add_column("count", justify="right")
    for key in ("new", "queued", "approved", "needs_review", "applied", "failed", "skipped"):
        table.add_row(key, str(s.get(key, 0)))
    table.add_row("[bold]total[/bold]", f"[bold]{s.get('total', 0)}[/bold]")
    console.print(table)


@app.command()
def doctor(
    skip_network: bool = typer.Option(False, help="Skip connectivity probes."),
    skip_browser: bool = typer.Option(False, help="Skip the browser launch check."),
) -> None:
    """Preflight: verify config, profile, resume, browser, and network."""
    from jobpilot.doctor import run_doctor
    raise SystemExit(1 if run_doctor(skip_network=skip_network,
                                     skip_browser=skip_browser) else 0)


@app.command()
def version() -> None:
    """Print the JobPilot version."""
    console.print(f"JobPilot {__version__}")


def _warn_example() -> None:
    if using_example_config():
        console.print(
            "[yellow]Using bundled EXAMPLE config[/yellow] — run "
            "[cyan]jobpilot init[/cyan] and edit config.yaml/profile.yaml first "
            "for real use.\n"
        )


if __name__ == "__main__":
    app()
