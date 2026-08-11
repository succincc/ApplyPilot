"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


BATCH_SCORE_PROMPT = """You are a job fit evaluator. You will be given ONE candidate resume and a NUMBERED LIST of job postings. Score how well the candidate fits each job.

SCORING CRITERIA:
- 9-10: Perfect match. Direct experience in nearly all required skills.
- 7-8: Strong match. Most required skills present, minor gaps easily bridged.
- 5-6: Moderate match. Some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, substantial ramp-up needed.
- 1-2: Poor match. Different field or wrong experience level entirely.

IMPORTANT FACTORS:
- Weight technical skills heavily (languages, frameworks, tools)
- Credit transferable experience (automation, scripting, API work)
- Be realistic about seniority and years of experience

Return ONLY a JSON array, one object per job, in the SAME ORDER as given.
Every job must appear exactly once. Each object has exactly these keys:
  "n": the job's number as given
  "score": integer 1-10
  "keywords": comma-separated ATS keywords from that posting matching the candidate
  "reasoning": one or two sentences

No text before or after the JSON array."""


def _parse_batch_response(response: str, count: int) -> list[dict] | None:
    """Parse a batched scoring response into per-job results.

    Returns None when the response can't be trusted, so the caller can fall
    back to scoring the batch one job at a time. A partially-parsed batch is
    never returned — a misaligned score would attach the wrong reasoning to
    the wrong job and silently mis-rank the whole queue.
    """
    if not response:
        return None

    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        array = re.search(r"\[.*\]", response, re.DOTALL)
        candidate = array.group(0) if array else None
    if candidate is None:
        return None

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None

    by_index: dict[int, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("n"))
            score = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= count:
            continue
        by_index[n] = {
            "score": max(1, min(10, score)),
            "keywords": str(item.get("keywords") or "").strip(),
            "reasoning": str(item.get("reasoning") or "").strip(),
        }

    # Require every job to come back; anything missing means we can't trust
    # the alignment of the ones that did.
    if len(by_index) != count:
        log.warning("Batch scoring returned %d of %d jobs — falling back",
                    len(by_index), count)
        return None

    return [by_index[i] for i in range(1, count + 1)]


def score_jobs_batch(resume_text: str, jobs: list[dict],
                     desc_chars: int = 1800) -> list[dict]:
    """Score several jobs in a single LLM request.

    This is the main throughput lever. Scoring one job per request spends
    roughly one free-tier request per discovered job, which caps daily volume
    long before the browser does. Batching sends the resume once and amortizes
    it across many postings, cutting scoring requests by an order of magnitude
    and freeing that quota for tailoring — the stage that actually produces
    applications.

    Falls back to individual scoring if the batch response can't be parsed
    reliably, so a malformed response costs accuracy nowhere.
    """
    if not jobs:
        return []

    parts = []
    for i, job in enumerate(jobs, start=1):
        description = (job.get("full_description") or job.get("description") or "")
        parts.append(
            f"--- JOB {i} ---\n"
            f"TITLE: {job.get('title')}\n"
            f"COMPANY: {job.get('site')}\n"
            f"LOCATION: {job.get('location', 'N/A')}\n"
            f"DESCRIPTION:\n{description[:desc_chars]}"
        )

    messages = [
        {"role": "system", "content": BATCH_SCORE_PROMPT},
        {"role": "user", "content":
            f"RESUME:\n{resume_text}\n\n"
            f"=== {len(jobs)} JOB POSTINGS TO SCORE ===\n\n" + "\n\n".join(parts)},
    ]

    client = get_client()
    response = client.chat(
        messages,
        max_tokens=min(8192, 220 * len(jobs) + 512),
        temperature=0.2,
        stage="score",
    )

    parsed = _parse_batch_response(response, len(jobs))
    if parsed is not None:
        return parsed

    log.info("Falling back to individual scoring for %d job(s)", len(jobs))
    return [score_job(resume_text, job) for job in jobs]


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    from applypilot.budget import BudgetExhausted

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2, stage="score")
        return _parse_score_response(response)
    except BudgetExhausted:
        # Must propagate: writing a 0 here would permanently mark the job as
        # a bad fit when the only problem is that today's quota ran out.
        raise
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    # Free triage first: reject unambiguous mismatches without spending a
    # request on them. Rejected jobs get a real score of 1 so they are
    # excluded from the apply queue but still auditable in the panel.
    from applypilot.filters import prescore

    kept: list[dict] = []
    prefiltered: list[tuple[str, str]] = []
    for job in jobs:
        worth, reason = prescore(job)
        if worth:
            kept.append(job)
        else:
            prefiltered.append((job["url"], reason or "prefiltered"))

    if prefiltered:
        now_pf = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "UPDATE jobs SET fit_score = 1, score_reasoning = ?, scored_at = ? WHERE url = ?",
            [(f"prefiltered: {reason}", now_pf, url) for url, reason in prefiltered],
        )
        conn.commit()
        log.info("Prefiltered %d job(s) with zero API cost (%d remain to score)",
                 len(prefiltered), len(kept))

    jobs = kept
    if not jobs:
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": [],
                "prefiltered": len(prefiltered), "budget_stopped": False}

    log.info("Scoring %d jobs in batches...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    import os

    from applypilot.budget import BudgetExhausted

    try:
        batch_size = max(1, int(os.environ.get("SCORE_BATCH_SIZE", "15")))
    except ValueError:
        batch_size = 15

    budget_stopped = False
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        try:
            batch_results = (
                score_jobs_batch(resume_text, batch) if len(batch) > 1
                else [score_job(resume_text, batch[0])]
            )
        except BudgetExhausted as e:
            # Stop cleanly and keep whatever was scored so far. Unscored jobs
            # stay unscored and are picked up on the next run.
            log.warning("%s", e)
            log.warning(
                "Stopping scoring after %d of %d jobs. The rest keep their "
                "unscored state and will be scored on the next run.",
                completed, len(jobs))
            budget_stopped = True
            break
        except Exception:
            log.exception("Batch scoring failed for %d jobs — skipping batch", len(batch))
            errors += len(batch)
            continue

        for job, result in zip(batch, batch_results):
            result["url"] = job["url"]
            completed += 1
            if result["score"] == 0:
                errors += 1
            results.append(result)

        log.info(
            "[%d/%d] batch of %d scored  (avg %.1f)",
            completed, len(jobs), len(batch),
            sum(r["score"] for r in batch_results) / len(batch_results),
        )

    # Write scores to DB
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (r["score"], f"{r['keywords']}\n{r['reasoning']}", now, r["url"]),
        )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
        "budget_stopped": budget_stopped,
        "prefiltered": len(prefiltered),
    }
