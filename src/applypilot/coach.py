"""Interview prep and follow-up drafting — the stages after "applied".

Every competitor stops caring once an application is submitted. AIApply and
JobCopilot sell mock interviews, but you have to go start one; nothing knows
an interview was actually scheduled. This module hangs off the email
classifier instead, so the trigger is real:

  interview email detected  ->  prep pack generated automatically
  no reply after N days     ->  follow-up draft queued

Both are LLM-cheap (one request each, only for events that actually happen)
and both are charged against the daily budget like every other stage.

Follow-ups are DRAFTED, never sent. A bot writing unsupervised email to a
hiring manager can cost an opportunity in one badly-worded sentence — the
panel shows the draft and sending stays a deliberate tap.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from applypilot import config
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

# Days without any reply before a follow-up is worth drafting. Under a week
# reads as impatient; past three weeks the requisition is usually cold.
FOLLOWUP_AFTER_DAYS = 7
FOLLOWUP_MAX_DAYS = 21


def ensure_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create prep/follow-up tables. Idempotent."""
    if conn is None:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interview_prep (
            job_url       TEXT PRIMARY KEY,
            company       TEXT,
            title         TEXT,
            likely_questions TEXT,
            talking_points   TEXT,
            questions_to_ask TEXT,
            company_notes    TEXT,
            created_at    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            job_url     TEXT PRIMARY KEY,
            company     TEXT,
            title       TEXT,
            to_address  TEXT,
            subject     TEXT,
            body        TEXT,
            status      TEXT DEFAULT 'draft',
            days_since  INTEGER,
            created_at  TEXT
        )
    """)
    conn.commit()


def _json_from_response(text: str) -> dict | None:
    """Extract a JSON object from an LLM response that may be fenced."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        candidate = brace.group(0) if brace else None
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Interview prep
# ---------------------------------------------------------------------------

PREP_PROMPT = """You are preparing a candidate for a real interview that has just been scheduled.

Use ONLY facts present in the resume. Never invent experience, employers, dates, or metrics.

Return a single JSON object with exactly these keys:
  "likely_questions": array of 8 interview questions this specific employer is likely to ask, drawn from the job description's actual requirements
  "talking_points": array of 5 short bullet strings, each mapping a real item from the resume to something the job description asks for
  "questions_to_ask": array of 4 sharp questions the candidate should ask them, specific to this role and company
  "company_notes": a 2-3 sentence brief on what this role appears to be about and what they seem to care about most

No text outside the JSON.

JOB TITLE: {title}
COMPANY: {company}

JOB DESCRIPTION:
{description}

CANDIDATE RESUME:
{resume}
"""


def generate_prep(job_url: str, force: bool = False) -> dict | None:
    """Build an interview prep pack for one job. Returns the pack or None."""
    from applypilot.budget import BudgetExhausted
    from applypilot.llm import get_client

    conn = get_connection()
    ensure_tables(conn)

    if not force:
        existing = conn.execute(
            "SELECT job_url FROM interview_prep WHERE job_url = ?", (job_url,)
        ).fetchone()
        if existing:
            return None

    row = conn.execute(
        "SELECT url, title, site, full_description FROM jobs WHERE url = ?",
        (job_url,)).fetchone()
    if row is None:
        logger.warning("No such job for prep: %s", job_url)
        return None

    if not config.RESUME_PATH.exists():
        logger.warning("No resume — cannot generate interview prep")
        return None
    resume = config.RESUME_PATH.read_text(encoding="utf-8")

    prompt = PREP_PROMPT.format(
        title=row["title"] or "the role",
        company=row["site"] or "the company",
        description=(row["full_description"] or "")[:6000],
        resume=resume[:6000],
    )

    try:
        raw = get_client().ask_for("coach", prompt, temperature=0.4, max_tokens=2048)
    except BudgetExhausted:
        logger.warning("Interview prep skipped — daily AI budget spent")
        return None
    except Exception:
        logger.exception("Interview prep generation failed")
        return None

    data = _json_from_response(raw)
    if not data:
        logger.warning("Interview prep response was not valid JSON")
        return None

    def _lines(key: str) -> str:
        value = data.get(key)
        if isinstance(value, list):
            return "\n".join(f"- {str(v).strip()}" for v in value if str(v).strip())
        return str(value or "").strip()

    pack = {
        "job_url": job_url,
        "company": row["site"],
        "title": row["title"],
        "likely_questions": _lines("likely_questions"),
        "talking_points": _lines("talking_points"),
        "questions_to_ask": _lines("questions_to_ask"),
        "company_notes": str(data.get("company_notes") or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    conn.execute("""
        INSERT INTO interview_prep
            (job_url, company, title, likely_questions, talking_points,
             questions_to_ask, company_notes, created_at)
        VALUES (:job_url, :company, :title, :likely_questions, :talking_points,
                :questions_to_ask, :company_notes, :created_at)
        ON CONFLICT(job_url) DO UPDATE SET
            likely_questions = excluded.likely_questions,
            talking_points   = excluded.talking_points,
            questions_to_ask = excluded.questions_to_ask,
            company_notes    = excluded.company_notes,
            created_at       = excluded.created_at
    """, pack)
    conn.commit()
    logger.info("Interview prep ready: %s @ %s", pack["title"], pack["company"])
    return pack


def generate_pending_preps(limit: int = 5) -> int:
    """Generate prep for any job that reached interview and has none yet."""
    conn = get_connection()
    ensure_tables(conn)
    rows = conn.execute("""
        SELECT j.url FROM jobs j
        LEFT JOIN interview_prep p ON p.job_url = j.url
        WHERE j.outcome = 'interview' AND p.job_url IS NULL
        ORDER BY j.outcome_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    made = 0
    for row in rows:
        if generate_prep(row["url"]):
            made += 1
    return made


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------

FOLLOWUP_PROMPT = """Write a short, professional follow-up email for a job application that has had no response.

Rules:
- 4 sentences maximum. Recruiters skim.
- Reference the specific role and one concrete, relevant fact from the resume.
- Reiterate interest and ask about timeline. No desperation, no apology, no flattery.
- Plain text. No markdown, no placeholders like [Name] or [Company].
- Sign off with the candidate's name only.

Return JSON with exactly two keys: "subject" and "body". No text outside the JSON.

ROLE: {title}
COMPANY: {company}
APPLIED: {days} days ago
CANDIDATE NAME: {name}

JOB DESCRIPTION (excerpt):
{description}

RESUME (excerpt):
{resume}
"""


def draft_followup(job_url: str) -> dict | None:
    """Draft a follow-up email for one stale application. Never sends."""
    from applypilot.budget import BudgetExhausted
    from applypilot.llm import get_client

    conn = get_connection()
    ensure_tables(conn)

    row = conn.execute("""
        SELECT url, title, site, full_description, applied_at
        FROM jobs WHERE url = ?
    """, (job_url,)).fetchone()
    if row is None or not row["applied_at"]:
        return None

    try:
        applied = datetime.fromisoformat(row["applied_at"])
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - applied).days
    except (ValueError, TypeError):
        days = FOLLOWUP_AFTER_DAYS

    profile = config.load_profile() if config.PROFILE_PATH.exists() else {}
    name = (profile.get("personal") or {}).get("full_name", "")
    resume = (config.RESUME_PATH.read_text(encoding="utf-8")
              if config.RESUME_PATH.exists() else "")

    # Prefer replying into an existing thread with this employer
    to_address = None
    email_row = conn.execute("""
        SELECT from_address FROM emails
        WHERE job_url = ? AND from_address IS NOT NULL
        ORDER BY received_at DESC LIMIT 1
    """, (job_url,)).fetchone()
    if email_row:
        match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", email_row["from_address"] or "")
        if match and not any(
            noreply in match.group(0).lower()
            for noreply in ("no-reply", "noreply", "donotreply", "do-not-reply")
        ):
            to_address = match.group(0)

    prompt = FOLLOWUP_PROMPT.format(
        title=row["title"] or "the role",
        company=row["site"] or "your team",
        days=days,
        name=name,
        description=(row["full_description"] or "")[:2000],
        resume=resume[:3000],
    )

    try:
        raw = get_client().ask_for("coach", prompt, temperature=0.5, max_tokens=600)
    except BudgetExhausted:
        logger.warning("Follow-up draft skipped — daily AI budget spent")
        return None
    except Exception:
        logger.exception("Follow-up drafting failed")
        return None

    data = _json_from_response(raw)
    if not data or not data.get("body"):
        return None

    draft = {
        "job_url": job_url,
        "company": row["site"],
        "title": row["title"],
        "to_address": to_address,
        "subject": str(data.get("subject") or f"Following up — {row['title']}").strip(),
        "body": str(data["body"]).strip(),
        "status": "draft",
        "days_since": days,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    conn.execute("""
        INSERT INTO followups
            (job_url, company, title, to_address, subject, body,
             status, days_since, created_at)
        VALUES (:job_url, :company, :title, :to_address, :subject, :body,
                :status, :days_since, :created_at)
        ON CONFLICT(job_url) DO UPDATE SET
            subject = excluded.subject, body = excluded.body,
            days_since = excluded.days_since, created_at = excluded.created_at
        WHERE followups.status = 'draft'
    """, draft)
    conn.commit()
    logger.info("Follow-up drafted: %s @ %s (%dd)",
                draft["title"], draft["company"], days)
    return draft


def find_stale_applications(limit: int = 10) -> list[str]:
    """Applications with no reply, old enough to deserve a nudge.

    Excludes anything that already has an outcome, already has a draft, or
    has any linked email that is not a bare confirmation — a real human reply
    means the ball is in your court, not theirs.
    """
    conn = get_connection()
    ensure_tables(conn)

    now = datetime.now(timezone.utc)
    oldest = (now - timedelta(days=FOLLOWUP_MAX_DAYS)).isoformat()
    newest = (now - timedelta(days=FOLLOWUP_AFTER_DAYS)).isoformat()

    rows = conn.execute("""
        SELECT j.url FROM jobs j
        LEFT JOIN followups f ON f.job_url = j.url
        WHERE j.applied_at IS NOT NULL
          AND j.applied_at BETWEEN ? AND ?
          AND (j.outcome IS NULL OR j.outcome = '')
          AND f.job_url IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM emails e
              WHERE e.job_url = j.url
                AND e.category NOT IN ('confirmation', 'other')
          )
        ORDER BY j.applied_at ASC
        LIMIT ?
    """, (oldest, newest, limit)).fetchall()
    return [r["url"] for r in rows]


def draft_pending_followups(limit: int = 10) -> int:
    """Draft follow-ups for every stale application. Returns count drafted."""
    drafted = 0
    for url in find_stale_applications(limit):
        if draft_followup(url):
            drafted += 1
    return drafted


def run(limit: int = 10) -> dict:
    """Generate all pending interview preps and follow-up drafts."""
    return {
        "preps": generate_pending_preps(limit=5),
        "followups": draft_pending_followups(limit=limit),
    }
