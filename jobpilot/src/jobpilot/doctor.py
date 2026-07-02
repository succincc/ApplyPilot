"""Preflight checks: verify the machine and config are ready to run.

`jobpilot doctor` runs every check and prints a pass/warn/fail report so
problems surface before a run, not in the middle of one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from jobpilot.config import (
    APP_DIR, load_answers, load_preferences, load_profile, using_example_config,
)

console = Console()

_PASS = "[green]ok[/green]    "
_WARN = "[yellow]warn[/yellow]  "
_FAIL = "[red]fail[/red]  "

# Placeholder values from profile.example.yaml that mean "not edited yet".
_PLACEHOLDERS = {"jane.doe@example.com", "Jane Doe", "your.email@example.com",
                 "YOUR_LEGAL_NAME", "555-123-4567", "+1 555 123 4567"}

# One representative endpoint per source to probe connectivity.
_PROBE_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{first}/jobs",
    "lever": "https://api.lever.co/v0/postings/{first}?mode=json&limit=1",
    "remotive": "https://remotive.com/api/remote-jobs?limit=1",
    "usajobs": "https://data.usajobs.gov/api/search?ResultsPerPage=1",
}


def _check(label: str, ok: bool, detail: str = "", warn: bool = False) -> bool:
    tag = _PASS if ok else (_WARN if warn else _FAIL)
    console.print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    return ok


def run_doctor(skip_network: bool = False, skip_browser: bool = False) -> int:
    """Run all checks. Returns the number of hard failures."""
    failures = 0
    console.print("[bold]JobPilot preflight[/bold]\n")

    # 1. Python version
    py_ok = sys.version_info >= (3, 10)
    if not _check("Python >= 3.10", py_ok, f"found {sys.version.split()[0]}"):
        failures += 1

    # 2. Config files
    example = using_example_config()
    _check("config.yaml", not example,
           "using bundled example — run `jobpilot init` and edit it" if example else "found",
           warn=True)

    # 3. Profile sanity
    try:
        profile = load_profile()
        personal = profile.get("personal", {})
        email = personal.get("email", "")
        name = personal.get("full_name", "")
        unedited = email in _PLACEHOLDERS or name in _PLACEHOLDERS or not email
        if not _check("profile.yaml personal info", not unedited,
                      f"email={email or '(empty)'} name={name or '(empty)'}"
                      + (" — still placeholder values, edit profile.yaml" if unedited else "")):
            failures += 1

        # 4. Resume file
        resume = str(profile.get("documents", {}).get("resume", "") or "")
        resume_ok = bool(resume) and Path(resume).is_file()
        if not _check("resume file", resume_ok,
                      resume if resume_ok else f"not found: {resume or '(not set)'}"):
            failures += 1
    except SystemExit as exc:
        _check("profile.yaml parses", False, str(exc))
        failures += 2

    # 5. answers.yaml parses and has rules
    try:
        answers = load_answers()
        n_rules = len(answers.get("rules", []) or [])
        if not _check("answers.yaml rules", n_rules > 0, f"{n_rules} rule(s)"):
            failures += 1
    except SystemExit as exc:
        _check("answers.yaml parses", False, str(exc))
        failures += 1

    # 6. Preferences parse + at least one source enabled
    prefs = None
    try:
        prefs = load_preferences()
        enabled = [k for k, v in prefs.sources.items() if (v or {}).get("enabled")]
        if not _check("discovery sources enabled", bool(enabled),
                      ", ".join(enabled) if enabled else "none — enable some in config.yaml"):
            failures += 1
    except SystemExit as exc:
        _check("config.yaml parses", False, str(exc))
        failures += 1

    # 7. Data dir writable
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        probe = APP_DIR / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _check("data directory writable", True, str(APP_DIR))
    except OSError as exc:
        _check("data directory writable", False, str(exc))
        failures += 1

    # 8. Browser
    if skip_browser:
        _check("browser (skipped)", True, "--skip-browser", warn=True)
    else:
        try:
            from playwright.sync_api import sync_playwright
            kwargs = {}
            executable = os.environ.get("JOBPILOT_BROWSER_EXECUTABLE")
            if executable:
                kwargs["executable_path"] = executable
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **kwargs)
                browser.close()
            _check("Chromium launches", True,
                   executable or "playwright-managed")
        except Exception as exc:
            _check("Chromium launches", False,
                   f"{str(exc).splitlines()[0][:90]} — run `playwright install chromium`")
            failures += 1

    # 9. Network reachability for enabled sources
    if skip_network or prefs is None:
        _check("network (skipped)", True, warn=True)
    else:
        import httpx
        for name, cfg in prefs.sources.items():
            cfg = cfg or {}
            if not cfg.get("enabled") or name not in _PROBE_URLS:
                continue
            first = (cfg.get("companies") or ["example"])[0]
            url = _PROBE_URLS[name].format(first=first)
            try:
                resp = httpx.get(url, timeout=10, trust_env=True, follow_redirects=True,
                                 headers={"User-Agent": "JobPilot/0.1", "Accept": "application/json"})
                ok = resp.status_code < 500
                if not _check(f"network: {name}", ok, f"HTTP {resp.status_code}"):
                    failures += 1
            except Exception as exc:
                _check(f"network: {name}", False, f"{type(exc).__name__}: {str(exc)[:80]}")
                failures += 1

    console.print()
    if failures:
        console.print(f"[red bold]{failures} problem(s) to fix before applying.[/red bold]")
    else:
        console.print("[green bold]All checks passed — you're ready: jobpilot run --apply[/green bold]")
    return failures
