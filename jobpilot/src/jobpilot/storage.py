"""SQLite persistence for discovered jobs and application status.

One table, `jobs`, keyed by a stable job_id. Discovery upserts rows;
matching writes scores; the apply runner writes application status. Using a
DB (not a CSV) gives us safe concurrent status updates and clean dedupe.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from jobpilot.config import DB_PATH, ensure_dirs
from jobpilot.models import Job, now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    source           TEXT,
    company          TEXT,
    title            TEXT,
    url              TEXT,
    location         TEXT,
    remote           INTEGER DEFAULT 0,
    description      TEXT,
    employment_type  TEXT,
    salary_min       INTEGER,
    salary_max       INTEGER,
    currency         TEXT,
    posted_at        TEXT,
    ats              TEXT,
    external_id      TEXT,

    score            INTEGER,
    score_reasons    TEXT,

    -- Application lifecycle
    apply_status     TEXT DEFAULT 'new',   -- new|queued|approved|applied|failed|skipped|needs_review
    apply_error      TEXT,
    apply_attempts   INTEGER DEFAULT 0,
    applied_at       TEXT,

    discovered_at    TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(apply_status);
CREATE INDEX IF NOT EXISTS idx_score ON jobs(score);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Idempotent: guarantees the schema exists on every connection so read
    # commands work before the first discover run.
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def init_db(conn: Optional[sqlite3.Connection] = None) -> None:
    # connect() already ensures the schema; kept for explicit/readable call sites.
    own = conn is None
    conn = conn or connect()
    conn.executescript(_SCHEMA)
    conn.commit()
    if own:
        conn.close()


def upsert_jobs(jobs: Iterable[Job]) -> int:
    """Insert new jobs; leave existing rows' status/scores untouched.

    Returns the number of *newly inserted* jobs.
    """
    conn = connect()
    init_db(conn)
    inserted = 0
    now = now_iso()
    try:
        for job in jobs:
            row = job.to_row()
            exists = conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
            if exists:
                # Refresh volatile fields only; preserve status/score.
                conn.execute(
                    """UPDATE jobs SET description=?, salary_min=?, salary_max=?,
                       location=?, remote=?, url=?, updated_at=? WHERE job_id=?""",
                    (row["description"], row["salary_min"], row["salary_max"],
                     row["location"], row["remote"], row["url"], now, job.job_id),
                )
                continue
            conn.execute(
                """INSERT INTO jobs (
                    job_id, source, company, title, url, location, remote,
                    description, employment_type, salary_min, salary_max, currency,
                    posted_at, ats, external_id, score, score_reasons,
                    apply_status, discovered_at, updated_at
                ) VALUES (
                    :job_id, :source, :company, :title, :url, :location, :remote,
                    :description, :employment_type, :salary_min, :salary_max, :currency,
                    :posted_at, :ats, :external_id, 0, '',
                    'new', :discovered_at, :updated_at
                )""",
                {**row, "discovered_at": now, "updated_at": now},
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def set_score(job_id: str, score: int, reasons: list[str], status: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE jobs SET score=?, score_reasons=?, apply_status=?, updated_at=? WHERE job_id=?",
            (score, "\n".join(reasons), status, now_iso(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def rows_by_status(status: str) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE apply_status=? ORDER BY score DESC", (status,)
        ).fetchall()
    finally:
        conn.close()


def new_jobs() -> list[sqlite3.Row]:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE apply_status='new'"
        ).fetchall()
    finally:
        conn.close()


def claim_for_apply(max_count: int, min_score: int) -> list[sqlite3.Row]:
    """Return queued/approved jobs ready to apply, best-scoring first."""
    conn = connect()
    try:
        return conn.execute(
            """SELECT * FROM jobs
               WHERE apply_status IN ('queued', 'approved')
                 AND score >= ?
                 AND COALESCE(apply_attempts, 0) < 3
               ORDER BY score DESC LIMIT ?""",
            (min_score, max_count),
        ).fetchall()
    finally:
        conn.close()


def mark_apply_result(job_id: str, status: str, error: str | None = None) -> None:
    conn = connect()
    try:
        if status == "applied":
            conn.execute(
                "UPDATE jobs SET apply_status='applied', applied_at=?, apply_error=NULL, updated_at=? WHERE job_id=?",
                (now_iso(), now_iso(), job_id),
            )
        else:
            conn.execute(
                """UPDATE jobs SET apply_status=?, apply_error=?,
                   apply_attempts=COALESCE(apply_attempts,0)+1, updated_at=? WHERE job_id=?""",
                (status, error, now_iso(), job_id),
            )
        conn.commit()
    finally:
        conn.close()


def set_status(job_id: str, status: str) -> int:
    """Force a job's status (used by `review approve/reject`). Returns rows changed."""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET apply_status=?, updated_at=? WHERE job_id=?",
            (status, now_iso(), job_id),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stats() -> dict[str, int]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT apply_status, COUNT(*) c FROM jobs GROUP BY apply_status"
        ).fetchall()
        out = {r["apply_status"]: r["c"] for r in rows}
        out["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return out
    finally:
        conn.close()
