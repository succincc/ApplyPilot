"""Greenhouse public job board API.

Docs: https://developers.greenhouse.io/job-board.html
Endpoint (no auth): https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

`token` is the company's board slug (the bit in boards.greenhouse.io/<token>).
"""

from __future__ import annotations

import re
from typing import Any

from jobpilot.discovery.base import client, guess_remote, parse_salary
from jobpilot.models import Job, Preferences

_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
_TAG = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _TAG.sub(" ", html or "").replace("&nbsp;", " ").strip()


def fetch(prefs: Preferences, cfg: dict[str, Any]) -> list[Job]:
    companies = cfg.get("companies", []) or []
    out: list[Job] = []
    with client() as http:
        for token in companies:
            try:
                resp = http.get(_API.format(token=token), params={"content": "true"})
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            company = token.replace("-", " ").title()
            for j in data.get("jobs", []):
                loc = (j.get("location") or {}).get("name", "")
                desc = _strip_html(j.get("content", ""))
                smin, smax = parse_salary(desc)
                out.append(
                    Job(
                        source="greenhouse",
                        ats="greenhouse",
                        company=company,
                        title=j.get("title", ""),
                        url=j.get("absolute_url", ""),
                        location=loc,
                        remote=guess_remote(loc, desc[:500]),
                        description=desc,
                        salary_min=smin,
                        salary_max=smax,
                        posted_at=j.get("updated_at"),
                        external_id=str(j.get("id", "")),
                    )
                )
    return out
