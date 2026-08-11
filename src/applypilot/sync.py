"""Supabase bridge daemon: connects the local engine to Mission Control.

Long-running loop that makes the web panel's Start button real:
  - heartbeats engine status so the panel shows online/offline live
  - polls the `commands` table and executes start_run / stop / apply_one /
    mark_resolved by driving the existing pipeline as subprocesses
  - mirrors the local SQLite jobs table into the cloud `jobs` table
    (change-detected via fingerprints, upsert on url)
  - uploads tailored resume / cover letter PDFs to Storage
  - pulls human decisions down: review approvals, rejections, question
    answer corrections — the panel always wins over engine writes
  - renders the active search preset into ~/.applypilot/searches.yaml

The panel and this daemon never talk directly; Supabase is the contract
(see docs/BRIDGE_SPEC.md). Requires SUPABASE_URL and SUPABASE_SERVICE_KEY
in ~/.applypilot/.env. Run with: applypilot sync
"""

import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from applypilot import config, questions
from applypilot.database import get_connection

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30
COMMAND_INTERVAL = 15
MIRROR_INTERVAL = 60
MAIL_INTERVAL = 600
UPSERT_BATCH = 200

# Panel statuses set by a human — the engine must never overwrite these.
HUMAN_STATUSES = {"rejected_by_me", "interview", "offer", "rejected"}

# apply_error reasons that mean "a human can unblock this in minutes"
ATTENTION_REASONS = {
    "captcha": "captcha",
    "login_issue": "login_wall",
    "sso_required": "login_wall",
    "manual ATS": "captcha",
}


# ---------------------------------------------------------------------------
# Supabase REST client (PostgREST + Storage), service-role key
# ---------------------------------------------------------------------------

class Supabase:
    def __init__(self, url: str, key: str):
        self.rest = f"{url.rstrip('/')}/rest/v1"
        self.storage = f"{url.rstrip('/')}/storage/v1"
        self.client = httpx.Client(
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def select(self, table: str, query: str = "") -> list[dict]:
        url = f"{self.rest}/{table}"
        if query:
            url += f"?{query}"
        r = self.client.get(url)
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        for i in range(0, len(rows), UPSERT_BATCH):
            r = self.client.post(
                f"{self.rest}/{table}?on_conflict={on_conflict}",
                content=json.dumps(rows[i:i + UPSERT_BATCH]),
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
            r.raise_for_status()

    def update(self, table: str, query: str, patch: dict) -> None:
        r = self.client.patch(
            f"{self.rest}/{table}?{query}",
            content=json.dumps(patch),
            headers={"Prefer": "return=minimal"},
        )
        r.raise_for_status()

    def upload(self, bucket: str, path: str, data: bytes,
               content_type: str = "application/pdf") -> bool:
        r = self.client.post(
            f"{self.storage}/object/{bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        if r.status_code >= 400:
            logger.warning("Storage upload failed (%s): %s", path, r.text[:200])
            return False
        return True


def connect() -> Supabase:
    config.load_env()
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in "
            f"{config.ENV_PATH}.\nFind both in your Supabase project: "
            "Settings -> API (use the service_role key, keep it secret).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return Supabase(url, key)


# ---------------------------------------------------------------------------
# Status mapping: local SQLite row -> panel status
# ---------------------------------------------------------------------------

def derive_status(row: dict, review_low: int = 4, auto_min: int = 6) -> tuple[str, str | None]:
    """Map engine columns to the panel's status vocabulary.

    Returns (status, attention_reason).
    """
    apply_status = row.get("apply_status") or ""
    apply_error = row.get("apply_error") or ""

    # An outcome learned from classified email outranks everything: once a
    # recruiter has replied, that reply is the truth about this application.
    outcome = row.get("outcome")
    if outcome in ("interview", "offer", "rejection"):
        return {"rejection": "rejected"}.get(outcome, outcome), None

    if row.get("applied_at"):
        return "applied", None
    if apply_status == "in_progress":
        return "applying", None
    for key, reason in ATTENTION_REASONS.items():
        if key in apply_status or key in apply_error:
            return "needs_attention", reason
    if apply_status == "manual":
        return "needs_attention", "captcha"
    if apply_status == "failed":
        attempts = row.get("apply_attempts") or 0
        if attempts >= config.DEFAULTS["max_apply_attempts"]:
            return "failed", None
        return "queued", None  # will be retried
    if row.get("tailored_resume_path"):
        return "queued", None
    score = row.get("fit_score")
    if score is not None and review_low <= score < auto_min and not row.get("panel_approved"):
        return "review", None
    return "discovered", None


def _fingerprint(payload: dict) -> str:
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _ensure_shadow(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_shadow (
            url TEXT PRIMARY KEY,
            fingerprint TEXT,
            resume_uploaded INTEGER DEFAULT 0,
            cover_uploaded INTEGER DEFAULT 0
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------

class SyncDaemon:
    def __init__(self, sb: Supabase):
        self.sb = sb
        self.run_proc: subprocess.Popen | None = None
        self.apply_proc: subprocess.Popen | None = None
        self.stopping = False
        self.preset: dict = {}
        self._last = {"heartbeat": 0.0, "commands": 0.0, "mirror": 0.0, "mail": 0.0}

    # -- engine status -----------------------------------------------------

    def heartbeat(self) -> None:
        conn = get_connection()
        counters = {}
        try:
            counters["discovered"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            counters["scored"] = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL").fetchone()[0]
            counters["applied_today"] = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE applied_at >= ?",
                (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
            counters["attention"] = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE apply_status IN ('manual','captcha','login_issue')"
                " OR apply_error LIKE '%captcha%' OR apply_error LIKE '%login%'"
                " OR apply_error LIKE '%sso%'").fetchone()[0]
            from applypilot import budget
            b = budget.summary()
            counters["ai_used_today"] = b["used"]
            counters["ai_remaining"] = b["remaining"]
            counters["ai_percent_used"] = b["percent_used"]
        except sqlite3.Error:
            logger.exception("Counter query failed")

        stage = "idle"
        if self.run_proc and self.run_proc.poll() is None:
            stage = "pipeline"
        elif self.apply_proc and self.apply_proc.poll() is None:
            stage = "apply"

        self.sb.upsert("engine_status", [{
            "id": 1,
            "online": True,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "current_stage": stage,
            "counters": counters,
        }], on_conflict="id")

    def go_offline(self) -> None:
        try:
            self.sb.update("engine_status", "id=eq.1",
                           {"online": False, "current_stage": "idle"})
        except Exception:
            pass

    # -- commands ----------------------------------------------------------

    def poll_commands(self) -> None:
        rows = self.sb.select(
            "commands", "status=eq.pending&order=created_at.asc&limit=10")
        for cmd in rows:
            cid = cmd["id"]
            self.sb.update("commands", f"id=eq.{cid}", {"status": "picked_up"})
            try:
                self.handle_command(cmd)
                self.sb.update("commands", f"id=eq.{cid}", {
                    "status": "done",
                    "handled_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.exception("Command %s failed", cmd.get("command"))
                self.sb.update("commands", f"id=eq.{cid}", {
                    "status": "error", "error": str(e)[:500],
                    "handled_at": datetime.now(timezone.utc).isoformat(),
                })

    def handle_command(self, cmd: dict) -> None:
        name = cmd.get("command")
        payload = cmd.get("payload") or {}
        logger.info("Command: %s %s", name, payload)

        if name == "start_run":
            self.start_run(payload.get("preset_id"))
        elif name in ("stop", "pause"):
            self.stop_run()
        elif name == "resume":
            self.start_apply()
        elif name == "apply_one":
            url = payload.get("url") or payload.get("job_url")
            if not url:
                job_id = payload.get("job_id")
                if job_id:
                    rows = self.sb.select("jobs", f"id=eq.{job_id}&select=url")
                    url = rows[0]["url"] if rows else None
            if url:
                subprocess.Popen(
                    [sys.executable, "-m", "applypilot", "apply",
                     "--url", url, "--min-score", "1", "--headless"],
                )
        elif name == "mark_resolved":
            url = payload.get("url")
            if not url and payload.get("job_id"):
                rows = self.sb.select("jobs", f"id=eq.{payload['job_id']}&select=url")
                url = rows[0]["url"] if rows else None
            if url:
                conn = get_connection()
                conn.execute(
                    "UPDATE jobs SET apply_status = NULL, apply_error = NULL,"
                    " apply_attempts = 0, agent_id = NULL WHERE url = ?", (url,))
                conn.commit()
        else:
            logger.warning("Unknown command: %s", name)

    # -- run management ----------------------------------------------------

    def fetch_preset(self, preset_id: str | None) -> dict:
        if preset_id:
            rows = self.sb.select("search_presets", f"id=eq.{preset_id}")
        else:
            rows = self.sb.select("search_presets", "is_active=eq.true&limit=1")
        return rows[0] if rows else {}

    def render_searches_yaml(self, preset: dict) -> None:
        """Write the panel's preset as the engine's searches.yaml."""
        if not preset:
            return
        existing = {}
        if config.SEARCH_CONFIG_PATH.exists():
            backup = config.SEARCH_CONFIG_PATH.with_suffix(".yaml.bak")
            backup.write_text(
                config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8"),
                encoding="utf-8")
            existing = yaml.safe_load(
                config.SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}

        # Merge no-go patterns into title excludes (title-level enforcement);
        # company excludes ride along for scorer/enrichment use.
        exclude_titles = list(dict.fromkeys(
            (preset.get("exclude_titles") or [])
            + (preset.get("no_go_patterns") or [])
        ))

        data = {
            "queries": preset.get("queries") or [],
            "locations": [
                {"location": loc.get("location"), "remote": bool(loc.get("remote"))}
                for loc in (preset.get("locations") or [])
            ],
            # Preserve manually-tuned accept/reject patterns if present
            "location": existing.get("location", {}),
            "country": existing.get("country", "USA"),
            "boards": preset.get("boards") or existing.get("boards", []),
            "defaults": {
                "results_per_site": (existing.get("defaults") or {}).get("results_per_site", 100),
                "hours_old": preset.get("hours_old", 72),
                "distance": max(
                    [loc.get("radius_miles", 25) for loc in (preset.get("locations") or [])]
                    or [25]),
            },
            "exclude_titles": exclude_titles,
            "exclude_companies": preset.get("exclude_companies") or [],
            "salary_floor": preset.get("salary_floor"),
            "extra_sources": preset.get("extra_sources") or [],
        }
        config.SEARCH_CONFIG_PATH.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        logger.info("Rendered preset '%s' -> %s",
                    preset.get("name"), config.SEARCH_CONFIG_PATH)

    def preflight(self) -> list[str]:
        """Check that the engine has everything it needs to actually apply.

        Returns a list of human-readable problems (empty = good to go).
        Without these, a run would 'succeed' while submitting nothing.
        """
        problems: list[str] = []

        if not config.PROFILE_PATH.exists():
            problems.append(
                "No profile.json — run 'applypilot init' (your name, email, "
                "work authorization, salary, EEO answers)")
        else:
            try:
                profile = config.load_profile()
                email = (profile.get("personal") or {}).get("email", "")
                if not email or "example.com" in email:
                    problems.append(
                        "profile.json still has a placeholder email — employer "
                        "confirmations would go nowhere")
            except (json.JSONDecodeError, OSError) as e:
                problems.append(f"profile.json unreadable: {e}")

        if not config.RESUME_PATH.exists():
            if config.RESUME_PDF_PATH.exists():
                problems.append(
                    "resume.pdf found but resume.txt missing — the AI stages need "
                    "plain text; run 'applypilot init'")
            else:
                problems.append(
                    "No resume — run 'applypilot init' and provide your resume. "
                    "Nothing can be tailored or uploaded without it")

        if not any(os.environ.get(k) for k in
                   ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
            problems.append(
                "No LLM API key — set GEMINI_API_KEY in ~/.applypilot/.env "
                "(free at aistudio.google.com)")

        # Applying with one address while watching another means every
        # confirmation and recruiter reply is invisible.
        from applypilot.mail import check_email_alignment
        mismatch = check_email_alignment()
        if mismatch:
            problems.append(mismatch)

        return problems

    def report_blocked(self, problems: list[str]) -> None:
        """Surface setup problems in the panel instead of failing silently."""
        self.sb.upsert("engine_status", [{
            "id": 1,
            "online": True,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "current_stage": "blocked: setup incomplete",
            "counters": {"setup_problems": problems},
        }], on_conflict="id")
        for p in problems:
            logger.error("PREFLIGHT: %s", p)

    def start_run(self, preset_id: str | None) -> None:
        problems = self.preflight()
        if problems:
            self.report_blocked(problems)
            raise RuntimeError("Setup incomplete: " + " | ".join(problems))

        self.stop_run()
        self.preset = self.fetch_preset(preset_id)
        self.render_searches_yaml(self.preset)
        min_score = int(self.preset.get("min_score_auto_apply") or 6)

        self.run_proc = subprocess.Popen(
            [sys.executable, "-m", "applypilot", "run", "all",
             "--min-score", str(min_score), "--validation", "normal"],
        )
        self.start_apply()

    def start_apply(self) -> None:
        """Start the applier alongside the pipeline.

        Runs continuously so it waits for tailored resumes to appear instead
        of exiting the moment it finds an empty queue — it is launched at the
        same time as discovery, which takes many minutes to produce anything.
        The daily cap is still enforced via --limit.
        """
        if self.apply_proc and self.apply_proc.poll() is None:
            return

        min_score = int(self.preset.get("min_score_auto_apply") or 6)
        cap = int(self.preset.get("daily_apply_cap") or 100)
        workers = int(self.preset.get("apply_workers") or 2)

        # Don't re-apply past the daily cap after a mid-day restart.
        applied_today = 0
        try:
            conn = get_connection()
            applied_today = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE applied_at >= ?",
                (datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
        except sqlite3.Error:
            logger.exception("Could not read today's application count")

        remaining_today = max(0, cap - applied_today)
        if remaining_today == 0:
            logger.info("Daily apply cap of %d already reached — not starting applier", cap)
            return

        logger.info("Starting applier: %d remaining of %d daily cap, %d worker(s)",
                    remaining_today, cap, workers)
        self.apply_proc = subprocess.Popen(
            [sys.executable, "-m", "applypilot", "apply",
             "--limit", str(remaining_today),
             "--min-score", str(min_score),
             "--workers", str(workers),
             "--continuous", "--headless"],
        )

    def stop_run(self) -> None:
        for proc in (self.run_proc, self.apply_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.run_proc = None
        self.apply_proc = None

    # -- jobs mirror: local -> cloud ---------------------------------------

    def mirror_jobs(self) -> None:
        conn = get_connection()
        _ensure_shadow(conn)
        review_low = int(self.preset.get("review_band_low") or 4)
        auto_min = int(self.preset.get("min_score_auto_apply") or 6)

        local = conn.execute("SELECT * FROM jobs").fetchall()
        if not local:
            return

        # Statuses a human set in the panel — never overwrite those rows' status
        protected: set[str] = set()
        try:
            status_filter = ",".join(sorted(HUMAN_STATUSES))
            for r in self.sb.select(
                    "jobs", f"status=in.({status_filter})&select=url"):
                protected.add(r["url"])
        except httpx.HTTPError:
            logger.warning("Could not fetch protected statuses; skipping status writes")
            protected = None  # sentinel: push without status field entirely

        shadow = {r["url"]: r["fingerprint"] for r in
                  conn.execute("SELECT url, fingerprint FROM sync_shadow")}

        push: list[dict] = []
        fingerprints: list[tuple[str, str]] = []
        for row in local:
            row = dict(row)
            status, attention = derive_status(row, review_low, auto_min)
            payload = {
                "url": row["url"],
                "title": row.get("title"),
                "company": row.get("site"),
                "location": row.get("location"),
                "salary_text": row.get("salary"),
                "site": row.get("site"),
                "full_description": (row.get("full_description") or "")[:20000] or None,
                "application_url": row.get("application_url"),
                "fit_score": row.get("fit_score"),
                "score_reasoning": row.get("score_reasoning"),
                "status": status,
                "attention_reason": attention,
                "skip_reason": row.get("apply_error") if status in ("failed", "needs_attention") else None,
                "applied_at": row.get("applied_at"),
                "confirmation_status": row.get("confirmation_status"),
                "confirmed_at": row.get("confirmed_at"),
                "discovered_at": row.get("discovered_at"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if protected is None or row["url"] in (protected or set()):
                payload.pop("status", None)
                payload.pop("attention_reason", None)
            fp_payload = {k: v for k, v in payload.items() if k != "updated_at"}
            fp = _fingerprint(fp_payload)
            if shadow.get(row["url"]) != fp:
                push.append(payload)
                fingerprints.append((row["url"], fp))

        if push:
            self.sb.upsert("jobs", push, on_conflict="url")
            conn.executemany(
                "INSERT INTO sync_shadow (url, fingerprint) VALUES (?, ?) "
                "ON CONFLICT(url) DO UPDATE SET fingerprint = excluded.fingerprint",
                fingerprints)
            conn.commit()
            logger.info("Mirrored %d changed job(s)", len(push))

        self.upload_artifacts(conn)

    def upload_artifacts(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("""
            SELECT j.url, j.tailored_resume_path, j.cover_letter_path,
                   COALESCE(s.resume_uploaded, 0) AS resume_uploaded,
                   COALESCE(s.cover_uploaded, 0) AS cover_uploaded
            FROM jobs j LEFT JOIN sync_shadow s ON s.url = j.url
            WHERE (j.tailored_resume_path IS NOT NULL AND COALESCE(s.resume_uploaded, 0) = 0)
               OR (j.cover_letter_path IS NOT NULL AND COALESCE(s.cover_uploaded, 0) = 0)
            LIMIT 25
        """).fetchall()

        for row in rows:
            url = row["url"]
            slug = hashlib.sha1(url.encode()).hexdigest()[:16]
            patch: dict = {}

            if row["tailored_resume_path"] and not row["resume_uploaded"]:
                pdf = Path(row["tailored_resume_path"]).with_suffix(".pdf")
                if pdf.exists() and self.sb.upload(
                        "artifacts", f"resumes/{slug}.pdf", pdf.read_bytes()):
                    patch["resume_pdf_path"] = f"resumes/{slug}.pdf"
                    conn.execute(
                        "INSERT INTO sync_shadow (url, resume_uploaded) VALUES (?, 1) "
                        "ON CONFLICT(url) DO UPDATE SET resume_uploaded = 1", (url,))

            if row["cover_letter_path"] and not row["cover_uploaded"]:
                pdf = Path(row["cover_letter_path"]).with_suffix(".pdf")
                if pdf.exists() and self.sb.upload(
                        "artifacts", f"covers/{slug}.pdf", pdf.read_bytes()):
                    patch["cover_letter_path"] = f"covers/{slug}.pdf"
                    conn.execute(
                        "INSERT INTO sync_shadow (url, cover_uploaded) VALUES (?, 1) "
                        "ON CONFLICT(url) DO UPDATE SET cover_uploaded = 1", (url,))

            if patch:
                self.sb.update("jobs", f"url=eq.{httpx.QueryParams({'u': url})['u']}", patch)
        conn.commit()

    # -- human decisions: cloud -> local -----------------------------------

    def pull_decisions(self) -> None:
        conn = get_connection()
        auto_min = int(self.preset.get("min_score_auto_apply") or 6)

        # Review approvals: panel moved a review-band job to 'queued'
        for r in self.sb.select(
                "jobs", "status=eq.queued&select=url,fit_score"):
            local = conn.execute(
                "SELECT fit_score, panel_approved, applied_at FROM jobs WHERE url = ?",
                (r["url"],)).fetchone()
            if not local or local["applied_at"] or local["panel_approved"]:
                continue
            score = local["fit_score"]
            if score is not None and score < auto_min:
                conn.execute("""
                    UPDATE jobs SET panel_approved = 1,
                                   original_fit_score = fit_score,
                                   fit_score = ?
                    WHERE url = ?
                """, (auto_min, r["url"]))
                logger.info("Panel approved: %s (score %s -> %s)",
                            r["url"][:60], score, auto_min)

        # Rejections: panel says never apply
        for r in self.sb.select("jobs", "status=eq.rejected_by_me&select=url"):
            conn.execute("""
                UPDATE jobs SET apply_status = 'skipped_by_user',
                               apply_attempts = 99, agent_id = NULL
                WHERE url = ? AND applied_at IS NULL
                  AND COALESCE(apply_status, '') != 'skipped_by_user'
            """, (r["url"],))
        conn.commit()

    # -- question bank two-way sync ----------------------------------------

    def sync_questions(self) -> None:
        conn = get_connection()
        questions.ensure_table(conn)

        # Pull: panel-edited answers/confidence always win locally
        cloud = {q["question_normalized"]: q
                 for q in self.sb.select("questions", "select=*")}
        now = datetime.now(timezone.utc).isoformat()
        for q_norm, q in cloud.items():
            conn.execute("""
                INSERT INTO questions
                    (question_normalized, question_raw, answer, answer_type,
                     confidence, times_used, source_job_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_normalized) DO UPDATE SET
                    answer = excluded.answer,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
            """, (q_norm, q.get("question_raw") or q_norm, q.get("answer") or "",
                  q.get("answer_type") or "text", q.get("confidence") or "low",
                  q.get("times_used") or 0, q.get("source_job_url"), now, now))
        conn.commit()

        # Push: new local questions + usage counters
        push = []
        for row in conn.execute("SELECT * FROM questions").fetchall():
            row = dict(row)
            c = cloud.get(row["question_normalized"])
            if c is None:
                push.append({
                    "question_normalized": row["question_normalized"],
                    "question_raw": row["question_raw"],
                    "answer": row["answer"],
                    "answer_type": row.get("answer_type") or "text",
                    "confidence": row.get("confidence") or "low",
                    "times_used": row.get("times_used") or 0,
                    "source_job_url": row.get("source_job_url"),
                    "last_used_at": row.get("last_used_at"),
                })
            elif (row.get("times_used") or 0) > (c.get("times_used") or 0):
                self.sb.update(
                    "questions",
                    f"question_normalized=eq.{httpx.QueryParams({'q': row['question_normalized']})['q']}",
                    {"times_used": row["times_used"],
                     "last_used_at": row.get("last_used_at")})
        if push:
            self.sb.upsert("questions", push, on_conflict="question_normalized")
            logger.info("Pushed %d new question(s) to panel", len(push))

    # -- scheduled autonomous runs -----------------------------------------

    def check_schedule(self) -> None:
        """Start a run on a fixed interval, with no button press.

        The active preset's `auto_run_hours` (0 = manual only) makes the
        system genuinely hands-off: it keeps finding and applying while you
        do something else, the same cadence the paid tools advertise.
        Skipped while a run is already in flight or setup is incomplete.
        """
        hours = self.preset.get("auto_run_hours") or 0
        try:
            hours = float(hours)
        except (TypeError, ValueError):
            return
        if hours <= 0:
            return

        running = any(p and p.poll() is None
                      for p in (self.run_proc, self.apply_proc))
        if running:
            return

        marker = config.APP_DIR / ".last_auto_run"
        if marker.exists():
            try:
                last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).total_seconds() < hours * 3600:
                    return
            except (ValueError, OSError):
                pass

        problems = self.preflight()
        if problems:
            self.report_blocked(problems)
            return

        logger.info("Scheduled run starting (every %sh)", hours)
        marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        try:
            self.start_run(self.preset.get("id"))
        except Exception:
            logger.exception("Scheduled run failed to start")

    # -- interview prep + follow-ups ---------------------------------------

    def sync_coach(self) -> None:
        """Generate prep packs and follow-up drafts, then push them up."""
        from applypilot import coach

        try:
            result = coach.run(limit=10)
        except Exception:
            logger.exception("Coach generation failed")
            return

        if result["preps"] or result["followups"]:
            logger.info("Coach: %d prep pack(s), %d follow-up draft(s)",
                        result["preps"], result["followups"])

        conn = get_connection()
        coach.ensure_tables(conn)

        def _job_id(url: str) -> str | None:
            try:
                rows = self.sb.select(
                    "jobs", f"url=eq.{httpx.QueryParams({'u': url})['u']}&select=id")
                return rows[0]["id"] if rows else None
            except httpx.HTTPError:
                return None

        preps = []
        for row in conn.execute("SELECT * FROM interview_prep"):
            row = dict(row)
            preps.append({
                "job_url": row["job_url"],
                "job_id": _job_id(row["job_url"]),
                "company": row.get("company"),
                "title": row.get("title"),
                "likely_questions": row.get("likely_questions"),
                "talking_points": row.get("talking_points"),
                "questions_to_ask": row.get("questions_to_ask"),
                "company_notes": row.get("company_notes"),
            })
        if preps:
            self.sb.upsert("interview_prep", preps, on_conflict="job_url")

        drafts = []
        for row in conn.execute("SELECT * FROM followups WHERE status = 'draft'"):
            row = dict(row)
            drafts.append({
                "job_url": row["job_url"],
                "job_id": _job_id(row["job_url"]),
                "company": row.get("company"),
                "title": row.get("title"),
                "to_address": row.get("to_address"),
                "subject": row.get("subject"),
                "body": row.get("body"),
                "days_since": row.get("days_since"),
                "status": "draft",
            })
        if drafts:
            self.sb.upsert("followups", drafts, on_conflict="job_url")

        # Pull back statuses set in the panel (sent / dismissed)
        try:
            for r in self.sb.select("followups", "status=neq.draft&select=job_url,status"):
                conn.execute("UPDATE followups SET status = ? WHERE job_url = ?",
                             (r["status"], r["job_url"]))
            conn.commit()
        except httpx.HTTPError:
            pass

    # -- profile + resume from the panel ------------------------------------

    def sync_profile(self) -> None:
        """Pull profile fields and resume text edited in the panel.

        The panel is the editing surface; profile.json on disk stays the
        engine's source of truth. Only fields actually present in the cloud
        row are applied, so a partially-filled panel form never blanks out
        data set during `applypilot init`.
        """
        try:
            rows = self.sb.select("profile", "limit=1")
        except httpx.HTTPError:
            return
        if not rows:
            return

        row = rows[0]
        remote_updated = row.get("updated_at")

        # Skip when nothing changed since the last pull
        marker = config.APP_DIR / ".profile_sync"
        if remote_updated and marker.exists():
            if marker.read_text(encoding="utf-8").strip() == str(remote_updated):
                return

        if not config.PROFILE_PATH.exists():
            logger.warning("Panel profile received but no local profile.json — "
                           "run 'applypilot init' first so defaults exist")
            return

        profile = config.load_profile()
        changed = []

        # Map flat panel columns onto the nested profile structure
        field_map = {
            "full_name": ("personal", "full_name"),
            "preferred_name": ("personal", "preferred_name"),
            "email": ("personal", "email"),
            "phone": ("personal", "phone"),
            "address": ("personal", "address"),
            "city": ("personal", "city"),
            "province_state": ("personal", "province_state"),
            "country": ("personal", "country"),
            "postal_code": ("personal", "postal_code"),
            "linkedin_url": ("personal", "linkedin_url"),
            "github_url": ("personal", "github_url"),
            "portfolio_url": ("personal", "portfolio_url"),
            "salary_expectation": ("compensation", "salary_expectation"),
            "salary_range_min": ("compensation", "salary_range_min"),
            "salary_range_max": ("compensation", "salary_range_max"),
            "years_of_experience": ("experience", "years_of_experience_total"),
            "education_level": ("experience", "education_level"),
            "target_role": ("experience", "target_role"),
            "legally_authorized": ("work_authorization", "legally_authorized_to_work"),
            "require_sponsorship": ("work_authorization", "require_sponsorship"),
            "gender": ("eeo_voluntary", "gender"),
            "race_ethnicity": ("eeo_voluntary", "race_ethnicity"),
            "veteran_status": ("eeo_voluntary", "veteran_status"),
            "disability_status": ("eeo_voluntary", "disability_status"),
        }

        for column, (section, key) in field_map.items():
            value = row.get(column)
            if value in (None, ""):
                continue
            value = str(value)
            profile.setdefault(section, {})
            if profile[section].get(key) != value:
                profile[section][key] = value
                changed.append(key)

        if changed:
            backup = config.PROFILE_PATH.with_suffix(".json.bak")
            backup.write_text(config.PROFILE_PATH.read_text(encoding="utf-8"),
                              encoding="utf-8")
            config.PROFILE_PATH.write_text(
                json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info("Profile updated from panel: %s", ", ".join(changed))

        # Resume FILE uploaded in the panel takes priority over pasted text:
        # it is the real document, and it also becomes what gets uploaded to
        # employers, so formatting survives.
        self._pull_resume_file(row)

        # Resume text edited in the panel
        resume_text = row.get("resume_text")
        if resume_text and resume_text.strip():
            current = (config.RESUME_PATH.read_text(encoding="utf-8")
                       if config.RESUME_PATH.exists() else "")
            if current.strip() != resume_text.strip():
                if current:
                    config.RESUME_PATH.with_suffix(".txt.bak").write_text(
                        current, encoding="utf-8")
                config.RESUME_PATH.write_text(resume_text, encoding="utf-8")
                logger.info("Resume updated from panel (%d chars)", len(resume_text))

        if remote_updated:
            marker.write_text(str(remote_updated), encoding="utf-8")

    def _pull_resume_file(self, row: dict) -> None:
        """Download a resume uploaded in the panel, extract its text, install it.

        The original file is kept as the document uploaded to employers, so
        the user's real formatting survives. The extracted text is what the
        scorer reads and the tailoring stage rewrites per job.
        """
        storage_path = row.get("resume_file_path")
        if not storage_path:
            return

        marker = config.APP_DIR / ".resume_file_sync"
        stamp = f"{storage_path}|{row.get('resume_uploaded_at') or ''}"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == stamp:
            return

        try:
            r = self.sb.client.get(f"{self.sb.storage}/object/artifacts/{storage_path}")
            r.raise_for_status()
            data = r.content
        except httpx.HTTPError as e:
            logger.warning("Could not download uploaded resume (%s): %s", storage_path, e)
            return

        suffix = Path(storage_path).suffix.lower() or ".pdf"
        dest = config.APP_DIR / f"resume_upload{suffix}"
        dest.write_bytes(data)

        from applypilot.resume import ResumeParseError, analyze, extract_text
        try:
            text = extract_text(dest)
        except ResumeParseError as e:
            logger.error("Uploaded resume could not be parsed: %s", e)
            try:
                self.sb.update("profile", "id=eq.1",
                               {"resume_parse_error": str(e), "resume_text": None})
            except httpx.HTTPError:
                pass
            return

        # Back up whatever was there before replacing it
        if config.RESUME_PATH.exists():
            config.RESUME_PATH.with_suffix(".txt.bak").write_text(
                config.RESUME_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        config.RESUME_PATH.write_text(text, encoding="utf-8")

        # A PDF upload doubles as the document sent to employers
        if suffix == ".pdf":
            config.RESUME_PDF_PATH.write_bytes(data)

        report = analyze(text)
        try:
            self.sb.update("profile", "id=eq.1", {
                "resume_text": text,
                "resume_parse_error": None,
                "resume_word_count": report["words"],
                "resume_warnings": "\n".join(report["warnings"]) or None,
            })
        except httpx.HTTPError:
            pass

        marker.write_text(stamp, encoding="utf-8")
        logger.info("Installed uploaded resume: %s (%d words)%s",
                    storage_path, report["words"],
                    "" if report["looks_good"] else f" — {len(report['warnings'])} warning(s)")

    def push_profile(self) -> None:
        """Seed the panel's profile row from profile.json on first run."""
        if not config.PROFILE_PATH.exists():
            return
        try:
            if self.sb.select("profile", "limit=1"):
                return  # panel already has a row; it owns edits from here
        except httpx.HTTPError:
            return

        p = config.load_profile()
        personal = p.get("personal") or {}
        comp = p.get("compensation") or {}
        exp = p.get("experience") or {}
        auth = p.get("work_authorization") or {}
        eeo = p.get("eeo_voluntary") or {}

        resume_text = (config.RESUME_PATH.read_text(encoding="utf-8")
                       if config.RESUME_PATH.exists() else None)

        self.sb.upsert("profile", [{
            "id": 1,
            "full_name": personal.get("full_name"),
            "preferred_name": personal.get("preferred_name"),
            "email": personal.get("email"),
            "phone": personal.get("phone"),
            "address": personal.get("address"),
            "city": personal.get("city"),
            "province_state": personal.get("province_state"),
            "country": personal.get("country"),
            "postal_code": personal.get("postal_code"),
            "linkedin_url": personal.get("linkedin_url"),
            "github_url": personal.get("github_url"),
            "portfolio_url": personal.get("portfolio_url"),
            "salary_expectation": comp.get("salary_expectation"),
            "salary_range_min": comp.get("salary_range_min"),
            "salary_range_max": comp.get("salary_range_max"),
            "years_of_experience": exp.get("years_of_experience_total"),
            "education_level": exp.get("education_level"),
            "target_role": exp.get("target_role"),
            "legally_authorized": auth.get("legally_authorized_to_work"),
            "require_sponsorship": auth.get("require_sponsorship"),
            "gender": eeo.get("gender"),
            "race_ethnicity": eeo.get("race_ethnicity"),
            "veteran_status": eeo.get("veteran_status"),
            "disability_status": eeo.get("disability_status"),
            "resume_text": resume_text,
        }], on_conflict="id")
        logger.info("Seeded panel profile from profile.json")

    # -- email ingest + push ------------------------------------------------

    def sync_mail(self) -> None:
        """Fetch and classify new mail, then push it to the panel's Inbox."""
        from applypilot import mail as mail_mod

        if not os.environ.get("MAIL_APP_PASSWORD"):
            return

        stats = mail_mod.fetch_and_store(lookback_days=14, limit=100)
        if stats.get("warning"):
            logger.error("MAIL CONFIG: %s", stats["warning"])
        if stats.get("error"):
            logger.warning("Mail ingest: %s", stats["error"])
            return

        try:
            mail_mod.reconcile_applications()
        except Exception:
            logger.exception("Application reconciliation failed")
        if stats["stored"]:
            logger.info("Mail: %d new job-related (%s)",
                        stats["stored"], stats["by_category"])

        conn = get_connection()
        mail_mod.ensure_table(conn)

        # Map local job URLs to cloud job UUIDs so emails link correctly
        url_to_id: dict[str, str] = {}
        linked_urls = [r[0] for r in conn.execute(
            "SELECT DISTINCT job_url FROM emails WHERE job_url IS NOT NULL")]
        for url in linked_urls:
            try:
                rows = self.sb.select(
                    "jobs",
                    f"url=eq.{httpx.QueryParams({'u': url})['u']}&select=id")
                if rows:
                    url_to_id[url] = rows[0]["id"]
            except httpx.HTTPError:
                continue

        existing = {e["gmail_id"] for e in
                    self.sb.select("emails", "select=gmail_id")}

        push = []
        for row in conn.execute(
                "SELECT * FROM emails ORDER BY received_at DESC LIMIT 500"):
            row = dict(row)
            if row["message_id"] in existing:
                continue
            push.append({
                "gmail_id": row["message_id"],
                "from_address": row.get("from_address"),
                "subject": row.get("subject"),
                "snippet": row.get("snippet"),
                "body_text": (row.get("body_text") or "")[:15000] or None,
                "received_at": row.get("received_at"),
                "category": row.get("category"),
                "classified_by": row.get("classified_by"),
                "job_id": url_to_id.get(row.get("job_url")),
                "is_read": bool(row.get("is_read")),
            })

        if push:
            self.sb.upsert("emails", push, on_conflict="gmail_id")
            logger.info("Pushed %d email(s) to panel", len(push))

    def sync_target_companies(self) -> None:
        """Write the panel's company registry where ATS discovery reads it."""
        try:
            rows = self.sb.select("target_companies", "enabled=eq.true&select=*")
        except httpx.HTTPError:
            return

        path = config.APP_DIR / "target_companies.yaml"
        payload = {"companies": [
            {"company": r.get("company"), "ats": r.get("ats"),
             "board_token": r.get("board_token"), "enabled": True}
            for r in rows
        ]}
        new_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        if not path.exists() or path.read_text(encoding="utf-8") != new_text:
            path.write_text(new_text, encoding="utf-8")
            logger.info("Updated target_companies.yaml (%d companies)", len(rows))

    # -- main loop ---------------------------------------------------------

    def loop(self, once: bool = False) -> None:
        # Load the active preset so thresholds are right from the start, and
        # seed the panel's profile row if this is the first connection.
        try:
            self.preset = self.fetch_preset(None)
            self.push_profile()
        except httpx.HTTPError:
            logger.warning("Could not reach Supabase on startup (will retry)")
        except Exception:
            logger.exception("Startup sync failed (continuing)")

        logger.info("Sync daemon started (heartbeat %ss, commands %ss, mirror %ss)",
                    HEARTBEAT_INTERVAL, COMMAND_INTERVAL, MIRROR_INTERVAL)

        while not self.stopping:
            now = time.monotonic()
            try:
                if now - self._last["heartbeat"] >= HEARTBEAT_INTERVAL:
                    self.heartbeat()
                    self._last["heartbeat"] = now
                if now - self._last["commands"] >= COMMAND_INTERVAL:
                    self.poll_commands()
                    self._last["commands"] = now
                if now - self._last["mirror"] >= MIRROR_INTERVAL:
                    self.sync_profile()
                    self.pull_decisions()
                    self.mirror_jobs()
                    self.sync_questions()
                    self.sync_target_companies()
                    self._last["mirror"] = now
                if now - self._last["mail"] >= MAIL_INTERVAL:
                    self.sync_mail()
                    self.sync_coach()
                    self.check_schedule()
                    self._last["mail"] = now
            except httpx.HTTPError as e:
                logger.warning("Supabase unreachable (%s) — retrying", e)
            except Exception:
                logger.exception("Sync cycle error — continuing")

            if once:
                break
            time.sleep(5)

        self.stop_run()
        self.go_offline()
        logger.info("Sync daemon stopped")


def main(once: bool = False) -> None:
    """Entry point for `applypilot sync`."""
    sb = connect()
    daemon = SyncDaemon(sb)

    def _stop(*_):
        daemon.stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    daemon.loop(once=once)
