"""Email ingestion and classification: turns your inbox into a pipeline signal.

Reads mail over IMAP (stdlib only — no Google API client, no OAuth consent
screen, no token refresh to break) and sorts it into the categories that
matter during a job hunt: interview, offer, rejection, action_needed,
confirmation.

Classification is rules-first: keyword and sender-domain matching handles the
overwhelming majority at zero API cost. Only genuinely ambiguous job-related
mail falls through to the LLM, keeping daily usage far inside the free tier.

Setup (one time, free):
  1. Enable 2-Step Verification on the Google account.
  2. Create an App Password: https://myaccount.google.com/apppasswords
  3. Put it in ~/.applypilot/.env:
       MAIL_ADDRESS=you@gmail.com
       MAIL_APP_PASSWORD=abcd efgh ijkl mnop
       MAIL_IMAP_HOST=imap.gmail.com   # optional, this is the default

Access is read-only: this module never sends, deletes, or modifies mail.
"""

import email
import imaplib
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

from applypilot import config
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_LOOKBACK_DAYS = 30
MAX_BODY_CHARS = 20000

# Domains that only ever send recruiting mail — a strong signal that a message
# is job-related even when the wording is unusual.
ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkday.com", "myworkdayjobs.com",
    "ashbyhq.com", "icims.com", "smartrecruiters.com", "jobvite.com",
    "taleo.net", "successfactors.com", "bamboohr.com", "workable.com",
    "breezy.hr", "recruitee.com", "teamtailor.com", "hire.lever.co",
    "applytojob.com", "paylocity.com", "ultipro.com", "adp.com",
    "indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com",
    "hackerrank.com", "codility.com", "hackerearth.com", "karat.com",
)

# Ordered by precedence — the first category whose patterns match wins.
# Rejection outranks interview because rejection mail often references an
# interview that already happened ("thank you for interviewing... unfortunately").
CATEGORY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("offer", (
        "pleased to offer", "offer of employment", "offer letter",
        "we are excited to offer", "extend an offer", "your offer",
        "compensation package", "welcome to the team",
    )),
    ("rejection", (
        "unfortunately", "not moving forward", "will not be moving forward",
        "other candidates", "decided to move forward with other",
        "regret to inform", "not selected", "no longer under consideration",
        "position has been filled", "decided to pursue other",
        "not a match at this time", "we won't be proceeding",
        "wish you the best in your search", "keep your resume on file",
    )),
    ("interview", (
        "schedule an interview", "interview invitation", "phone screen",
        "would like to speak", "would love to chat", "set up a time",
        "your availability", "schedule a call", "next steps in the process",
        "meet with the team", "invite you to interview", "book a time",
        "calendly.com", "schedule some time", "initial conversation",
        "hiring manager would like",
    )),
    ("action_needed", (
        "action required", "please complete", "complete your application",
        "assessment", "coding challenge", "take-home", "skills test",
        "verify your email", "verification code", "confirm your email",
        "additional information needed", "please respond", "documents required",
        "background check", "reference check", "fill out the following",
    )),
    ("confirmation", (
        "application received", "thank you for applying",
        "we received your application", "successfully submitted",
        "your application has been submitted", "thanks for your interest",
        "application confirmation", "we have your application",
    )),
]


# ---------------------------------------------------------------------------
# Local storage
# ---------------------------------------------------------------------------

def ensure_table(conn: sqlite3.Connection | None = None) -> None:
    """Create the emails table if missing. Idempotent."""
    if conn is None:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            message_id     TEXT PRIMARY KEY,
            from_address   TEXT,
            subject        TEXT,
            snippet        TEXT,
            body_text      TEXT,
            received_at    TEXT,
            category       TEXT,
            classified_by  TEXT,
            job_url        TEXT,
            is_read        INTEGER DEFAULT 0,
            created_at     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at)")
    conn.commit()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_job_related(from_address: str, subject: str, body: str) -> bool:
    """Cheap pre-filter so personal mail is never stored or sent to an LLM."""
    sender = (from_address or "").lower()
    if any(d in sender for d in ATS_DOMAINS):
        return True

    blob = f"{subject or ''} {body or ''}".lower()
    signals = (
        "application", "applying", "candidate", "position", "role",
        "interview", "recruiter", "hiring", "job", "resume", "cv",
        "opportunity", "career",
    )
    return sum(1 for s in signals if s in blob) >= 2


def classify_rules(from_address: str, subject: str, body: str) -> str | None:
    """Rules-based classification. Returns a category or None if unclear.

    Subject line matches are weighted over body matches: a subject saying
    'Interview invitation' is decisive even if the body mentions 'unfortunately'
    somewhere in a footer.
    """
    subj = (subject or "").lower()
    blob = f"{subject or ''}\n{body or ''}".lower()

    for category, patterns in CATEGORY_PATTERNS:
        if any(p in subj for p in patterns):
            return category

    for category, patterns in CATEGORY_PATTERNS:
        if any(p in blob for p in patterns):
            return category

    return None


def classify_ai(subject: str, body: str) -> str | None:
    """LLM fallback for ambiguous job-related mail. Returns None on any failure.

    Only reached for mail that passed is_job_related() but matched no rule —
    typically a handful of messages per day.
    """
    try:
        from applypilot.llm import get_client
    except ImportError:
        return None

    prompt = (
        "Classify this job-search email into exactly one category. "
        "Reply with ONLY the category word, nothing else.\n\n"
        "Categories:\n"
        "interview - inviting to interview, scheduling, or asking availability\n"
        "rejection - declining the candidate\n"
        "offer - extending a job offer\n"
        "action_needed - requires the candidate to do something (assessment, "
        "verification, forms, additional info)\n"
        "confirmation - acknowledging an application was received\n"
        "other - anything else\n\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{(body or '')[:3000]}\n\n"
        "Category:"
    )

    try:
        response = get_client().ask(prompt)
    except Exception:
        logger.debug("AI classification unavailable", exc_info=True)
        return None

    if not response:
        return None

    answer = str(response).strip().lower()
    for valid in ("interview", "rejection", "offer", "action_needed",
                  "confirmation", "other"):
        if valid in answer:
            return valid
    return None


def classify(from_address: str, subject: str, body: str) -> tuple[str, str]:
    """Classify one email. Returns (category, classified_by)."""
    category = classify_rules(from_address, subject, body)
    if category:
        return category, "rules"

    category = classify_ai(subject, body)
    if category:
        return category, "ai"

    return "other", "rules"


# ---------------------------------------------------------------------------
# Job linking
# ---------------------------------------------------------------------------

def _tokens(name: str) -> set[str]:
    """Significant lowercase word tokens from a company name."""
    stop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co",
            "the", "group", "technologies", "technology", "labs", "systems",
            "solutions", "software", "services", "holdings", "international"}
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if len(w) > 2 and w not in stop}


def link_to_job(conn: sqlite3.Connection, from_address: str,
                subject: str, body: str) -> str | None:
    """Best-effort match of an email to an applied job. Returns a job URL.

    Matches on company-name tokens appearing in the sender, subject, or body,
    preferring the most recently applied job. Returns None rather than
    guessing when nothing matches confidently.
    """
    rows = conn.execute("""
        SELECT url, title, site FROM jobs
        WHERE applied_at IS NOT NULL
        ORDER BY applied_at DESC
        LIMIT 300
    """).fetchall()
    if not rows:
        return None

    haystack = f"{from_address or ''} {subject or ''} {(body or '')[:2000]}".lower()

    for row in rows:
        company_tokens = _tokens(row["site"] or "")
        if company_tokens and any(t in haystack for t in company_tokens):
            return row["url"]

    for row in rows:
        title_tokens = _tokens(row["title"] or "")
        # Require a strong title overlap to avoid matching on generic words
        if len(title_tokens) >= 2 and sum(1 for t in title_tokens if t in haystack) >= 2:
            return row["url"]

    return None


# ---------------------------------------------------------------------------
# Pipeline advancement
# ---------------------------------------------------------------------------

# Terminal outcomes are never downgraded by later automated mail: once a job
# reaches interview or offer, a rejection for some other requisition at the
# same company must not silently erase it.
_RANK = {"applied": 0, "action_needed": 1, "rejection": 2, "interview": 3, "offer": 4}


def advance_job(conn: sqlite3.Connection, job_url: str, category: str) -> bool:
    """Update a job's outcome from a classified email. Returns True if changed."""
    if category not in ("interview", "offer", "rejection"):
        return False

    row = conn.execute(
        "SELECT outcome FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if row is None:
        return False

    current = row["outcome"] or "applied"
    if _RANK.get(category, 0) <= _RANK.get(current, 0):
        return False

    conn.execute(
        "UPDATE jobs SET outcome = ?, outcome_at = ? WHERE url = ?",
        (category, datetime.now(timezone.utc).isoformat(), job_url))
    conn.commit()
    logger.info("Job outcome: %s -> %s (%s)", current, category, job_url[:60])
    return True


# ---------------------------------------------------------------------------
# IMAP fetch
# ---------------------------------------------------------------------------

def _decode(value: str | None) -> str:
    """Decode an RFC 2047 encoded header into plain text."""
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _extract_body(msg) -> str:
    """Plain-text body, preferring text/plain and falling back to stripped HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                    part.get("Content-Disposition", "")):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace")
                        return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
        return ""

    try:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            return text
    except Exception:
        pass
    return ""


def fetch_and_store(lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                    limit: int = 200) -> dict:
    """Fetch recent mail over IMAP, classify it, and store job-related messages.

    Returns a stats dict: fetched, stored, skipped, by_category, advanced.
    """
    config.load_env()
    address = os.environ.get("MAIL_ADDRESS", "")
    password = os.environ.get("MAIL_APP_PASSWORD", "")
    host = os.environ.get("MAIL_IMAP_HOST", DEFAULT_IMAP_HOST)

    stats = {"fetched": 0, "stored": 0, "skipped": 0, "advanced": 0,
             "by_category": {}, "error": None}

    if not address or not password:
        stats["error"] = (
            "MAIL_ADDRESS and MAIL_APP_PASSWORD not set in ~/.applypilot/.env — "
            "create an app password at https://myaccount.google.com/apppasswords"
        )
        return stats

    conn = get_connection()
    ensure_table(conn)

    known = {r[0] for r in conn.execute("SELECT message_id FROM emails")}

    try:
        imap = imaplib.IMAP4_SSL(host)
        imap.login(address, password)
        imap.select("INBOX", readonly=True)

        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            stats["error"] = f"IMAP search failed: {typ}"
            return stats

        ids = data[0].split()
        ids = ids[-limit:]  # newest N
        stats["fetched"] = len(ids)

        for msg_id in reversed(ids):
            try:
                typ, msg_data = imap.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                message_id = _decode(msg.get("Message-ID")) or f"{address}-{msg_id.decode()}"
                if message_id in known:
                    continue

                from_addr = _decode(msg.get("From"))
                subject = _decode(msg.get("Subject"))
                body = _extract_body(msg)[:MAX_BODY_CHARS]

                if not is_job_related(from_addr, subject, body):
                    stats["skipped"] += 1
                    continue

                try:
                    received = parsedate_to_datetime(msg.get("Date")).isoformat()
                except (TypeError, ValueError):
                    received = datetime.now(timezone.utc).isoformat()

                category, by = classify(from_addr, subject, body)
                job_url = link_to_job(conn, from_addr, subject, body)

                conn.execute("""
                    INSERT OR IGNORE INTO emails
                        (message_id, from_address, subject, snippet, body_text,
                         received_at, category, classified_by, job_url,
                         is_read, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """, (message_id, from_addr, subject,
                      " ".join(body.split())[:300], body, received,
                      category, by, job_url,
                      datetime.now(timezone.utc).isoformat()))

                stats["stored"] += 1
                stats["by_category"][category] = stats["by_category"].get(category, 0) + 1

                if job_url and advance_job(conn, job_url, category):
                    stats["advanced"] += 1

            except Exception:
                logger.exception("Failed to process message %s", msg_id)
                continue

        conn.commit()
        imap.close()
        imap.logout()

    except imaplib.IMAP4.error as e:
        stats["error"] = (
            f"IMAP login failed: {e}. Use a Google App Password, not your "
            "account password (https://myaccount.google.com/apppasswords)."
        )
    except Exception as e:
        stats["error"] = f"Mail fetch failed: {e}"
        logger.exception("Mail fetch failed")

    return stats
