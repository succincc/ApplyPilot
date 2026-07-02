"""Job discovery from public APIs and feeds.

Each source is a documented, public endpoint — no scraping, no ToS
violations, no account bans. Add new sources by implementing `fetch()` and
registering them in SOURCES.
"""

from __future__ import annotations

import logging

from jobpilot.models import Job, Preferences
from jobpilot.discovery import greenhouse, lever, remotive, usajobs

log = logging.getLogger(__name__)

# name -> module-level fetch(prefs, source_cfg) -> list[Job]
SOURCES = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "remotive": remotive.fetch,
    "usajobs": usajobs.fetch,
}


def run_discovery(prefs: Preferences) -> list[Job]:
    """Poll every enabled source and return a deduped list of jobs."""
    seen: dict[str, Job] = {}
    for name, fetch in SOURCES.items():
        cfg = prefs.sources.get(name, {}) or {}
        if not cfg.get("enabled"):
            continue
        try:
            jobs = fetch(prefs, cfg)
            log.info("discovery[%s]: %d jobs", name, len(jobs))
            for j in jobs:
                seen.setdefault(j.job_id, j)
        except Exception as exc:  # one bad source shouldn't kill the run
            log.error("discovery[%s] failed: %s", name, exc)
    return list(seen.values())
