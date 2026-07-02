"""Remotive public jobs API — a large aggregator of remote roles.

Docs: https://remotive.com/api/remote-jobs
No auth. Supports a `search` param; we query once per keyword and merge.
"""

from __future__ import annotations

import re
from typing import Any

from jobpilot.discovery.base import client, parse_salary
from jobpilot.models import Job, Preferences

_API = "https://remotive.com/api/remote-jobs"
_TAG = re.compile(r"<[^>]+>")

_TYPE_MAP = {
    "full_time": "full_time",
    "part_time": "part_time",
    "contract": "contract",
    "freelance": "contract",
    "internship": "internship",
}


def fetch(prefs: Preferences, cfg: dict[str, Any]) -> list[Job]:
    queries = prefs.keywords or [""]
    limit = int(cfg.get("limit_per_keyword", 50))
    out: list[Job] = []
    with client() as http:
        for q in queries:
            params: dict[str, Any] = {"limit": limit}
            if q:
                params["search"] = q
            try:
                resp = http.get(_API, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            for j in data.get("jobs", []):
                desc = _TAG.sub(" ", j.get("description", "")).strip()
                salary_txt = j.get("salary", "") or ""
                smin, smax = parse_salary(salary_txt or desc[:400])
                out.append(
                    Job(
                        source="remotive",
                        ats="external",
                        company=j.get("company_name", ""),
                        title=j.get("title", ""),
                        url=j.get("url", ""),
                        location=j.get("candidate_required_location", "") or "Remote",
                        remote=True,
                        description=desc,
                        employment_type=_TYPE_MAP.get(j.get("job_type", ""), ""),
                        salary_min=smin,
                        salary_max=smax,
                        posted_at=j.get("publication_date"),
                        external_id=str(j.get("id", "")),
                    )
                )
    return out
