"""Deterministic, no-AI job matching.

Given a Job and the user's Preferences, decide whether the job passes hard
filters and assign a 0-100 fit score. Every decision has a plain-English
reason attached so the user can see *why* a job scored the way it did.

There is no LLM here. Scoring is keyword overlap + location + salary +
recency, all transparent and tweakable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from jobpilot.models import Job, Preferences

_WORD = re.compile(r"[a-z0-9+#.]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def _contains_any(haystack: str, needles: list[str]) -> Optional[str]:
    low = (haystack or "").lower()
    for n in needles:
        if n and n.lower() in low:
            return n
    return None


def _parse_age_days(posted_at: Optional[str]) -> Optional[int]:
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except (ValueError, TypeError):
        return None


def _location_ok(job: Job, prefs: Preferences) -> tuple[bool, str]:
    if job.remote and prefs.allow_remote:
        return True, "remote job accepted"
    if not job.location:
        if prefs.allow_unspecified:
            return True, "location unspecified (allowed)"
        return False, "location unspecified (not allowed)"
    match = _contains_any(job.location, prefs.preferred_locations)
    if match:
        return True, f"location matches '{match}'"
    # "Remote" appearing in the location string also counts if allowed.
    if prefs.allow_remote and "remote" in job.location.lower():
        return True, "remote in location string"
    return False, f"location '{job.location}' not in preferred list"


def _salary_ok(job: Job, prefs: Preferences) -> tuple[bool, str]:
    if job.salary_max is None and job.salary_min is None:
        if prefs.drop_if_no_salary:
            return False, "no salary stated (drop_if_no_salary=true)"
        return True, "no salary stated (kept)"
    if prefs.min_salary is None:
        return True, "no salary floor configured"
    top = job.salary_max or job.salary_min or 0
    if top >= prefs.min_salary:
        return True, f"salary max {top} >= floor {prefs.min_salary}"
    return False, f"salary max {top} < floor {prefs.min_salary}"


def evaluate(job: Job, prefs: Preferences) -> tuple[int, list[str], bool]:
    """Return (score 0-100, reasons, passed_hard_filters).

    If a hard filter fails, `passed` is False and score is 0.
    """
    reasons: list[str] = []
    hay = f"{job.title}\n{job.description}"

    # ---- Hard filters ------------------------------------------------------
    excl = _contains_any(hay, prefs.exclude_keywords)
    if excl:
        return 0, [f"excluded by keyword '{excl}'"], False

    if prefs.employment_types and job.employment_type:
        if job.employment_type not in prefs.employment_types:
            return 0, [f"employment type '{job.employment_type}' not accepted"], False

    loc_ok, loc_reason = _location_ok(job, prefs)
    if not loc_ok:
        return 0, [loc_reason], False

    sal_ok, sal_reason = _salary_ok(job, prefs)
    if not sal_ok:
        return 0, [sal_reason], False

    age = _parse_age_days(job.posted_at)
    if prefs.max_age_days and age is not None and age > prefs.max_age_days:
        return 0, [f"posted {age}d ago > max {prefs.max_age_days}d"], False

    # ---- Soft scoring ------------------------------------------------------
    score = 0.0

    # Keyword overlap (up to 55). Title hits count double.
    kw = [k.lower() for k in prefs.keywords]
    title_low = job.title.lower()
    desc_tokens = _tokens(job.description)
    matched = []
    if kw:
        hits = 0.0
        for k in kw:
            in_title = k in title_low
            in_desc = all(t in desc_tokens for t in _WORD.findall(k)) or k in job.description.lower()
            if in_title:
                hits += 2.0
                matched.append(f"{k} (title)")
            elif in_desc:
                hits += 1.0
                matched.append(k)
        # Normalize against the best case (every keyword in the title).
        kw_score = min(1.0, hits / (2.0 * len(kw))) * 55
        score += kw_score
        reasons.append(f"keywords {kw_score:.0f}/55 — matched: {', '.join(matched) or 'none'}")
    else:
        score += 30
        reasons.append("no keywords configured (+30 baseline)")

    # Location (up to 20)
    score += 20
    reasons.append(f"location +20 — {loc_reason}")

    # Salary (up to 15)
    if job.salary_max and prefs.target_salary:
        ratio = min(1.0, job.salary_max / prefs.target_salary)
        sal_pts = ratio * 15
        score += sal_pts
        reasons.append(f"salary +{sal_pts:.0f}/15 — {sal_reason}")
    else:
        score += 8
        reasons.append(f"salary +8 (no target comparison) — {sal_reason}")

    # Recency (up to 10)
    if age is not None:
        rec = max(0.0, 1.0 - age / max(1, prefs.max_age_days or 30)) * 10
        score += rec
        reasons.append(f"recency +{rec:.0f}/10 — posted {age}d ago")
    else:
        score += 5
        reasons.append("recency +5 (posting date unknown)")

    final = int(round(min(100.0, score)))
    return final, reasons, True
