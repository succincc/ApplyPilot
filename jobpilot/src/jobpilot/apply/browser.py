"""Playwright browser management for JobPilot.

Uses a *persistent* browser context stored under ~/.jobpilot/browser-profile
so that any logins you complete (LinkedIn, a company SSO) survive between
runs — you log in once, by hand, and the bot reuses the session.

Runs on Windows natively (Chromium via Playwright). Visible by default so
you can take over for CAPTCHAs and logins.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator

from jobpilot.config import BROWSER_PROFILE_DIR, ensure_dirs

log = logging.getLogger(__name__)

# Point this at an existing Chromium/Chrome binary to skip
# `playwright install chromium` (e.g. a system Chrome install).
_EXECUTABLE_ENV = "JOBPILOT_BROWSER_EXECUTABLE"


@contextmanager
def browser_page(headless: bool = False, slow_mo_ms: int = 50) -> Iterator["Page"]:  # noqa: F821
    """Yield a Playwright Page backed by a persistent context.

    Imports Playwright lazily so the rest of JobPilot (discovery, matching)
    works without the browser installed.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        ) from exc

    ensure_dirs()
    launch_kwargs: dict = {
        "user_data_dir": str(BROWSER_PROFILE_DIR),
        "headless": headless,
        "slow_mo": slow_mo_ms,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    executable = os.environ.get(_EXECUTABLE_ENV)
    if executable:
        launch_kwargs["executable_path"] = executable
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20_000)
        try:
            yield page
        finally:
            context.close()
