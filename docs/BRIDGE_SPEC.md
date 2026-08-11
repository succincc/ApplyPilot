# Bridge Spec — Panel ⇄ Supabase ⇄ Engine Contract

This is the engineering contract that makes the three components interoperate. The Lovable
panel and the local engine never talk to each other directly — **Supabase is the only
interface**. Anything not in these tables does not exist as far as the other side knows.

## 1. Roles and credentials

| Actor | Credential | Access |
|---|---|---|
| Mission Control (browser) | Supabase Auth session (your email only) + anon key | RLS-scoped read/write |
| Engine (your PC) | Service-role key in `~/.applypilot/.env` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`) | Full, bypasses RLS |
| Everyone else | — | Nothing. RLS denies all rows to any UID except yours |

Single-user lockdown: disable public signups in Supabase Auth; create your one user
manually. Every RLS policy is `auth.uid() = '<your-user-uuid>'`. Even if the URL leaks,
nobody can register or read a row.

## 2. Tables

SQL below is authoritative; the Lovable prompts reference it. All tables:
`id uuid primary key default gen_random_uuid()`, `created_at timestamptz default now()`.

### `jobs` — mirror of the engine's SQLite jobs table
```sql
create table jobs (
  id uuid primary key default gen_random_uuid(),
  url text unique not null,              -- dedupe key, same as engine
  title text, company text, location text,
  salary_text text, salary_min int, salary_max int,
  site text,                             -- indeed | linkedin | workday | greenhouse | ...
  full_description text,
  application_url text,
  fit_score int, score_reasoning text,
  status text not null default 'discovered',
    -- discovered | review | queued | applying | needs_attention |
    -- applied | rejected_by_me | interview | offer | rejected | failed | skipped
  skip_reason text,                      -- audit trail: why the bot skipped it
  attention_reason text,                 -- captcha | login_wall | unknown_question | error
  resume_pdf_path text, cover_letter_path text,   -- Supabase Storage paths
  applied_at timestamptz,
  discovered_at timestamptz,
  updated_at timestamptz default now(),
  created_at timestamptz default now()
);
```

### `search_presets` — the filter builder's output, the engine's input
```sql
create table search_presets (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  is_active boolean default false,        -- exactly one active at a time
  queries jsonb not null,                 -- [{"query":"software engineer","tier":1}, ...]
  locations jsonb not null,               -- [{"location":"Tampa, FL","remote":false,"radius_miles":25}]
  job_types text[] default '{fulltime}',  -- fulltime | parttime | contract | temp
  salary_floor int,
  hours_old int default 72,
  boards text[] default '{indeed,linkedin,glassdoor,zip_recruiter,google}',
  extra_sources text[] default '{}',      -- workday | ats_registry | usajobs | adzuna | remote_boards
  exclude_titles text[] default '{}',
  exclude_companies text[] default '{}',
  no_go_patterns text[] default '{commission only,commission-only,1099 only,unlimited earning potential,door-to-door,door to door}',
  min_score_auto_apply int default 6,
  review_band_low int default 4,
  daily_apply_cap int default 100,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
```

### `commands` — panel → engine queue
```sql
create table commands (
  id uuid primary key default gen_random_uuid(),
  command text not null,       -- start_run | stop | pause | resume | apply_one | mark_resolved
  payload jsonb default '{}',  -- e.g. {"preset_id": "..."} or {"job_id": "..."}
  status text default 'pending',  -- pending | picked_up | done | error
  error text,
  created_at timestamptz default now(),
  handled_at timestamptz
);
```

### `engine_status` — heartbeat + live progress (single row, upserted)
```sql
create table engine_status (
  id int primary key default 1 check (id = 1),
  online boolean default false,
  last_heartbeat timestamptz,
  current_stage text,            -- idle | discover | enrich | score | tailor | apply
  run_started_at timestamptz,
  counters jsonb default '{}'    -- {"discovered":142,"scored":98,"applied_today":37,"attention":4}
);
```

### `questions` — the answer memory bank
```sql
create table questions (
  id uuid primary key default gen_random_uuid(),
  question_normalized text unique not null,  -- lowercased, stripped, canonical
  question_raw text not null,                -- as first seen
  answer text not null,
  answer_type text default 'text',           -- text | select | boolean | number
  confidence text default 'low',             -- low = AI-guessed, needs review; high = human-confirmed
  times_used int default 0,
  last_used_at timestamptz,
  source_job_url text,
  created_at timestamptz default now()
);
```

### `emails` — classified inbox
```sql
create table emails (
  id uuid primary key default gen_random_uuid(),
  gmail_id text unique not null,
  from_address text, subject text, snippet text, body_text text,
  received_at timestamptz,
  category text,           -- interview | rejection | offer | action_needed | confirmation | other
  classified_by text,      -- rules | ai
  job_id uuid references jobs(id),   -- linked application, when matched
  is_read boolean default false,
  created_at timestamptz default now()
);
```

### `target_companies` — Phase 5 ATS registry
```sql
create table target_companies (
  id uuid primary key default gen_random_uuid(),
  company text not null,
  ats text not null,          -- greenhouse | lever | ashby | workday
  board_token text not null,  -- e.g. 'stripe' → boards-api.greenhouse.io/v1/boards/stripe/jobs
  enabled boolean default true,
  created_at timestamptz default now()
);
```

Enable **Realtime** on `jobs`, `engine_status`, `commands`, and `emails` so the panel
updates live without polling.

## 3. The engine sync loop (`applypilot sync`)

New long-running command in this repo (Phase 2). Single asyncio loop, ~200 lines:

```
every 30s:
    upsert engine_status heartbeat (online=true, stage, counters)
every 15s:
    poll commands where status='pending' order by created_at
      start_run  → fetch active/specified search_preset
                   → render it to ~/.applypilot/searches.yaml
                   → launch pipeline (run) then apply --continuous, as subprocesses
      stop/pause/resume → signal the subprocesses
      apply_one  → applypilot apply --url <payload.url>
    mark command picked_up → done/error
every 60s:
    diff local SQLite jobs (updated since last sync) → upsert to Supabase jobs by url
    upload new tailored PDFs / cover letters → Storage bucket 'artifacts',
      write storage paths back to the jobs row
every 10min:
    run mail ingest (see §4)
on apply blocker (captcha / login wall / unanswerable question):
    set job status='needs_attention' + attention_reason, continue with next job
on shutdown:
    engine_status.online = false
```

Status mapping engine → bridge: `apply_status` NULL→`discovered`/`queued`,
`success`→`applied`, `failed`→`failed`, plus the new `needs_attention` path. The engine's
SQLite stays the source of truth for pipeline internals; Supabase is the source of truth
for **human decisions** (review approvals, question corrections, no-go lists) — the sync
loop pulls those down before each run.

Conflict rule: fields written by humans in the panel (status overrides like
`rejected_by_me`, question answers, presets) always win over engine writes.

## 4. Mail ingest (`applypilot mail`, called by sync)

1. Gmail API OAuth (one-time browser consent on your PC; token cached in `~/.applypilot/`).
   Free. Read-only scope (`gmail.readonly`) — the system never sends or deletes mail.
2. Pull messages since last sync (`after:` query), skip already-stored `gmail_id`s.
3. Classify, cheapest first:
   - **Rules layer** (no AI cost): sender domain in known-ATS list
     (greenhouse.io, lever.co, myworkday.com, ashbyhq.com, icims.com, smartrecruiters.com…)
     or keyword hits — "unfortunately", "not moving forward", "other candidates" → `rejection`;
     "schedule", "interview", "availability", "phone screen" → `interview`;
     "offer letter", "compensation package" → `offer`; "assessment", "complete your
     application", "verify" → `action_needed`; "application received", "thank you for
     applying" → `confirmation`.
   - **AI layer**: only unmatched job-related mail goes to Gemini (one short prompt,
     JSON out). Typically <30 calls/day — noise in the free-tier budget.
4. Link to application: match sender domain or company name against applied jobs
   (fuzzy, most-recent-first). Store `job_id`.
5. Auto-advance: linked `interview`/`offer`/`rejection` emails update `jobs.status`
   (never downgrade: an `interview` job is not moved back to `rejected` by a later
   automated rejection for a different req — flag for review instead).

## 5. Question bank integration (engine side)

In the apply stage prompt flow (`src/applypilot/apply/prompt.py`):

1. Before answering any screening question, normalize it (lowercase, strip punctuation,
   collapse whitespace) and look up `questions` (exact match, then trigram/fuzzy ≥ 0.85 —
   Postgres `pg_trgm`).
2. Hit → use the stored answer verbatim, bump `times_used`.
3. Miss → answer from profile.json via the LLM as today, then insert with
   `confidence='low'`. **Never block the application waiting for a human.**
4. The panel's Questions screen lists low-confidence entries; you edit/confirm → `high`.
   Sync pulls the bank down before each apply session (single query, cached in memory).

## 6. Storage layout

Bucket `artifacts` (private): `resumes/<job-uuid>.pdf`, `covers/<job-uuid>.pdf`,
`base/resume.pdf`. Panel renders signed URLs on demand. ~100 KB/PDF → 1 GB free tier
holds ~10,000 applications; prune artifacts of `rejected` jobs if it ever fills.

## 7. Failure and restart semantics

- Every command is idempotent to re-pickup: `picked_up` older than 10 min with no
  heartbeat progress → panel offers "re-issue".
- Sync crash: next start re-diffs SQLite by `updated_at` — no data loss, Supabase upserts
  are idempotent on `url`.
- Supabase unreachable: engine keeps working locally, sync catches up when back.
- Panel never blocks on the engine: it only writes rows. Engine offline is a visible
  state (heartbeat stale), not an error dialog.
