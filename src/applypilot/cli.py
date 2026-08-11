"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    volume: bool = typer.Option(
        False, "--volume",
        help="Maximum applications per day: lenient validation, larger score "
             "batches, cover letters only for top matches.",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    if volume:
        # Every throughput lever at once. Each of these trades a little
        # polish for a lot of submitted applications:
        #   lenient      -> no LLM judge, far fewer tailoring retries
        #   batch 20     -> ~20 jobs triaged per scoring request
        #   cover high   -> cover letters only where they might matter
        import os
        validation = "lenient"
        os.environ.setdefault("SCORE_BATCH_SIZE", "20")
        os.environ.setdefault("COVER_LETTER_MODE", "high")
        os.environ.setdefault("COVER_LETTER_MIN_SCORE", "8")
        console.print(
            "[bold yellow]Volume mode[/bold yellow] — lenient validation, "
            "batch-20 scoring, cover letters for score 8+ only\n"
        )

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def mail(
    days: int = typer.Option(30, "--days", help="How many days back to scan."),
    limit: int = typer.Option(200, "--limit", help="Max messages to examine."),
) -> None:
    """Fetch and classify job-related email (interview / offer / rejection / action needed)."""
    _bootstrap()

    from applypilot.mail import fetch_and_store

    console.print("\n[bold blue]Scanning inbox...[/bold blue]")
    stats = fetch_and_store(lookback_days=days, limit=limit)

    if stats.get("warning"):
        console.print(f"\n[bold red]WARNING:[/bold red] {stats['warning']}\n")

    if stats.get("error"):
        console.print(f"[red]{stats['error']}[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"\n  Examined [bold]{stats['fetched']}[/bold] messages, "
        f"stored [bold]{stats['stored']}[/bold] job-related "
        f"([dim]{stats['skipped']} personal/unrelated skipped[/dim])"
    )

    if stats["by_category"]:
        icons = {"interview": "[green]interview[/green]",
                 "offer": "[bold green]offer[/bold green]",
                 "rejection": "[red]rejection[/red]",
                 "action_needed": "[yellow]action needed[/yellow]",
                 "confirmation": "[dim]confirmation[/dim]",
                 "other": "[dim]other[/dim]"}
        console.print()
        for category, count in sorted(stats["by_category"].items(),
                                      key=lambda kv: -kv[1]):
            console.print(f"  {count:>4}  {icons.get(category, category)}")

    if stats["advanced"]:
        console.print(f"\n  [bold]{stats['advanced']}[/bold] application(s) advanced by email")

    # Reconcile what the bot says it submitted against employer acknowledgements
    from applypilot.mail import reconcile_applications
    rec = reconcile_applications()

    if rec["total_applied"]:
        console.print("\n[bold]Submission verification[/bold]")
        console.print(
            f"  [green]{rec['confirmed']}[/green] confirmed by employer  "
            f"[yellow]{rec['pending']}[/yellow] awaiting reply  "
            f"[red]{rec['unconfirmed']}[/red] unconfirmed after 48h"
        )
        console.print(
            f"  [dim]{rec['confirm_rate']}% of applications got an acknowledgement[/dim]"
        )

        weak = [s for s in rec["by_site"] if s["rate"] < 40 and s["total"] >= 5]
        if weak:
            console.print(
                "\n  [yellow]Sources with low confirmation rates[/yellow] "
                "[dim](submissions here may be failing silently)[/dim]"
            )
            for s in weak[:5]:
                console.print(
                    f"    {s['rate']:>5.1f}%  {s['site'][:40]:<40} "
                    f"[dim]{s['confirmed']}/{s['total']}[/dim]"
                )
            console.print(
                "    [dim]Note: some employers never acknowledge. Compare "
                "sources rather than reading any single number as failure.[/dim]"
            )
    console.print()


@app.command()
def coach(
    job_url: Optional[str] = typer.Option(None, "--url", help="Generate prep for one specific job."),
    followups_only: bool = typer.Option(False, "--followups", help="Only draft follow-ups."),
    prep_only: bool = typer.Option(False, "--prep", help="Only generate interview prep."),
) -> None:
    """Generate interview prep packs and follow-up email drafts."""
    _bootstrap()

    from applypilot.config import check_tier
    check_tier(2, "interview prep and follow-ups")

    from applypilot import coach as coach_mod

    if job_url:
        pack = coach_mod.generate_prep(job_url, force=True)
        if not pack:
            console.print("[yellow]No prep generated — check the job URL exists and has a description.[/yellow]")
            raise typer.Exit(code=1)
        console.print(f"\n[bold]Interview prep — {pack['title']} @ {pack['company']}[/bold]\n")
        console.print(f"[dim]{pack['company_notes']}[/dim]\n")
        console.print("[bold cyan]Likely questions[/bold cyan]")
        console.print(pack["likely_questions"])
        console.print("\n[bold cyan]Your talking points[/bold cyan]")
        console.print(pack["talking_points"])
        console.print("\n[bold cyan]Ask them[/bold cyan]")
        console.print(pack["questions_to_ask"])
        console.print()
        return

    preps = 0 if followups_only else coach_mod.generate_pending_preps(limit=5)
    drafts = 0 if prep_only else coach_mod.draft_pending_followups(limit=10)

    console.print(
        f"\n  [bold]{preps}[/bold] interview prep pack(s) generated\n"
        f"  [bold]{drafts}[/bold] follow-up draft(s) queued "
        f"[dim](review and send from the panel — nothing is sent automatically)[/dim]\n"
    )


@app.command()
def verify() -> None:
    """Live-test every free job API and your mail connection. Run this before your first real run."""
    _bootstrap()

    console.print("\n[bold]ApplyPilot Verify — live connectivity test[/bold]\n")

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]FAIL[/red]"
    skip_mark = "[dim]skip[/dim]"
    results: list[tuple[str, str, str]] = []

    from applypilot.discovery import ats

    # Public ATS boards — well-known tokens that should always have openings
    for label, fn, token in (
        ("Greenhouse API", ats.fetch_greenhouse, "stripe"),
        ("Lever API", ats.fetch_lever, "netflix"),
        ("Ashby API", ats.fetch_ashby, "ramp"),
    ):
        try:
            jobs = fn(token)
            results.append((label, ok_mark if jobs else fail_mark,
                            f"{len(jobs)} jobs from '{token}'"))
        except Exception as e:
            results.append((label, fail_mark, f"{type(e).__name__}: {str(e)[:60]}"))

    for label, fn in (("Remotive API", ats.fetch_remotive),
                      ("RemoteOK API", ats.fetch_remoteok),
                      ("Arbeitnow API", ats.fetch_arbeitnow)):
        try:
            jobs = fn()
            results.append((label, ok_mark if jobs else fail_mark, f"{len(jobs)} jobs"))
        except Exception as e:
            results.append((label, fail_mark, f"{type(e).__name__}: {str(e)[:60]}"))

    # USAJobs (optional key)
    import os
    if os.environ.get("USAJOBS_API_KEY"):
        try:
            jobs = ats.fetch_usajobs("software engineer")
            results.append(("USAJobs API", ok_mark if jobs else fail_mark, f"{len(jobs)} jobs"))
        except Exception as e:
            results.append(("USAJobs API", fail_mark, str(e)[:60]))
    else:
        results.append(("USAJobs API", skip_mark, "set USAJOBS_API_KEY to enable"))

    # Mail
    if os.environ.get("MAIL_APP_PASSWORD"):
        from applypilot.mail import fetch_and_store
        stats = fetch_and_store(lookback_days=7, limit=25)
        if stats.get("error"):
            results.append(("Email (IMAP)", fail_mark, stats["error"][:70]))
        else:
            results.append(("Email (IMAP)", ok_mark,
                            f"{stats['fetched']} scanned, {stats['stored']} job-related"))
    else:
        results.append(("Email (IMAP)", skip_mark, "set MAIL_APP_PASSWORD to enable Inbox"))

    # Supabase bridge
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        try:
            from applypilot.sync import connect
            sb = connect()
            sb.select("jobs", "limit=1")
            results.append(("Supabase bridge", ok_mark, "connected, jobs table readable"))
        except Exception as e:
            results.append(("Supabase bridge", fail_mark, str(e)[:70]))
    else:
        results.append(("Supabase bridge", skip_mark, "set SUPABASE_URL + SUPABASE_SERVICE_KEY"))

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        console.print(f"  {check}{' ' * (col_w - len(check))}{status}  [dim]{note}[/dim]")

    failed = sum(1 for _, s, _ in results if s == fail_mark)
    console.print()
    if failed:
        console.print(f"[yellow]{failed} check(s) failed.[/yellow] "
                      "Network blocks or bad credentials are the usual cause.\n")
        raise typer.Exit(code=1)
    console.print("[green]All active checks passed.[/green]\n")


@app.command()
def sync(
    once: bool = typer.Option(False, "--once", help="Run a single sync cycle and exit (for testing)."),
) -> None:
    """Run the Mission Control bridge: mirror jobs to Supabase, execute panel commands."""
    _bootstrap()

    from applypilot.sync import main as sync_main

    console.print("\n[bold blue]ApplyPilot Sync — Mission Control bridge[/bold blue]")
    console.print("[dim]Ctrl+C to stop. The panel will show the engine as offline.[/dim]\n")

    sync_main(once=once)


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # Daily AI request usage (free-tier headroom, not money)
    from applypilot import budget as budget_mod
    b = budget_mod.summary()
    if b["unlimited"]:
        console.print("\n[dim]AI requests: unlimited (local model — no quota)[/dim]")
    else:
        pct = b["percent_used"]
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        budget_table = Table(title="\nDaily AI Requests (free tier — not billed)",
                             show_header=True, header_style="bold cyan")
        budget_table.add_column("Stage")
        budget_table.add_column("Used", justify="right")
        budget_table.add_column("Reserved/day", justify="right")

        for stage, share in budget_mod.STAGE_RESERVATIONS.items():
            used = b["by_stage"].get(stage, 0)
            budget_table.add_row(stage, str(used), str(int(b["limit"] * share)))
        budget_table.add_row(
            "[bold]total[/bold]",
            f"[{color}]{b['used']}[/{color}]",
            f"[bold]{b['limit']}[/bold]",
        )
        console.print(budget_table)
        console.print(
            f"  [{color}]{pct}% of today's free-tier requests used[/{color}] "
            f"[dim]({b['remaining']} left — resets at UTC midnight, costs nothing)[/dim]"
        )

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # Email: the address on forms must match the mailbox being scanned
    from applypilot.mail import check_email_alignment
    mail_addr = os.environ.get("MAIL_ADDRESS")
    mismatch = check_email_alignment()
    if mismatch:
        results.append(("Application email", fail_mark, mismatch))
    elif mail_addr:
        results.append(("Application email", ok_mark,
                        f"{mail_addr} — forms and inbox match"))
    else:
        results.append(("Application email", "[dim]optional[/dim]",
                        "Set MAIL_ADDRESS + MAIL_APP_PASSWORD to track confirmations"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
