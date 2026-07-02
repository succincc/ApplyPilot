"""Human-in-the-loop CAPTCHA / login handling.

DESIGN NOTE — we deliberately do NOT solve or bypass CAPTCHAs. Circumventing
a site's anti-bot controls is what gets your accounts permanently banned and
is legally fraught. Instead we *detect* a challenge, bring the visible browser
to the foreground, and wait for you to solve it, then continue. This is more
reliable than any solver (which break constantly) and keeps you on the right
side of every site's terms.
"""

from __future__ import annotations

import logging
import time

from rich.console import Console

log = logging.getLogger(__name__)
console = Console()

# Signatures of common challenge / login walls.
_CHALLENGE_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",       # Turnstile
    "iframe[title*='captcha' i]",
    "div.g-recaptcha",
    "div.h-captcha",
    "#challenge-running",                              # Cloudflare interstitial
]

_CHALLENGE_TEXT = [
    "verify you are human",
    "are you a robot",
    "confirm you are not a robot",
    "complete the captcha",
    "security check",
]


def detect_challenge(page) -> str | None:
    """Return a short label if a CAPTCHA/challenge is present, else None."""
    for sel in _CHALLENGE_SELECTORS:
        try:
            if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                return sel
        except Exception:
            continue
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    for phrase in _CHALLENGE_TEXT:
        if phrase in body:
            return phrase
    return None


def detect_login_wall(page) -> bool:
    """Heuristic: a password field visible usually means a login is required."""
    try:
        pw = page.locator("input[type='password']")
        return pw.count() > 0 and pw.first.is_visible()
    except Exception:
        return False


def wait_for_human(page, timeout_s: int, reason: str) -> bool:
    """Pause and let the user resolve a challenge/login in the visible browser.

    Returns True if the challenge cleared before the timeout, False otherwise.
    In headless mode there's no human available, so we return False immediately.
    """
    console.print(
        f"\n[bold yellow]⚠  Human needed:[/bold yellow] {reason}.\n"
        f"   Solve it in the browser window. Waiting up to {timeout_s}s...\n"
    )
    try:
        page.bring_to_front()
    except Exception:
        pass

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if detect_challenge(page) is None and not detect_login_wall(page):
            console.print("[green]✓ Challenge cleared — continuing.[/green]")
            return True
        time.sleep(2)
    console.print("[red]✗ Timed out waiting for human. Skipping this job.[/red]")
    return False
