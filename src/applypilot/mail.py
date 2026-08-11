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
        response = get_client().ask_for("mail", prompt)
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
                subject: str, body: str,
                received_at: str | None = None) -> str | None:
    """Best-effort match of an email to an applied job. Returns a job URL.

    Matches on company-name tokens in the sender, subject, or body. When the
    email's arrival time is known, jobs applied to shortly beforehand are
    preferred: confirmation mail almost always lands within minutes of
    submission, which makes timing a strong disambiguator when the same
    company has several open requisitions.

    Returns None rather than guessing when nothing matches confidently.
    """
    rows = conn.execute("""
        SELECT url, title, site, applied_at FROM jobs
        WHERE applied_at IS NOT NULL
        ORDER BY applied_at DESC
        LIMIT 300
    """).fetchall()
    if not rows:
        return None

    haystack = f"{from_address or ''} {subject or ''} {(body or '')[:2000]}".lower()

    received = None
    if received_at:
        try:
            received = datetime.fromisoformat(received_at)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            received = None

    def _gap_hours(applied_at: str | None) -> float:
        """Hours between applying and this email arriving. Large = unrelated."""
        if received is None or not applied_at:
            return 1e6
        try:
            applied = datetime.fromisoformat(applied_at)
            if applied.tzinfo is None:
                applied = applied.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return 1e6
        delta = (received - applied).total_seconds() / 3600
        return delta if delta >= 0 else 1e6  # email predates the application

    company_matches = [
        row for row in rows
        if (tokens := _tokens(row["site"] or "")) and any(t in haystack for t in tokens)
    ]
    if company_matches:
        # Closest application in time wins; falls back to most recent when
        # the email carries no usable timestamp.
        return min(company_matches, key=lambda r: _gap_hours(r["applied_at"]))["url"]

    for row in rows:
        title_tokens = _tokens(row["title"] or "")
        # Require a strong title overlap to avoid matching on generic words
        if len(title_tokens) >= 2 and sum(1 for t in title_tokens if t in haystack) >= 2:
            return row["url"]

    return None


# ---------------------------------------------------------------------------
# Submission verification
# ---------------------------------------------------------------------------

# An employer's confirmation email is independent proof that a form actually
# submitted — stronger than the apply agent's own report, which can be wrong
# if a final page silently rejected the submission.
CONFIRM_PENDING_HOURS = 3     # too early to draw any conclusion
CONFIRM_TIMEOUT_HOURS = 48    # past this, no confirmation is a real signal


def mark_confirmed(conn: sqlite3.Connection, job_url: str,
                   received_at: str | None = None) -> bool:
    """Record that an employer acknowledged this application."""
    row = conn.execute(
        "SELECT confirmation_status FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if row is None or row["confirmation_status"] == "confirmed":
        return False

    conn.execute("""
        UPDATE jobs SET confirmation_status = 'confirmed', confirmed_at = ?
        WHERE url = ?
    """, (received_at or datetime.now(timezone.utc).isoformat(), job_url))
    conn.commit()
    return True


def reconcile_applications() -> dict:
    """Compare what the bot claims it submitted against what employers acknowledged.

    Every application lands in one of three states:
      confirmed   — the employer emailed back; the submission definitely landed
      pending     — applied recently; too early to expect a reply
      unconfirmed — applied over CONFIRM_TIMEOUT_HOURS ago with silence

    'unconfirmed' is a signal, not a verdict: plenty of employers never send an
    acknowledgement. It becomes meaningful in aggregate — if one ATS confirms
    90% of the time and another confirms 10%, submissions to the second are
    probably failing silently, which is exactly the failure the apply agent
    cannot self-report.
    """
    conn = get_connection()
    ensure_table(conn)

    now = datetime.now(timezone.utc)
    pending_cutoff = (now - timedelta(hours=CONFIRM_PENDING_HOURS)).isoformat()
    timeout_cutoff = (now - timedelta(hours=CONFIRM_TIMEOUT_HOURS)).isoformat()

    # Any linked email at all proves the application reached a real system.
    conn.execute("""
        UPDATE jobs SET confirmation_status = 'confirmed',
                        confirmed_at = COALESCE(confirmed_at, (
                            SELECT MIN(e.received_at) FROM emails e
                            WHERE e.job_url = jobs.url
                        ))
        WHERE applied_at IS NOT NULL
          AND COALESCE(confirmation_status, '') != 'confirmed'
          AND EXISTS (SELECT 1 FROM emails e WHERE e.job_url = jobs.url)
    """)

    conn.execute("""
        UPDATE jobs SET confirmation_status = 'pending'
        WHERE applied_at IS NOT NULL
          AND applied_at > ?
          AND COALESCE(confirmation_status, '') NOT IN ('confirmed', 'pending')
    """, (pending_cutoff,))

    conn.execute("""
        UPDATE jobs SET confirmation_status = 'unconfirmed'
        WHERE applied_at IS NOT NULL
          AND applied_at <= ?
          AND COALESCE(confirmation_status, '') != 'confirmed'
          AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.job_url = jobs.url)
    """, (timeout_cutoff,))

    conn.execute("""
        UPDATE jobs SET confirmation_status = 'pending'
        WHERE applied_at IS NOT NULL
          AND applied_at <= ? AND applied_at > ?
          AND COALESCE(confirmation_status, '') != 'confirmed'
    """, (pending_cutoff, timeout_cutoff))
    conn.commit()

    counts = {
        row[0] or "unknown": row[1]
        for row in conn.execute("""
            SELECT confirmation_status, COUNT(*) FROM jobs
            WHERE applied_at IS NOT NULL GROUP BY confirmation_status
        """)
    }

    # Confirmation rate per source — the diagnostic that reveals a site whose
    # submissions are quietly failing.
    by_site = []
    for row in conn.execute("""
        SELECT site,
               COUNT(*) AS total,
               SUM(CASE WHEN confirmation_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed
        FROM jobs
        WHERE applied_at IS NOT NULL AND applied_at <= ?
        GROUP BY site HAVING total >= 3
        ORDER BY (confirmed * 1.0 / total) ASC
    """, (timeout_cutoff,)):
        by_site.append({
            "site": row[0] or "unknown",
            "total": row[1],
            "confirmed": row[2] or 0,
            "rate": round(100 * (row[2] or 0) / row[1], 1),
        })

    total = sum(counts.values())
    confirmed = counts.get("confirmed", 0)
    return {
        "total_applied": total,
        "confirmed": confirmed,
        "pending": counts.get("pending", 0),
        "unconfirmed": counts.get("unconfirmed", 0),
        "confirm_rate": round(100 * confirmed / total, 1) if total else 0.0,
        "by_site": by_site,
    }


def check_email_alignment() -> str | None:
    """Verify the inbox being scanned is the one applications actually use.

    These are two independent settings: `personal.email` in profile.json goes
    onto every application form, while MAIL_ADDRESS is the mailbox scanned for
    replies. When they differ, confirmations arrive somewhere nobody is
    looking, the Inbox stays empty, and nothing ever links — with no error to
    explain why. Returns a description of the problem, or None when aligned.
    """
    config.load_env()
    scanned = (os.environ.get("MAIL_ADDRESS") or "").strip().lower()
    if not scanned:
        return None  # mail ingestion simply not configured yet

    if not config.PROFILE_PATH.exists():
        return None

    try:
        profile = config.load_profile()
    except (OSError, ValueError):
        return None

    applying = ((profile.get("personal") or {}).get("email") or "").strip().lower()
    if not applying:
        return None

    if applying != scanned:
        return (
            f"Email mismatch: applications are submitted with '{applying}' but "
            f"MAIL_ADDRESS scans '{scanned}'. Confirmations and recruiter replies "
            f"will arrive at '{applying}' where nothing is watching. Make both the "
            f"same address."
        )
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
             "confirmed": 0, "by_category": {}, "error": None,
             "warning": check_email_alignment()}

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
                job_url = link_to_job(conn, from_addr, subject, body, received)

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

                if job_url:
                    # Any reply from the employer proves the form submitted.
                    if mark_confirmed(conn, job_url, received):
                        stats["confirmed"] = stats.get("confirmed", 0) + 1
                    if advance_job(conn, job_url, category):
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
