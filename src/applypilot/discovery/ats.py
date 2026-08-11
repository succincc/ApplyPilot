"""Direct-from-source job discovery via free, public ATS and government APIs.

Scraping aggregators finds listings; this module finds jobs at the source.
That matters twice over:

  1. No scraping, no rate-limit games, no bot detection — these are public
     JSON endpoints intended to be read by machines.
  2. Jobs discovered here point straight at the employer's own application
     form (Greenhouse, Lever, Ashby), which is the highest auto-apply success
     path there is. An Indeed listing usually redirects through one or more
     hops before landing somewhere the agent can actually submit.

Every source here is free and most need no key at all:

  greenhouse  boards-api.greenhouse.io   no key
  lever       api.lever.co               no key
  ashby       api.ashbyhq.com            no key
  remotive    remotive.com/api           no key
  remoteok    remoteok.com/api           no key
  arbeitnow   arbeitnow.com/api          no key
  usajobs     data.usajobs.gov           free key (USAJOBS_API_KEY + USAJOBS_EMAIL)

Company board tokens come from `target_companies` in the control panel, or
from config/employers.yaml. The token is the slug in the board URL:
job-boards.greenhouse.io/**stripe** -> token "stripe".
"""

import logging
import os
import re
from datetime import datetime, timezone

import httpx

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "ApplyPilot/0.4 (+https://github.com/Pickle-Pixel/ApplyPilot)"

REMOTE_SOURCES = ("remotive", "remoteok", "arbeitnow")


def _clean_html(text: str | None) -> str | None:
    """Strip tags and collapse whitespace from an HTML job description."""
    if not text:
        return None
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip() or None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                        follow_redirects=True)


# ---------------------------------------------------------------------------
# Per-ATS fetchers — each returns a list of normalized job dicts
# ---------------------------------------------------------------------------

def fetch_greenhouse(token: str, company: str | None = None) -> list[dict]:
    """Greenhouse public board API. content=true includes full descriptions."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    with _client() as c:
        r = c.get(url)
        if r.status_code == 404:
            log.warning("Greenhouse board not found: %s", token)
            return []
        r.raise_for_status()
        data = r.json()

    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name")
        jobs.append({
            "url": j.get("absolute_url"),
            "title": j.get("title"),
            "company": company or token,
            "location": location,
            "salary": None,
            "description": _clean_html(j.get("content")),
            "site": f"greenhouse:{company or token}",
            "application_url": j.get("absolute_url"),
        })
    return jobs


def fetch_lever(token: str, company: str | None = None) -> list[dict]:
    """Lever public postings API."""
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    with _client() as c:
        r = c.get(url)
        if r.status_code == 404:
            log.warning("Lever board not found: %s", token)
            return []
        r.raise_for_status()
        data = r.json()

    jobs = []
    for j in data:
        categories = j.get("categories") or {}
        jobs.append({
            "url": j.get("hostedUrl"),
            "title": j.get("text"),
            "company": company or token,
            "location": categories.get("location"),
            "salary": categories.get("commitment"),
            "description": _clean_html(j.get("descriptionPlain") or j.get("description")),
            "site": f"lever:{company or token}",
            "application_url": j.get("applyUrl") or j.get("hostedUrl"),
        })
    return jobs


def fetch_ashby(token: str, company: str | None = None) -> list[dict]:
    """Ashby public job board API."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    with _client() as c:
        r = c.get(url)
        if r.status_code == 404:
            log.warning("Ashby board not found: %s", token)
            return []
        r.raise_for_status()
        data = r.json()

    jobs = []
    for j in data.get("jobs", []):
        comp = j.get("compensation") or {}
        summary = comp.get("compensationTierSummary") if isinstance(comp, dict) else None
        jobs.append({
            "url": j.get("jobUrl"),
            "title": j.get("title"),
            "company": company or token,
            "location": j.get("location"),
            "salary": summary,
            "description": _clean_html(j.get("descriptionPlain") or j.get("descriptionHtml")),
            "site": f"ashby:{company or token}",
            "application_url": j.get("applyUrl") or j.get("jobUrl"),
        })
    return jobs


def fetch_usajobs(query: str, location: str | None = None,
                  results: int = 100) -> list[dict]:
    """USAJobs API — every US federal opening. Free key, real careers.

    Requires USAJOBS_API_KEY and USAJOBS_EMAIL in the environment
    (register at https://developer.usajobs.gov/APIRequest).
    """
    api_key = os.environ.get("USAJOBS_API_KEY")
    email = os.environ.get("USAJOBS_EMAIL")
    if not api_key or not email:
        log.info("USAJobs skipped — set USAJOBS_API_KEY and USAJOBS_EMAIL to enable")
        return []

    params = {"Keyword": query, "ResultsPerPage": min(results, 500)}
    if location and location.lower() not in ("remote", "anywhere"):
        params["LocationName"] = location

    with _client() as c:
        r = c.get(
            "https://data.usajobs.gov/api/search",
            params=params,
            headers={"Host": "data.usajobs.gov", "User-Agent": email,
                     "Authorization-Key": api_key},
        )
        r.raise_for_status()
        data = r.json()

    jobs = []
    items = (data.get("SearchResult") or {}).get("SearchResultItems", []) or []
    for item in items:
        d = item.get("MatchedObjectDescriptor") or {}
        pay = (d.get("PositionRemuneration") or [{}])[0]
        salary = None
        if pay.get("MinimumRange"):
            try:
                lo = int(float(pay["MinimumRange"]))
                hi = int(float(pay.get("MaximumRange", pay["MinimumRange"])))
                salary = f"${lo:,}-${hi:,}"
            except (ValueError, TypeError):
                salary = None

        locations = d.get("PositionLocation") or []
        loc_name = locations[0].get("LocationName") if locations else None
        summary = (d.get("UserArea") or {}).get("Details", {}).get("JobSummary")

        jobs.append({
            "url": d.get("PositionURI"),
            "title": d.get("PositionTitle"),
            "company": d.get("OrganizationName"),
            "location": loc_name,
            "salary": salary,
            "description": _clean_html(summary or d.get("QualificationSummary")),
            "site": "usajobs",
            "application_url": (d.get("ApplyURI") or [d.get("PositionURI")])[0]
            if d.get("ApplyURI") else d.get("PositionURI"),
        })
    return jobs


def fetch_remotive(query: str | None = None) -> list[dict]:
    """Remotive public remote-jobs feed."""
    url = "https://remotive.com/api/remote-jobs"
    params = {"limit": 200}
    if query:
        params["search"] = query
    with _client() as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    return [{
        "url": j.get("url"),
        "title": j.get("title"),
        "company": j.get("company_name"),
        "location": j.get("candidate_required_location") or "Remote",
        "salary": j.get("salary") or None,
        "description": _clean_html(j.get("description")),
        "site": "remotive",
        "application_url": j.get("url"),
    } for j in data.get("jobs", [])]


def fetch_remoteok() -> list[dict]:
    """RemoteOK public feed. First element is metadata, not a job."""
    with _client() as c:
        r = c.get("https://remoteok.com/api")
        r.raise_for_status()
        data = r.json()

    jobs = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        salary = None
        if j.get("salary_min"):
            try:
                salary = f"${int(j['salary_min']):,}-${int(j.get('salary_max', j['salary_min'])):,}"
            except (ValueError, TypeError):
                salary = None
        jobs.append({
            "url": j.get("url") or j.get("apply_url"),
            "title": j.get("position"),
            "company": j.get("company"),
            "location": j.get("location") or "Remote",
            "salary": salary,
            "description": _clean_html(j.get("description")),
            "site": "remoteok",
            "application_url": j.get("apply_url") or j.get("url"),
        })
    return jobs


def fetch_arbeitnow() -> list[dict]:
    """Arbeitnow free job board API."""
    with _client() as c:
        r = c.get("https://www.arbeitnow.com/api/job-board-api")
        r.raise_for_status()
        data = r.json()

    return [{
        "url": j.get("url"),
        "title": j.get("title"),
        "company": j.get("company_name"),
        "location": j.get("location") or ("Remote" if j.get("remote") else None),
        "salary": None,
        "description": _clean_html(j.get("description")),
        "site": "arbeitnow",
        "application_url": j.get("url"),
    } for j in data.get("data", [])]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store(jobs: list[dict], filters: dict) -> tuple[int, int, int]:
    """Filter and insert jobs. Returns (new, duplicates, filtered)."""
    from applypilot.filters import job_passes

    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    new = dupes = filtered = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue

        passed, _reason = job_passes(job, filters)
        if not passed:
            filtered += 1
            continue

        description = job.get("description")
        # These APIs return full descriptions inline, so enrichment can be
        # skipped entirely — mark them already-enriched.
        full_description = description if description and len(description) > 200 else None
        detail_scraped_at = now if full_description else None

        try:
            conn.execute("""
                INSERT INTO jobs (url, title, salary, description, location, site,
                                  strategy, discovered_at, full_description,
                                  application_url, detail_scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ats_api', ?, ?, ?, ?)
            """, (url, job.get("title"), job.get("salary"), description,
                  job.get("location"), job.get("site"), now,
                  full_description, job.get("application_url"), detail_scraped_at))
            new += 1
        except Exception:
            dupes += 1

    conn.commit()
    return new, dupes, filtered


def load_target_companies() -> list[dict]:
    """Company board registry from ~/.applypilot/target_companies.yaml.

    The sync daemon writes this file from the control panel's
    target_companies table, so companies added in the panel appear here.
    """
    import yaml
    path = config.APP_DIR / "target_companies.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        log.exception("target_companies.yaml is malformed")
        return []
    return [c for c in (data.get("companies") or []) if c.get("enabled", True)]


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def run_discovery(search_cfg: dict | None = None) -> dict:
    """Discover jobs from every enabled free API source.

    Reads `extra_sources` from the search config to decide which of
    ats_registry / usajobs / remote_boards to run.
    """
    from applypilot.filters import load_filter_config

    if search_cfg is None:
        search_cfg = config.load_search_config()

    init_db()
    filters = load_filter_config(search_cfg)
    sources = set(search_cfg.get("extra_sources") or [])

    totals = {"new": 0, "existing": 0, "filtered": 0, "errors": 0, "by_source": {}}

    def _record(label: str, jobs: list[dict]) -> None:
        new, dupes, filt = _store(jobs, filters)
        totals["new"] += new
        totals["existing"] += dupes
        totals["filtered"] += filt
        totals["by_source"][label] = new
        log.info("[%s] %d fetched -> %d new, %d dupes, %d filtered",
                 label, len(jobs), new, dupes, filt)

    # Company ATS boards
    if "ats_registry" in sources:
        for company in load_target_companies():
            ats = (company.get("ats") or "").lower()
            token = company.get("board_token")
            name = company.get("company")
            fetcher = FETCHERS.get(ats)
            if not fetcher or not token:
                if ats and ats not in FETCHERS:
                    log.warning("No API fetcher for ATS '%s' (%s) — Workday "
                                "boards are handled by the workday module", ats, name)
                continue
            try:
                _record(f"{ats}:{name or token}", fetcher(token, name))
            except Exception as e:
                totals["errors"] += 1
                log.error("%s board '%s' failed: %s", ats, token, e)

    # US federal jobs
    if "usajobs" in sources:
        queries = [q["query"] for q in (search_cfg.get("queries") or [])
                   if q.get("tier", 3) <= 2][:6]
        locations = [loc.get("location") for loc in (search_cfg.get("locations") or [])] or [None]
        for query in queries:
            for location in locations[:2]:
                try:
                    _record(f"usajobs:{query}", fetch_usajobs(query, location))
                except Exception as e:
                    totals["errors"] += 1
                    log.error("USAJobs '%s' failed: %s", query, e)

    # Remote-only aggregators
    if "remote_boards" in sources:
        queries = [q["query"] for q in (search_cfg.get("queries") or [])
                   if q.get("tier", 3) == 1][:3]
        for query in (queries or [None]):
            try:
                _record(f"remotive:{query or 'all'}", fetch_remotive(query))
            except Exception as e:
                totals["errors"] += 1
                log.error("Remotive failed: %s", e)
        for label, fetcher in (("remoteok", fetch_remoteok), ("arbeitnow", fetch_arbeitnow)):
            try:
                _record(label, fetcher())
            except Exception as e:
                totals["errors"] += 1
                log.error("%s failed: %s", label, e)

    log.info("ATS/API discovery complete: %d new, %d dupes, %d filtered, %d errors",
             totals["new"], totals["existing"], totals["filtered"], totals["errors"])
    return totals
