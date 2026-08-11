"""Job filtering: the rules that decide what never reaches your application queue.

Single source of truth for every filter the control panel exposes. Before this
module existed, `exclude_titles`, `exclude_companies`, `salary_floor`, and the
no-go patterns were present in searches.yaml and documented, but no code read
them — the filters were decorative.

Design rule, from the volume doctrine: filters exist to remove jobs that are
*definitely* wrong (scam patterns, blocked companies, wrong title level), never
to remove jobs that are merely uncertain. Anything ambiguous passes through and
lets the AI scorer decide. In particular a job with no listed salary always
passes the salary filter — most postings omit salary, and filtering them out
would delete the majority of the market.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Patterns that mark a posting as not-a-real-career-job. Matched against the
# job description and title. These are the "no bullshit jobs" rules.
DEFAULT_NO_GO_PATTERNS = [
    "commission only",
    "commission-only",
    "100% commission",
    "uncapped commission only",
    "unlimited earning potential",
    "door to door",
    "door-to-door",
    "1099 only",
    "1099-only",
    "be your own boss",
    "unlimited income potential",
    "no experience necessary",
    "work from home opportunity",
    "mlm",
    "multi-level marketing",
    "pyramid",
    "pay to start",
    "startup fee",
    "purchase your own leads",
]

_REMOTE_TOKENS = ("remote", "anywhere", "work from home", "wfh", "distributed", "telecommute")

# Salary strings like "$95,000-$120,000/yearly", "USD80,000", "$45/hr"
_MONEY_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*([kK])?")
_HOURLY_TOKENS = ("hour", "hourly", "/hr", "per hour")


# ---------------------------------------------------------------------------
# Config loading (tolerates both schema shapes)
# ---------------------------------------------------------------------------

def load_filter_config(search_cfg: dict) -> dict:
    """Normalize filter settings out of a search config.

    Accepts both the nested shape used by searches.example.yaml
    (`location: {accept_patterns, reject_patterns}`) and the flat shape the
    discovery modules originally expected (`location_accept`,
    `location_reject_non_remote`). Previously only the flat keys were read,
    so the shipped example config silently produced an EMPTY accept list —
    which made the location filter reject every non-remote job.
    """
    location_cfg = search_cfg.get("location") or {}

    accept = (
        search_cfg.get("location_accept")
        or location_cfg.get("accept_patterns")
        or []
    )
    reject = (
        search_cfg.get("location_reject_non_remote")
        or location_cfg.get("reject_patterns")
        or []
    )

    no_go = search_cfg.get("no_go_patterns")
    if no_go is None:
        no_go = DEFAULT_NO_GO_PATTERNS

    return {
        "location_accept": [str(a).lower() for a in accept],
        "location_reject": [str(r).lower() for r in reject],
        "exclude_titles": [str(t).lower() for t in (search_cfg.get("exclude_titles") or [])],
        "exclude_companies": [str(c).lower() for c in (search_cfg.get("exclude_companies") or [])],
        "no_go_patterns": [str(p).lower() for p in no_go],
        "salary_floor": search_cfg.get("salary_floor"),
        "job_types": [str(j).lower() for j in (search_cfg.get("job_types") or [])],
    }


def get_boards(search_cfg: dict, default: list[str] | None = None) -> list[str]:
    """Read the board list, accepting either `boards` or `sites` as the key.

    run_discovery originally read only `sites`, while both the shipped example
    config and the control panel write `boards` — so board selection was
    silently ignored and always fell back to the hardcoded default.
    """
    boards = search_cfg.get("boards") or search_cfg.get("sites")
    if not boards:
        return default if default is not None else ["indeed", "linkedin", "zip_recruiter"]
    return list(boards)


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

def parse_salary(salary_text: str | None) -> tuple[int | None, int | None]:
    """Extract (min, max) annual salary in whole dollars from a salary string.

    Handles "$95,000-$120,000", "95k-120k", "USD 80,000/yearly", "$45/hr"
    (converted to annual at 2080 hours). Returns (None, None) when nothing
    parseable is found — callers must treat that as "unknown", not "zero".
    """
    if not salary_text:
        return None, None

    text = str(salary_text).lower()
    if text in ("nan", "none", ""):
        return None, None

    hourly = any(tok in text for tok in _HOURLY_TOKENS)

    values: list[int] = []
    for match in _MONEY_RE.finditer(text):
        raw, k_suffix = match.group(1), match.group(2)
        try:
            amount = float(raw.replace(",", ""))
        except ValueError:
            continue
        if k_suffix:
            amount *= 1000
        # Bare numbers under 1000 in a non-hourly string are usually noise
        # (dates, "401k" fragments); treat small values as hourly rates only.
        if hourly:
            if 5 <= amount <= 500:
                amount *= 2080
            else:
                continue
        elif amount < 1000:
            continue
        if 1000 <= amount <= 10_000_000:
            values.append(int(amount))

    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return min(values), max(values)


# ---------------------------------------------------------------------------
# Individual filters — each returns (passed, reason_if_rejected)
# ---------------------------------------------------------------------------

def location_ok(location: str | None, accept: list[str], reject: list[str]) -> tuple[bool, str | None]:
    """Remote always passes. Unknown location passes (scorer decides).

    An empty accept list means 'no location restriction' — it must NOT mean
    'reject everything', which was the old behavior when the config key
    mismatch left the list empty.
    """
    if not location:
        return True, None

    loc = str(location).lower()
    if loc == "nan":
        return True, None

    if any(tok in loc for tok in _REMOTE_TOKENS):
        return True, None

    for r in reject:
        if r in loc:
            return False, f"location rejected: {location}"

    if not accept:
        return True, None

    for a in accept:
        if a in loc:
            return True, None

    return False, f"location outside target area: {location}"


def title_ok(title: str | None, exclude_titles: list[str]) -> tuple[bool, str | None]:
    """Reject titles containing a blocked keyword (word-boundary aware)."""
    if not title or not exclude_titles:
        return True, None

    t = str(title).lower()
    for bad in exclude_titles:
        bad = bad.strip()
        if not bad:
            continue
        # Word-boundary match so "VP" doesn't hit "Developer" and
        # "intern" doesn't hit "internal" or "international".
        if re.search(rf"(?<!\w){re.escape(bad)}(?!\w)", t):
            return False, f"excluded title keyword: {bad}"
    return True, None


def company_ok(company: str | None, exclude_companies: list[str]) -> tuple[bool, str | None]:
    """Reject blocked companies (substring match — company names vary)."""
    if not company or not exclude_companies:
        return True, None

    c = str(company).lower()
    for bad in exclude_companies:
        bad = bad.strip()
        if bad and bad in c:
            return False, f"blocked company: {company}"
    return True, None


def salary_ok(salary_text: str | None, floor: int | None) -> tuple[bool, str | None]:
    """Jobs with no listed salary ALWAYS pass — most postings omit it.

    Only reject when a salary is stated and its maximum is below the floor.
    Using the max (not the min) keeps wide ranges that reach your floor.
    """
    if not floor:
        return True, None

    lo, hi = parse_salary(salary_text)
    if lo is None:
        return True, None

    top = hi if hi is not None else lo
    if top < int(floor):
        return False, f"salary below floor: {salary_text}"
    return True, None


def no_go_ok(title: str | None, description: str | None,
             patterns: list[str]) -> tuple[bool, str | None]:
    """Reject scam / commission-only / MLM postings by pattern."""
    if not patterns:
        return True, None

    blob = f"{title or ''}\n{description or ''}".lower()
    if not blob.strip():
        return True, None

    for pattern in patterns:
        pattern = pattern.strip()
        if pattern and pattern in blob:
            return False, f"no-go pattern: {pattern}"
    return True, None


# ---------------------------------------------------------------------------
# Combined gate
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Free pre-scoring triage
# ---------------------------------------------------------------------------

# Seniority markers that mean a posting is out of range in either direction.
_TOO_SENIOR = (
    "vice president", "vp of", "chief ", "head of", "director of",
    "senior director", "principal architect", "distinguished engineer",
    "svp", "evp", "c-level", "cto", "cio", "ceo",
)
_TOO_JUNIOR = (
    "intern", "internship", "co-op", "coop program", "apprentice",
    "trainee", "student", "entry level only", "high school",
)
# Hard blockers no amount of fit can overcome.
_HARD_BLOCKERS = (
    "active security clearance", "ts/sci", "top secret clearance",
    "polygraph", "must have clearance", "secret clearance required",
)


def prescore(job: dict, profile_years: int | None = None) -> tuple[bool, str | None]:
    """Rule-based triage run BEFORE any LLM request. Free.

    Returns (worth_scoring, reason_if_not). This exists purely for volume:
    every job rejected here is a free-tier request that goes to tailoring
    instead of being spent proving a mismatch the title already made obvious.

    Deliberately conservative — it only rejects what is unambiguous. Anything
    debatable passes through to the AI scorer, because the volume doctrine
    says uncertainty gets an application, not a filter.
    """
    title = (job.get("title") or "").lower()
    description = (job.get("full_description") or job.get("description") or "").lower()

    if not title:
        return True, None

    for marker in _TOO_SENIOR:
        if marker in title:
            return False, f"seniority mismatch: {marker}"

    for marker in _TOO_JUNIOR:
        if re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", title):
            return False, f"level mismatch: {marker}"

    blob = f"{title}\n{description[:3000]}"
    for blocker in _HARD_BLOCKERS:
        if blocker in blob:
            return False, f"hard requirement: {blocker}"

    # A posting with no description at all cannot be scored meaningfully;
    # enrichment will fill it in on a later pass.
    if not description.strip():
        return False, "no description yet"

    return True, None


def job_passes(job: dict, filters: dict) -> tuple[bool, str | None]:
    """Run every filter against one job.

    Args:
        job: dict with any of title, company, location, salary, description.
        filters: output of load_filter_config().

    Returns:
        (passed, reason). reason is None when passed, otherwise a short
        human-readable string stored as skip_reason for the audit trail.
    """
    checks = (
        location_ok(job.get("location"), filters["location_accept"], filters["location_reject"]),
        title_ok(job.get("title"), filters["exclude_titles"]),
        company_ok(job.get("company") or job.get("site"), filters["exclude_companies"]),
        salary_ok(job.get("salary"), filters["salary_floor"]),
        no_go_ok(job.get("title"), job.get("description"), filters["no_go_patterns"]),
    )

    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, None
