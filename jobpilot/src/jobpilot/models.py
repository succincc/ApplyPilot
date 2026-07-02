"""Core data structures shared across JobPilot.

Plain dataclasses, no framework magic. A `Job` is the unit that flows through
the pipeline: discovered -> matched -> queued -> applied.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


def _slug(*parts: str) -> str:
    raw = "||".join(p.strip().lower() for p in parts if p)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class Job:
    """A single job posting from any discovery source."""

    source: str                       # e.g. "greenhouse", "lever", "remotive"
    company: str
    title: str
    url: str                          # canonical posting / apply URL
    location: str = ""
    remote: bool = False
    description: str = ""
    employment_type: str = ""         # full_time, part_time, contract, internship
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    currency: str = "USD"
    posted_at: Optional[str] = None   # ISO date string if known
    ats: str = ""                     # detected ATS for the apply step
    external_id: str = ""             # source-native id, if any

    # Populated later in the pipeline
    job_id: str = ""
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.job_id:
            # Stable id from company+title+url so re-discovery dedupes cleanly.
            self.job_id = _slug(self.company, self.title, self.url)
        if not self.ats and self.source in ("greenhouse", "lever", "ashby"):
            self.ats = self.source

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["score_reasons"] = "\n".join(self.score_reasons)
        d["remote"] = 1 if self.remote else 0
        return d


@dataclass
class Preferences:
    """Parsed user preferences from config.yaml (see config.py)."""

    keywords: list[str]
    exclude_keywords: list[str]
    preferred_locations: list[str]
    location_anchor: str
    radius_miles: int
    allow_remote: bool
    allow_unspecified: bool
    min_salary: Optional[int]
    target_salary: Optional[int]
    currency: str
    drop_if_no_salary: bool
    employment_types: list[str]
    max_age_days: int
    min_score: int
    auto_approve_score: int
    auto_submit: bool
    headless: bool
    max_per_run: int
    delay_between_seconds: int
    human_solve_timeout: int
    sources: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
