"""Shared helpers for discovery sources: HTTP client and salary parsing."""

from __future__ import annotations

import re
from typing import Optional

import httpx

_USER_AGENT = "JobPilot/0.1 (+https://github.com/; personal job search)"

# Matches "$120,000", "120k", "$120K - $150K", "USD 130000", etc.
_SALARY = re.compile(
    r"(?:\$|usd|eur|gbp)?\s*(\d{2,3}(?:[,\.]\d{3})?)\s*(k)?",
    re.IGNORECASE,
)


def client(timeout: float = 30.0) -> httpx.Client:
    """An httpx client that honors HTTPS_PROXY / HTTP_PROXY from the env."""
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        trust_env=True,
    )


def parse_salary(text: str) -> tuple[Optional[int], Optional[int]]:
    """Best-effort salary range extraction from free text.

    Returns (min, max) annual figures, or (None, None) if nothing plausible.
    Only accepts figures that look like annual salaries (>= $10k after k-expansion).
    """
    if not text:
        return None, None
    values: list[int] = []
    for m in _SALARY.finditer(text):
        raw = m.group(1).replace(",", "").replace(".", "")
        try:
            n = int(raw)
        except ValueError:
            continue
        if m.group(2):  # trailing "k"
            n *= 1000
        # Reject hourly/tiny numbers and absurd figures.
        if 10_000 <= n <= 1_000_000:
            values.append(n)
    if not values:
        return None, None
    values.sort()
    if len(values) == 1:
        return values[0], values[0]
    return values[0], values[-1]


def guess_remote(location: str, text: str = "") -> bool:
    blob = f"{location} {text}".lower()
    return "remote" in blob or "work from home" in blob or "anywhere" in blob
