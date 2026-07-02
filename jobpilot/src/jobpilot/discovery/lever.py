"""Lever public postings API.

Endpoint (no auth): https://api.lever.co/v0/postings/{company}?mode=json
`company` is the slug in jobs.lever.co/<company>.
"""

from __future__ import annotations

from typing import Any

from jobpilot.discovery.base import client, guess_remote, parse_salary
from jobpilot.models import Job, Preferences

_API = "https://api.lever.co/v0/postings/{company}"

# Lever commitment strings -> our employment_type vocabulary.
_TYPE_MAP = {
    "full-time": "full_time",
    "full time": "full_time",
    "part-time": "part_time",
    "contract": "contract",
    "internship": "internship",
    "intern": "internship",
}


def fetch(prefs: Preferences, cfg: dict[str, Any]) -> list[Job]:
    companies = cfg.get("companies", []) or []
    out: list[Job] = []
    with client() as http:
        for slug in companies:
            try:
                resp = http.get(_API.format(company=slug), params={"mode": "json"})
                resp.raise_for_status()
                postings = resp.json()
            except Exception:
                continue
            company = slug.replace("-", " ").title()
            for p in postings:
                cats = p.get("categories", {}) or {}
                loc = cats.get("location", "") or ""
                commitment = (cats.get("commitment", "") or "").lower()
                desc = p.get("descriptionPlain") or p.get("description", "")
                smin, smax = parse_salary(desc)
                out.append(
                    Job(
                        source="lever",
                        ats="lever",
                        company=company,
                        title=p.get("text", ""),
                        url=p.get("hostedUrl") or p.get("applyUrl", ""),
                        location=loc,
                        remote=guess_remote(loc, desc[:500]),
                        description=desc,
                        employment_type=_TYPE_MAP.get(commitment, ""),
                        salary_min=smin,
                        salary_max=smax,
                        posted_at=_ms_to_iso(p.get("createdAt")),
                        external_id=str(p.get("id", "")),
                    )
                )
    return out


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None
