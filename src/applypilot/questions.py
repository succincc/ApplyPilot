"""Screening-question memory bank.

Stores every screening question the apply agent has answered, so repeat
questions get the exact same (human-confirmable) answer instead of a fresh
AI guess each time. New questions are logged with confidence='low' for
review in the control panel; confirmed answers become confidence='high'
and are injected into the apply prompt as authoritative.

The bank lives in the local SQLite database alongside the jobs table and
is mirrored to Supabase by the sync daemon (see sync.py).
"""

import logging
import re
import sqlite3
from datetime import datetime, timezone

from applypilot.database import get_connection

logger = logging.getLogger(__name__)

# Cap how many known answers are injected into the apply prompt, most-used
# first, to keep the prompt token count bounded.
MAX_PROMPT_ANSWERS = 150

# QLOG line format the apply agent is instructed to emit:
#   QLOG: <question text> ||| <answer given>
_QLOG_RE = re.compile(r"^\s*QLOG:\s*(.+?)\s*\|\|\|\s*(.+?)\s*$", re.MULTILINE)


def ensure_table(conn: sqlite3.Connection | None = None) -> None:
    """Create the questions table if it doesn't exist. Idempotent."""
    if conn is None:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            question_normalized TEXT PRIMARY KEY,
            question_raw        TEXT NOT NULL,
            answer              TEXT NOT NULL,
            answer_type         TEXT DEFAULT 'text',
            confidence          TEXT DEFAULT 'low',
            times_used          INTEGER DEFAULT 0,
            last_used_at        TEXT,
            source_job_url      TEXT,
            created_at          TEXT,
            updated_at          TEXT
        )
    """)
    conn.commit()


def normalize(question: str) -> str:
    """Canonicalize a question for matching: lowercase, strip punctuation,
    collapse whitespace. 'Are you a Veteran?' == 'are you a veteran'."""
    q = question.lower().strip()
    q = re.sub(r"[^\w\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def get_known_answers(limit: int = MAX_PROMPT_ANSWERS) -> list[dict]:
    """Return stored Q&A pairs, high-confidence first, then most-used."""
    conn = get_connection()
    ensure_table(conn)
    rows = conn.execute("""
        SELECT question_raw, answer, confidence, times_used
        FROM questions
        ORDER BY CASE confidence WHEN 'high' THEN 0 ELSE 1 END,
                 times_used DESC, question_normalized
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def record_answer(question_raw: str, answer: str,
                  source_job_url: str | None = None) -> None:
    """Log a question/answer pair from an apply run.

    New question -> inserted with confidence='low' (needs human review).
    Known question -> bump times_used; the stored answer is NOT overwritten
    (human-confirmed answers must never be clobbered by an agent's output).
    """
    q_norm = normalize(question_raw)
    if not q_norm or not answer:
        return

    conn = get_connection()
    ensure_table(conn)
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute(
        "SELECT question_normalized FROM questions WHERE question_normalized = ?",
        (q_norm,),
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE questions
            SET times_used = COALESCE(times_used, 0) + 1,
                last_used_at = ?, updated_at = ?
            WHERE question_normalized = ?
        """, (now, now, q_norm))
    else:
        conn.execute("""
            INSERT INTO questions
                (question_normalized, question_raw, answer, confidence,
                 times_used, last_used_at, source_job_url, created_at, updated_at)
            VALUES (?, ?, ?, 'low', 1, ?, ?, ?, ?)
        """, (q_norm, question_raw.strip(), answer.strip(), now,
              source_job_url, now, now))
    conn.commit()


def parse_qlog(agent_output: str, source_job_url: str | None = None) -> int:
    """Extract QLOG lines from apply-agent output and store them.

    Returns the number of Q&A pairs recorded. Never raises — a logging
    failure must not affect the apply result.
    """
    count = 0
    try:
        for match in _QLOG_RE.finditer(agent_output):
            question, answer = match.group(1), match.group(2)
            # Guard against degenerate captures
            if len(question) < 4 or len(question) > 500 or len(answer) > 1000:
                continue
            record_answer(question, answer, source_job_url)
            count += 1
    except Exception:
        logger.exception("Failed to parse QLOG lines")
    return count


def build_prompt_section() -> str:
    """Build the KNOWN ANSWERS section for the apply prompt.

    Returns an empty string when the bank is empty (first runs), so the
    prompt stays unchanged until there is something to inject.
    """
    try:
        known = get_known_answers()
    except Exception:
        logger.exception("Failed to load question bank")
        known = []

    qa_lines = ""
    if known:
        qa_lines = "\n".join(
            f'- Q: "{q["question_raw"]}" -> A: {q["answer"]}'
            for q in known
        )
        qa_lines = f"""These answers were given on previous applications (human-reviewed where marked).
When a screening question matches one below (same meaning, even if worded differently), use the stored answer EXACTLY — consistency across applications matters:
{qa_lines}

"""

    return f"""== KNOWN ANSWERS & QUESTION LOG ==
{qa_lines}QUESTION LOG (mandatory): At the END of your output, after the RESULT line, emit one line per screening question you answered on this application, in this exact format:
QLOG: <question as shown on the form> ||| <answer you gave>
Log every screening question (dropdowns, yes/no, text). Skip basic contact fields (name, email, phone, address) and EEO/demographic questions."""
