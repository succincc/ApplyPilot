# JobPilot

**A no-AI, config-driven job application bot.** It discovers jobs from public
job-board APIs, ranks them against your preferences with transparent rules,
and fills out external application forms for you — pausing for a human only
when a site puts up a CAPTCHA or login wall.

Built in Python. Runs natively on Windows (and macOS/Linux). **No AI, no API
keys, no per-application cost.**

---

## Why this design (read this first)

You asked for a bot that scrapes LinkedIn/Indeed/etc., bypasses CAPTCHAs, and
auto-applies to thousands of jobs with no AI. I built the useful, sustainable
version of that and made three deliberate calls you should know about:

1. **No CAPTCHA bypassing.** Defeating a site's anti-bot check is exactly what
   gets accounts *permanently banned*, and it's legally risky. JobPilot instead
   **detects** a CAPTCHA/login and **pauses** so you solve it in the visible
   browser, then it resumes. This is more reliable than any solver (they break
   constantly) and keeps you in good standing.

2. **Discovery via public APIs, not scraping.** LinkedIn and Indeed forbid
   scraping and automated applying in their terms; doing it at scale gets your
   account banned and can cross legal lines. JobPilot pulls from **documented,
   free, public endpoints** — Greenhouse, Lever, Ashby, Remotive, USAJOBS —
   which is *also where "external" company applications actually live*. You get
   real breadth without torching your accounts. (See
   [Adding LinkedIn/Indeed](#can-i-add-linkedin--indeed) below.)

3. **No AI, by design.** Matching is keyword/location/salary/recency scoring you
   can read and tune. Question answering ("why do you want this job?") is a
   deterministic template bank *you* control. Zero LLM calls, zero credits.

4. **Human-approval by default.** Out of the box the bot fills every field and
   **stops at Submit** so you can eyeball it. Flip one setting to let it
   auto-submit high-confidence, high-scoring applications.

---

## Install

```bash
pip install -e .            # from this directory
playwright install chromium # one-time browser download (for the apply step)
```

Python 3.10+.

## Quick start

```bash
jobpilot init        # writes config.yaml, profile.yaml, answers.yaml here
#   -> edit those three files (details below)
jobpilot run         # discover + match
jobpilot review      # see what's queued and what needs your input
jobpilot apply       # open the browser and fill applications
```

`jobpilot run --apply` chains all of it.

---

## The three files you edit

| File | What it holds |
|------|---------------|
| **config.yaml** | Your "preset options": keywords, preferred **location**, **radius in miles** from an anchor, **salary** floor/target, employment types, and which discovery sources to poll. |
| **profile.yaml** | Your data used to fill forms: name, contact, links, work authorization, resume file path, EEO answers. |
| **answers.yaml** | The deterministic answer bank: substring rules → templated answers for free-text and dropdown questions. |

Every option is commented in the `*.example.yaml` files.

### Preferences that adjust per platform

You set your preferences once; JobPilot applies them everywhere:

- **Preferred location** — a list of acceptable locations, plus a remote toggle.
- **Radius (miles)** — `location.radius_miles` from `location.anchor`. (Textual
  location matching works out of the box; true distance filtering needs a
  geocoder — see `matching.py`, it's stubbed to fall back to text matching.)
- **Salary** — `min_salary` is a hard floor; `target_salary` feeds ranking.
- **Employment type**, **posting age**, **exclude keywords** — all hard filters.

The apply step adapts to each platform's form structure automatically
(Greenhouse and Lever have first-class handling; unknown ATS forms fall back to
the generic label-based filler).

---

## How it works

```
discover  →  match  →  (review)  →  apply
  │            │           │           │
public APIs  rule-based  your        Playwright fills the form;
(no scrape)  0–100 score approval    human solves any CAPTCHA/login;
                         (optional)  submits only if you allowed it
```

- **discover** — polls each enabled source, dedupes, stores in SQLite
  (`~/.jobpilot/jobpilot.db`).
- **match** — `matching.evaluate()` applies hard filters then scores 0–100 with
  a full plain-English reason trail. Jobs ≥ `min_score` become `queued`.
- **apply** — opens a persistent Chromium profile (log in once, sessions stick),
  navigates each job, fills fields from `profile.yaml`, answers questions from
  `answers.yaml`, and submits only when `auto_submit` is on **and** the score is
  ≥ `auto_approve_score` **and** nothing was flagged. Everything else parks as
  `needs_review` with the typing already done.

### Answering "why do you want this job?"

`answers.yaml` rules match on substrings and fill placeholders from your profile
and the job:

```yaml
- match_any: ["why do you want", "why are you interested"]
  answer: >-
    I'm excited about the {role} role at {company} because it's a strong match
    for my background as a {current_title} with {years_experience} years...
```

If a question matches nothing, the field is left blank and the job is flagged
for review — the bot never invents an answer.

---

## Commands

```
jobpilot init                 Create config/profile/answers from templates
jobpilot discover             Pull jobs from enabled sources
jobpilot match                Score new jobs (no AI)
jobpilot review               List jobs needing your decision
jobpilot review --approve ID  Approve one job for submission
jobpilot review --approve-all Approve everything in the review queue
jobpilot apply                Fill/submit queued + approved jobs
jobpilot apply --dry-run      Fill forms, never click Submit
jobpilot run [--apply]        discover + match (+ apply)
jobpilot status               Counts by stage
```

---

## Discovery sources

| Source | Auth | Notes |
|--------|------|-------|
| **Greenhouse** | none | Add company board tokens in config. Public job-board API. |
| **Lever** | none | Add company slugs. Public postings API. |
| **Remotive** | none | Large remote-jobs aggregator, keyword search. |
| **USAJOBS** | free key | US federal jobs; needs email + API key. |

Add more by dropping a `fetch(prefs, cfg)` function in `discovery/` and
registering it in `discovery/__init__.py:SOURCES`.

### Can I add LinkedIn / Indeed?

Their terms prohibit automated scraping and applying, and doing it at volume
reliably gets accounts banned — so it's not built in. If you have **official,
authorized API access** (e.g. an Indeed Publisher account), you can add a
compliant `fetch()` source the same way as the others. JobPilot's architecture
supports it; it just won't ship code that violates a site's terms.

---

## Safety & good-citizen defaults

- `auto_submit: false` — review before anything is sent.
- `max_per_run` and a randomized `delay_between_seconds` between applications.
- Human-in-the-loop for CAPTCHAs/logins; **no bypass**.
- All personal data stays local in your `profile.yaml` and the SQLite DB.

## Development

```bash
pip install -e ".[dev]"
pytest            # matching / answers / discovery-parsing tests (no network)
ruff check src
```

## License

MIT.
