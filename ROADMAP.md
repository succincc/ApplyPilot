# ApplyPilot Command Center — Master Roadmap

**Goal: a real career job within months. Method: volume with a floor, full automation, zero recurring cost.**

This document is the blueprint for turning ApplyPilot into a complete, private, self-hosted
job-hunting machine: a web control panel (built with Lovable), a free cloud bridge (Supabase),
and the autonomous engine that already lives in this repo.

Companion documents:

- **[docs/LOVABLE_PROMPTS.md](docs/LOVABLE_PROMPTS.md)** — the exact prompts to paste into Lovable, in order.
- **[docs/BRIDGE_SPEC.md](docs/BRIDGE_SPEC.md)** — the engineering contract between the panel, Supabase, and the engine.

---

## 0. Read this first: what you already have

You do not need Lovable to build a job-application bot. **This repository already is one.**
ApplyPilot v0.3.0 is a working 6-stage autonomous pipeline:

| Stage | Status today |
|---|---|
| Discover (Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy + 48 Workday portals + 30 direct career sites) | ✅ Working |
| Enrich (full job description extraction: JSON-LD → CSS → AI fallback) | ✅ Working |
| Score (Gemini rates every job 1–10 against your resume) | ✅ Working |
| Tailor (per-job resume rewrite, facts preserved) | ✅ Working |
| Cover letter (per-job) | ✅ Working |
| Auto-apply (real Chrome, form navigation, uploads, screening questions, submission) | ✅ Working |

What's **missing** — and what this roadmap adds:

1. **A private web hub** ("Mission Control") instead of a CLI — filters, dropdowns, one Start button.
2. **A question memory bank** — remember every screening answer ("Are you a veteran?" → saved once, reused forever), flag new/uncertain questions for your review.
3. **Email intelligence** — pull your Gmail, auto-classify into Rejected / Interview / Offer / Action-needed, and auto-advance the pipeline board.
4. **An attention queue** — when the bot hits a CAPTCHA, a login wall, or a question it can't answer, it parks the application and surfaces it to you instead of silently failing.
5. **More discovery sources** — free ATS APIs (Greenhouse, Lever, Ashby), USAJobs, Adzuna, remote boards, so you're not dependent on scraping alone.
6. **Analytics** — which sources, scores, and titles actually produce interviews, so volume gets smarter over time.

---

## 1. Architecture: why it's a hybrid (and why anything else fails)

**Hard truth Lovable's marketing won't tell you:** a Lovable app is a React frontend on
Supabase. It runs in a browser tab and short-lived edge functions. It **cannot** drive a Chrome
instance through a Workday login, and it never will. Any "fully in-Lovable" auto-applier is
broken by design. Also, applying from a datacenter IP (any cloud host) gets bot-flagged far
more than applying from your home IP.

So the design is three parts, each doing the only thing it's good at:

```
┌─────────────────────────┐        ┌──────────────────────────┐        ┌─────────────────────────────┐
│  MISSION CONTROL (web)  │        │   BRIDGE (Supabase free)  │        │   ENGINE (this repo, your PC) │
│  Built with Lovable     │◄──────►│  Postgres + Storage +     │◄──────►│  Discovery · Scoring ·        │
│  React, private, 1 user │ realtime│  Auth + Realtime          │  sync  │  Tailoring · Auto-apply ·     │
│  Filters, Start button, │        │  jobs / commands /        │  loop  │  Gmail ingest                 │
│  Kanban, Inbox, Q-bank  │        │  questions / emails       │        │  Runs from YOUR home IP       │
└─────────────────────────┘        └──────────────────────────┘        └─────────────────────────────┘
```

- **Mission Control** (Lovable): everything you see and touch. Reads/writes Supabase only.
  It never scrapes, never applies, never calls job boards.
- **Bridge** (Supabase free tier): the shared database, file storage for tailored
  resumes/cover letters, the command queue ("start run with these filters"), and realtime
  updates so the panel updates live while the engine works.
- **Engine** (this repo): the only component that touches job sites. It polls the bridge for
  commands, runs the pipeline, and streams results back. Your laptop being on is the "server."

The panel works from your phone anywhere; the engine does the dirty work from home. If your
PC is off, jobs queue up and run when it's back. That is the honest zero-cost architecture —
it's also *more* effective than a cloud bot, because home IP + real Chrome profile is the
lowest-detection setup that exists.

---

## 2. The phases

Build in this order. Each phase is independently useful — you get value after every phase,
not only at the end.

### Phase 0 — Foundation (half a day)
- Create a free Supabase project. Save the URL, anon key, and service-role key.
- Get a free Gemini API key (aistudio.google.com).
- Run `applypilot init` in this repo: resume, profile.json (contact info, work auth,
  EEO defaults, salary expectations), searches.yaml.
- Verify the engine end-to-end **before** building any UI: `applypilot run`, then
  `applypilot apply --dry-run`. If the engine can't fill forms in dry-run, no panel will fix it.

### Phase 1 — Mission Control v1 (Lovable, prompts 1–4)
The panel with the schema, locked-down auth (only your email can ever log in), and four screens:
- **Dashboard**: run status, live counters (discovered / scored / applied today), Start/Stop.
- **Search Config**: the full filter builder — see §3. Saved as named presets.
- **Review Queue**: mid-score jobs waiting for your swipe (approve → apply queue, reject → learns).
- **Pipeline Board**: kanban — Discovered → Queued → Applied → Interview → Offer / Rejected.

### Phase 2 — The Bridge (engine work in this repo)
New `applypilot sync` daemon:
- Mirrors the local SQLite jobs table → Supabase (upsert on URL).
- Uploads tailored resume/cover-letter PDFs → Supabase Storage.
- Polls the `commands` table: `start_run`, `stop`, `apply_one`, `pause`.
- Pulls the active search preset from Supabase and writes searches.yaml before each run.
- Heartbeat row every 30s so the panel shows ● Engine online / ○ offline.

After Phase 2, pressing **Start** on your phone launches a full pipeline run at home.

### Phase 3 — Question Memory (engine + panel, prompt 5)
- New `questions` table: canonical question text, your answer, confidence, times used.
- Engine flow during apply: normalize the question → exact/fuzzy match against the bank →
  use stored answer. No match? Answer with AI from profile.json, **log it with
  confidence=low**, and keep going (volume never stalls).
- Panel "Questions" screen: review low-confidence answers, correct them once, locked forever.
  Exactly the Sorce feature you liked — except it's yours and free.

### Phase 4 — Email Intelligence (engine + panel, prompt 6)
- `applypilot mail` command: Gmail API (free, works with your @gmail.com) pulls new mail
  every 10 minutes during sync.
- Two-layer classification: fast keyword rules first ("unfortunately", "not moving forward",
  "schedule a call", "interview", "offer letter"), Gemini only for ambiguous ones —
  keeps you far under the free-tier daily quota.
- Auto-link emails to applications by company/domain match; auto-advance the kanban card
  (Applied → Interview, Applied → Rejected).
- Panel "Inbox" screen: categorized feed (🎯 Interview / ❌ Rejected / 💰 Offer /
  ⚡ Action needed / 📋 Confirmations), with the linked application one click away.
  **Action-needed** (assessments, scheduling links, verification emails) pins to the top —
  those are the emails that turn into interviews when answered fast.

### Phase 5 — Discovery expansion (engine)
Reduce dependence on scraping with free structured sources:
- **Greenhouse / Lever / Ashby public board APIs** — free, no key, per-company JSON feeds.
  Build a target-company registry (panel screen to add companies); poll their boards directly.
  These are also the **easiest ATSes to auto-apply to** — highest submission success rate.
- **USAJobs API** — free, all US federal jobs (real careers, real benefits).
- **Adzuna API** — free 1,000 calls/month, aggregated coverage + salary data.
- **Remotive / RemoteOK / Arbeitnow** — free feeds for remote roles.
- Grow `employers.yaml` — the Workday registry is user-extensible; every employer you add
  is a permanent new source.

### Phase 6 — Analytics & tuning (panel, prompt 7)
- Funnel: discovered → scored → applied → response → interview → offer.
- Response rate by source, by fit-score band, by title keyword, by day-of-week.
- This is the "engineered for results" part: after 2–3 weeks of data, you *know* which
  channels produce interviews and point the volume there.

---

## 3. The filter system (Search Config spec)

Every filter below is a real control in the panel and a real parameter the engine consumes.
Saved as **named presets** (e.g. "Main hunt", "Remote only", "Wide net Fridays") — pick one
and press Start.

| Filter | Control | Engine mapping |
|---|---|---|
| Job titles / queries | Tag input, 3 priority tiers | `searches.yaml queries` (tiered) |
| Location(s) | Multi-entry text + Remote toggle | `locations` |
| **Mile radius** | Slider 5–100 mi per location | JobSpy `distance` param |
| Employment type | Checkboxes: Full-time / Part-time / Contract / Temp | JobSpy `job_type` |
| **Salary floor** | Number input | Post-filter on parsed salary; jobs with no salary listed pass through (most don't list it — filtering them out kills volume) |
| Posted within | Dropdown: 24h / 3d / 7d / 14d | `hours_old` |
| Boards/sources | Checkboxes: Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google, Workday registry, ATS registry, USAJobs, Adzuna, remote boards | `boards` + new source modules |
| Title blocklist | Tag input ("intern", "director", "clearance"…) | `exclude_titles` |
| **No-go companies** | Tag input | New `exclude_companies` |
| **No-go patterns** | Prefilled toggles: commission-only, "unlimited earning potential", door-to-door, MLM, 1099-only sales, staffing-agency spam | Description regex filter, on by default |
| Auto-apply threshold | Slider 1–10 (see §4) | `--min-score` |
| Daily apply cap | Number (default 100) | Engine rate limiter |

---

## 4. The volume doctrine

You said it exactly right: **don't let the bot be precious.** The score is a *router*, not
a gatekeeper:

- **Score ≥ 6 → auto-apply. No human review. Send it.** "Maybe it's a match, maybe it's
  not" — apply anyway. An application costs the bot 3 minutes; an interview you never got
  costs you everything.
- **Score 4–5 → Review Queue.** 10 seconds of your thumb per job, batch-swipe from your phone.
- **Score ≤ 3 or hard no-go → skip**, with the reason logged so you can audit that it's
  not skipping things you'd have wanted.

Hard no-gos (never applied, no matter the score): commission-only, MLM patterns, your
blocked companies, blocked title keywords. That's the *only* selectivity the bot is allowed.

Sustainable target: **50–150 applications/day** within the Gemini free tier
(~1,500 requests/day covers scoring + tailoring + cover letters + question answering at
that volume). At even a 2–4% response rate — the realistic band for high-volume applying —
that's **1–6 recruiter responses per day, every day.** Interviews become a scheduling
problem instead of a hoping problem.

---

## 5. The zero-cost budget

| Component | Provider | Cost | Limit that matters |
|---|---|---|---|
| Control panel build | Lovable free tier | $0 | Daily prompt cap — that's why docs/LOVABLE_PROMPTS.md is written as few, dense prompts |
| Panel hosting | Lovable / or export to GitHub → Vercel free | $0 | None at 1 user |
| Database + storage + auth + realtime | Supabase free | $0 | 500 MB DB, 1 GB storage — years of job hunting; prune PDFs of rejected apps if ever needed |
| AI (scoring, tailoring, letters, Q&A, email triage) | Gemini free tier | $0 | ~1,500 req/day → the daily apply cap |
| Job data | JobSpy scraping + free APIs (§ Phase 5) | $0 | Scraper rate limits → engine already staggers |
| Email | Gmail API | $0 | Quota far above any human inbox |
| Auto-apply compute | Your own PC + Chrome | $0 | PC must be on |
| CAPTCHA solving | **You**, via the Attention Queue | $0 | ~10–20% of apps pause; you clear them in batches (CapSolver stays optional/off — it's the only paid thing in the stack and it's not needed) |

**Total: $0/month.** Sorce charges $40/month for a subset of this and keeps your data.

---

## 6. Straight talk — constraints you have to know

Bulletproof means knowing exactly where the armor is thin. Anyone who tells you otherwise
is selling something.

1. **LinkedIn and Indeed prohibit bots and actively detect them.** The engine uses them for
   *discovery* (finding out a job exists), but the highest-success **apply** path is at the
   source: the employer's own ATS (Greenhouse, Lever, Ashby, Workday). Aggregator listing →
   follow to the real career page → apply there. Automating LinkedIn Easy Apply *from your
   logged-in account* is the one thing that risks a personal-account ban — keep it off, or
   accept that risk knowingly.
2. **CAPTCHAs are not "solved" for free.** The zero-cost answer is human-in-the-loop: the
   engine parks the application, the panel shows it in the Attention Queue, you clear a batch
   in 5 minutes with coffee. This is a feature, not a failure mode.
3. **Workday requires an account per employer.** The engine creates/logs in with credentials
   from profile.json. Some portals will still defeat it; those fall into the Attention Queue
   too. Expect roughly 70–85% fully-hands-free submission, not 100% — the queue is what
   makes the last 15–30% take minutes instead of hours.
4. **Never lie on applications.** Tailoring reorganizes and emphasizes; it never fabricates
   (the engine's validator enforces this). EEO answers come from your profile defaults.
   Work-authorization answers must be true — a lie there kills the offer at background check.
5. **Your PC is the server.** Engine offline = discovery and applying paused (panel still
   works, email still classifies on next run). If that ever becomes a real problem, a $0–5
   old-laptop-in-the-closet or free-tier VM running only *discovery* (not applying) is the
   escape hatch.
6. **Volume works, but response rates are single-digit.** 1,000 applications is ~20–40
   responses, not 200. The system wins because it makes 1,000 applications cost you almost
   nothing and makes every response impossible to miss. The analytics phase then bends the
   rate upward by killing dead channels.

---

## 7. Definition of done

- [ ] You open one private URL, log in, and see everything: runs, jobs, questions, emails, stats.
- [ ] You press **Start** with a preset; the engine at home discovers, scores, tailors, and applies — no further input.
- [ ] Anything the bot can't finish appears in the Attention Queue within seconds, fixable from your phone.
- [ ] Every screening question ever answered is in the bank; repeat questions never get asked again.
- [ ] Recruiter emails auto-sort into Interview / Rejected / Offer / Action-needed and move the board.
- [ ] 50+ applications/day sustained, $0/month, no third-party service that can paywall, throttle, or read your data.
- [ ] Analytics tell you where interviews actually come from.

Ship Phase 1–2 first. The day the Start button works end-to-end, the machine is alive —
everything after that is compounding.
