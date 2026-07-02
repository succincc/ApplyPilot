"""Apply orchestration.

Pulls queued jobs from the DB, drives the browser through each application,
and records the outcome. Safety-first: it fills everything but only clicks
Submit when (a) auto_submit is enabled, (b) the job scored at/above
auto_approve_score, and (c) the form filler is confident (nothing flagged).
Otherwise the job is parked as `needs_review` with the browser having done
all the tedious typing for you.
"""

from __future__ import annotations

import logging
import random
import time

from rich.console import Console

from jobpilot import storage
from jobpilot.answers import AnswerEngine
from jobpilot.apply import captcha
from jobpilot.apply.browser import browser_page
from jobpilot.apply.forms import FormFiller
from jobpilot.config import load_answers, load_preferences, load_profile
from jobpilot.models import Job, Preferences

log = logging.getLogger(__name__)
console = Console()

_APPLY_BUTTON = [
    "button:has-text('Apply')",
    "a:has-text('Apply')",
    "button:has-text('Apply for this job')",
    "a:has-text('Apply for this job')",
]

_SUBMIT_BUTTON = [
    "button:has-text('Submit application')",
    "button:has-text('Submit')",
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Send application')",
]

_SUCCESS_TEXT = [
    "thank you for applying",
    "application submitted",
    "your application has been submitted",
    "we received your application",
    "thanks for applying",
]


def _row_to_job(row) -> Job:
    return Job(
        source=row["source"], company=row["company"], title=row["title"],
        url=row["url"], location=row["location"] or "", remote=bool(row["remote"]),
        description=row["description"] or "", ats=row["ats"] or "",
        salary_min=row["salary_min"], salary_max=row["salary_max"],
        job_id=row["job_id"], score=row["score"] or 0,
    )


def _click_first(page, selectors: list[str], timeout: int = 4000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _looks_submitted(page) -> bool:
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return any(t in body for t in _SUCCESS_TEXT)


def apply_to_job(page, job: Job, prefs: Preferences, filler: FormFiller,
                 dry_run: bool) -> tuple[str, str | None]:
    """Drive one application. Returns (status, error)."""
    console.print(f"\n[cyan]→ {job.title}[/cyan] @ {job.company}  (score {job.score})")
    try:
        page.goto(job.url, wait_until="domcontentloaded")
    except Exception as exc:
        return "failed", f"navigation error: {exc}"

    # Some ATS pages need an "Apply" click to reveal the form.
    if page.locator("input[type='file']").count() == 0:
        if _click_first(page, _APPLY_BUTTON):
            page.wait_for_timeout(1500)

    # Challenge / login handling — human in the loop, never bypassed.
    if captcha.detect_challenge(page) or captcha.detect_login_wall(page):
        reason = "CAPTCHA" if captcha.detect_challenge(page) else "login required"
        if not captcha.wait_for_human(page, prefs.human_solve_timeout, reason):
            return "skipped", f"{reason} not resolved"

    report = filler.fill(page)
    console.print(
        f"   filled={report.filled} answered={report.answered} "
        f"uploaded={report.uploaded} flagged={len(report.flagged)}"
    )
    for q in report.flagged:
        console.print(f"     [yellow]? needs review:[/yellow] {q}")

    # Decide whether to submit.
    confident = not report.needs_review
    eligible = prefs.auto_submit and job.score >= prefs.auto_approve_score
    if dry_run:
        return "needs_review", "dry-run: form filled, not submitted"
    if not (confident and eligible):
        why = []
        if not confident:
            why.append(f"{len(report.flagged)} unanswered question(s)")
        if not prefs.auto_submit:
            why.append("auto_submit is off")
        if job.score < prefs.auto_approve_score:
            why.append(f"score {job.score} < auto_approve {prefs.auto_approve_score}")
        console.print(f"   [yellow]parked for manual review:[/yellow] {', '.join(why)}")
        return "needs_review", "; ".join(why)

    # Submit.
    if not _click_first(page, _SUBMIT_BUTTON):
        return "needs_review", "submit button not found"
    page.wait_for_timeout(3000)
    if captcha.detect_challenge(page):
        if not captcha.wait_for_human(page, prefs.human_solve_timeout, "CAPTCHA on submit"):
            return "skipped", "captcha on submit"
        page.wait_for_timeout(2000)
    if _looks_submitted(page):
        return "applied", None
    # We clicked submit but couldn't confirm — flag rather than lie.
    return "needs_review", "submitted click done but success not confirmed"


def run(dry_run: bool = False, limit: int | None = None) -> dict:
    """Main apply loop over the queued jobs."""
    prefs = load_preferences()
    profile = load_profile()
    engine = AnswerEngine(load_answers(), profile)

    max_count = limit if limit is not None else prefs.max_per_run
    rows = storage.claim_for_apply(max_count, prefs.min_score)
    if not rows:
        console.print("[yellow]No queued jobs. Run `jobpilot match` first.[/yellow]")
        return {"applied": 0, "review": 0, "failed": 0, "skipped": 0}

    console.print(
        f"[bold]Applying to up to {len(rows)} job(s)[/bold] "
        f"(auto_submit={prefs.auto_submit}, headless={prefs.headless}, dry_run={dry_run})"
    )

    counts = {"applied": 0, "review": 0, "failed": 0, "skipped": 0}
    with browser_page(headless=prefs.headless) as page:
        for i, row in enumerate(rows):
            job = _row_to_job(row)
            filler = FormFiller(profile, engine, job)
            try:
                status, err = apply_to_job(page, job, prefs, filler, dry_run)
            except Exception as exc:
                log.exception("apply crashed for %s", job.url)
                status, err = "failed", str(exc)[:200]

            storage.mark_apply_result(job.job_id, status, err)
            bucket = {"applied": "applied", "needs_review": "review",
                      "failed": "failed", "skipped": "skipped"}.get(status, "failed")
            counts[bucket] += 1

            if i < len(rows) - 1:
                base = prefs.delay_between_seconds
                delay = max(1, base + random.uniform(-0.3 * base, 0.3 * base))
                time.sleep(delay)

    console.print(
        f"\n[bold]Done:[/bold] {counts['applied']} applied, "
        f"{counts['review']} need review, {counts['failed']} failed, "
        f"{counts['skipped']} skipped."
    )
    return counts
