"""Daily LLM request budget — what keeps the free tier actually free.

The Gemini free tier allows roughly 1,500 requests per day. The pipeline can
spend that carelessly, because cost is not evenly distributed across stages:

  score   1 request per DISCOVERED job — the largest consumer by far, and the
          least valuable per request (most scored jobs are never applied to)
  tailor  1 request per job above threshold, retried up to 5 times on
          validation failure
  cover   same shape as tailor
  apply   a few requests per application for screening answers
  mail    a handful per day, only for ambiguous messages

A heavy discovery day (800 jobs) spends its entire quota on scoring, then
tailoring fails with 429s and the client sits in exponential backoff. The
result is a run that appears to work, burns hours, and submits nothing.

This module makes the budget explicit and spends it in priority order.
Reservations guarantee that the stages closest to an actual submitted
application always have quota left, even when discovery goes wide.

Counting is per UTC day in SQLite, so it survives restarts and parallel
workers.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

from applypilot.database import get_connection

logger = logging.getLogger(__name__)

# Conservative default for the Gemini free tier. Lower than the documented
# ceiling on purpose: quotas vary by model and Google adjusts them.
DEFAULT_DAILY_BUDGET = 1400

# Share of the daily budget reserved for each stage. Stages are served in this
# order; a stage may exceed its share only if later stages' reservations are
# still intact. Tailoring and applying outrank scoring because they convert
# directly into submitted applications.
# Shares are sized from real per-day consumption at ~100 applications:
# scoring runs once per DISCOVERED job (by far the largest consumer), while
# tailoring and cover letters run only for jobs that clear the threshold.
# The apply stage's share is small because form-filling runs on Claude Code,
# not on this quota. Shares must sum to 1.0.
STAGE_RESERVATIONS: dict[str, float] = {
    "score": 0.50,   # ~1 request per discovered job — the bulk of the spend
    "tailor": 0.20,  # per job above threshold, retried on validation failure
    "cover": 0.15,   # per job above threshold
    "enrich": 0.08,  # AI description extraction for unknown page layouts
    "mail": 0.04,    # ambiguous email classification only
    "apply": 0.03,   # screening-answer help during submission
}

_lock = threading.Lock()


class BudgetExhausted(RuntimeError):
    """Raised when a stage has no daily quota left.

    Callers should stop cleanly and let the run resume tomorrow, rather than
    retrying into rate-limit backoff.
    """

    def __init__(self, stage: str, used: int, limit: int):
        self.stage = stage
        self.used = used
        self.limit = limit
        super().__init__(
            f"Daily LLM budget exhausted for '{stage}' ({used}/{limit} requests used). "
            f"The run will continue tomorrow, or set LLM_DAILY_BUDGET higher if "
            f"you are on a paid tier."
        )


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_daily_budget() -> int:
    """Total requests allowed per day. 0 disables budgeting entirely."""
    raw = os.environ.get("LLM_DAILY_BUDGET")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("LLM_DAILY_BUDGET is not a number: %r", raw)

    # Local models and self-hosted endpoints have no per-day quota.
    if os.environ.get("LLM_URL"):
        return 0
    return DEFAULT_DAILY_BUDGET


def ensure_table(conn: sqlite3.Connection | None = None) -> None:
    """Create the usage-tracking table. Idempotent."""
    if conn is None:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            day    TEXT NOT NULL,
            stage  TEXT NOT NULL,
            count  INTEGER DEFAULT 0,
            PRIMARY KEY (day, stage)
        )
    """)
    conn.commit()


def record(stage: str, count: int = 1) -> None:
    """Record LLM requests against today's budget."""
    conn = get_connection()
    ensure_table(conn)
    with _lock:
        conn.execute("""
            INSERT INTO llm_usage (day, stage, count) VALUES (?, ?, ?)
            ON CONFLICT(day, stage) DO UPDATE SET count = count + excluded.count
        """, (_today(), stage or "other", count))
        conn.commit()


def usage(day: str | None = None) -> dict[str, int]:
    """Requests used per stage for a given day (default today)."""
    conn = get_connection()
    ensure_table(conn)
    rows = conn.execute(
        "SELECT stage, count FROM llm_usage WHERE day = ?", (day or _today(),)
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def total_used(day: str | None = None) -> int:
    return sum(usage(day).values())


def remaining(stage: str | None = None) -> int:
    """Requests still available — overall, or for one stage.

    A stage may spend its own unused reservation, plus any surplus that no
    other stage still has reserved. Concretely: a runaway discovery day can
    spend scoring down to zero without ever consuming the quota that tailoring
    and cover letters need to turn jobs into submitted applications.

    Returns a large number when budgeting is disabled (local models).
    """
    limit = get_daily_budget()
    if limit <= 0:
        return 10**9

    used_by_stage = usage()
    left_overall = max(0, limit - sum(used_by_stage.values()))

    if stage is None:
        return left_overall

    share = STAGE_RESERVATIONS.get(stage)
    if share is None:
        # Unknown stage: may only use surplus beyond every reservation.
        reserved = sum(max(0, int(limit * s) - used_by_stage.get(name, 0))
                       for name, s in STAGE_RESERVATIONS.items())
        return max(0, min(left_overall, left_overall - reserved))

    own_left = max(0, int(limit * share) - used_by_stage.get(stage, 0))
    others_reserved = sum(
        max(0, int(limit * s) - used_by_stage.get(name, 0))
        for name, s in STAGE_RESERVATIONS.items()
        if name != stage
    )
    surplus = max(0, left_overall - others_reserved)

    return min(left_overall, own_left + surplus)


def check(stage: str, need: int = 1) -> None:
    """Raise BudgetExhausted if `stage` cannot afford `need` more requests."""
    limit = get_daily_budget()
    if limit <= 0:
        return
    if remaining(stage) < need:
        raise BudgetExhausted(stage, total_used(), limit)


def can_afford(stage: str, need: int = 1) -> bool:
    """Non-raising form of check()."""
    try:
        check(stage, need)
        return True
    except BudgetExhausted:
        return False


def summary() -> dict:
    """Budget state for the dashboard and the panel heartbeat."""
    limit = get_daily_budget()
    used_by_stage = usage()
    used = sum(used_by_stage.values())
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if limit > 0 else None,
        "unlimited": limit <= 0,
        "by_stage": used_by_stage,
        "percent_used": round(100 * used / limit, 1) if limit > 0 else 0.0,
    }
