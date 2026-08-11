# Lovable — Final Build Prompts (v1.0)

**This file supersedes LOVABLE_PROMPTS.md.** It matches the engine that is now
actually built and tested: question bank, email inbox, outcome tracking,
target-company registry, and the sync bridge.

If you already built a panel from the earlier prompts, you do not need to start
over. Run **Prompt A** (schema alignment) and then whichever of B–E you're
missing.

**Rules to restate to Lovable any time it drifts:**
- Private single-user control panel. It reads and writes Supabase, nothing else.
- It must NEVER scrape job boards, call job APIs, or submit applications. A
  separate local engine does all of that. The panel displays data and writes
  commands.
- No landing page, no marketing copy, no signup flow. Login goes straight to the app.
- Mobile-first — it is driven from a phone.

---

## Prompt A — schema alignment (run this first, in Supabase SQL editor)

Not a Lovable prompt. Paste into **Supabase → SQL Editor** so the database matches
the engine. Safe to run on an existing project; every statement is idempotent.

```sql
-- Outcome vocabulary the engine now writes
alter table jobs drop constraint if exists jobs_status_check;

-- Columns the engine's sync daemon expects
alter table jobs add column if not exists skip_reason text;
alter table jobs add column if not exists attention_reason text;
alter table jobs add column if not exists salary_text text;
alter table jobs add column if not exists resume_pdf_path text;
alter table jobs add column if not exists cover_letter_path text;
alter table jobs add column if not exists updated_at timestamptz default now();

-- Question bank
create table if not exists questions (
  id uuid primary key default gen_random_uuid(),
  question_normalized text unique not null,
  question_raw text not null,
  answer text not null,
  answer_type text default 'text',
  confidence text default 'low',
  times_used int default 0,
  last_used_at timestamptz,
  source_job_url text,
  created_at timestamptz default now()
);

-- Inbox
create table if not exists emails (
  id uuid primary key default gen_random_uuid(),
  gmail_id text unique not null,
  from_address text, subject text, snippet text, body_text text,
  received_at timestamptz,
  category text,
  classified_by text,
  job_id uuid references jobs(id) on delete set null,
  is_read boolean default false,
  created_at timestamptz default now()
);

-- Company ATS registry (feeds direct-from-source discovery)
create table if not exists target_companies (
  id uuid primary key default gen_random_uuid(),
  company text not null,
  ats text not null,
  board_token text not null,
  enabled boolean default true,
  created_at timestamptz default now()
);

-- Indexes that matter once you have thousands of rows
create index if not exists idx_jobs_status on jobs(status);
create index if not exists idx_jobs_applied_at on jobs(applied_at desc);
create index if not exists idx_emails_received on emails(received_at desc);
create index if not exists idx_emails_category on emails(category);

-- Lock every new table to you (replace the UUID with your auth user id)
alter table questions enable row level security;
alter table emails enable row level security;
alter table target_companies enable row level security;

do $$
declare uid text := 'YOUR-AUTH-USER-UUID';
begin
  execute format('drop policy if exists owner_all on questions');
  execute format('create policy owner_all on questions for all using (auth.uid() = %L)', uid);
  execute format('drop policy if exists owner_all on emails');
  execute format('create policy owner_all on emails for all using (auth.uid() = %L)', uid);
  execute format('drop policy if exists owner_all on target_companies');
  execute format('create policy owner_all on target_companies for all using (auth.uid() = %L)', uid);
end $$;
```

Then enable **Realtime** on `jobs`, `engine_status`, `commands`, and `emails`.

---

## Prompt B — dashboard corrections + setup visibility

> Update the Dashboard with these corrections and additions.
>
> **1. Fix the run control.** When the engine is offline (no `engine_status`
> heartbeat within the last 2 minutes), ALWAYS show the Start button — never
> Stop — regardless of `current_stage`. Show Stop only when the engine is
> genuinely online and `current_stage` is not `idle`.
>
> **2. Setup blocker banner.** When `engine_status.current_stage` begins with
> `blocked:`, show a prominent amber banner at the top of the Dashboard reading
> "Engine can't run — setup incomplete", listing each string from
> `engine_status.counters.setup_problems` as a bullet. This is how the engine
> reports a missing resume or API key, and it must be impossible to miss.
>
> **3. Duplicate-run guard.** If a `commands` row with `command='start_run'` is
> still `pending` or `picked_up`, the Start button shows "Queued — waiting for
> engine" and is disabled instead of inserting another row. If such a row is
> older than 10 minutes while the heartbeat is stale, show "Not picked up —
> engine offline?" with a Cancel button that sets that row's status to `error`.
>
> **4. Outcome counters.** Add a second counter row beneath the existing one
> showing live counts from `jobs`: Interviews (`status='interview'`), Offers
> (`status='offer'`), Awaiting reply (`status='applied'`), and Response rate
> (interviews + offers + rejected, divided by total applied, as a percentage).
> Interviews and Offers should be visually emphasized — those are the numbers
> that matter.

---

## Prompt C — inbox

> Build the **Inbox** page over the `emails` table — a categorized feed, newest
> first.
>
> Filter chips across the top with unread counts (`is_read=false`):
> ⚡ Action needed, 🎯 Interview, 💰 Offer, ❌ Rejection, 📋 Confirmation, Other.
> Default view pins Action needed and Interview above everything else — those
> are the emails that turn into interviews when answered fast.
>
> Each row: a category color bar, sender, subject, snippet, relative time, and
> when `job_id` is set a chip showing the linked job's company and status.
> Unread rows are visually distinct.
>
> Clicking a row opens a drawer with the full `body_text`, marks it
> `is_read=true`, and offers: **Open linked job** (navigates to that job's
> detail), **Re-link** (search jobs by company name, set `job_id`), and
> **Re-categorize** (dropdown writing `category`).
>
> Subscribe to Realtime inserts filtered to `category=interview` and
> `category=offer`; when one arrives while the tab is open, fire a browser
> notification and show a toast. Empty state: "No job email yet — the engine
> scans your inbox every 10 minutes."

---

## Prompt D — question bank

> Build the **Questions** page over the `questions` table, with two tabs.
>
> **Needs review** — rows where `confidence='low'`, newest first. Each shows
> `question_raw`, the stored `answer` in an inline-editable field,
> `times_used`, and a link to `source_job_url`. A **Confirm** button sets
> `confidence='high'` (saving any edit first). Add "Confirm all shown" for
> batch work. This tab should show a count badge in the nav.
>
> **Confirmed** — rows where `confidence='high'`: a searchable table of
> question, answer, times_used, last_used_at. Answers stay inline-editable;
> deleting requires confirmation.
>
> Header text, one line: "The engine answers from this bank first. Anything it
> had to guess lands in Needs review — confirm it once and it's locked in."

---

## Prompt E — analytics + target companies

> **Analytics page**, computed from `jobs` and `emails`:
> - Funnel: Discovered → Applied → Response → Interview → Offer, with the
>   conversion percentage between each pair. "Response" means a job with at
>   least one linked email whose category is not `confirmation`.
> - Applications per day for the last 30 days (bar chart).
> - Three horizontal bar charts of **response rate**: by `site`, by fit-score
>   band (≤3 / 4–5 / 6–7 / 8–10), and by the first word of the job title.
>   Caption under each: "Point volume where the bars are longest."
> - Median days from `applied_at` to first linked email.
> - A "Blocked jobs" table: jobs with a `skip_reason`, grouped by reason with
>   counts, so it is auditable that the filters aren't discarding good jobs.
>
> **Settings → Target Companies**, over `target_companies`: add rows with
> company name, ATS (select: greenhouse / lever / ashby / workday), board token,
> and an enabled toggle. Under the token field show the resulting URL for the
> selected ATS so it can be verified before saving:
> - greenhouse → `boards-api.greenhouse.io/v1/boards/{token}/jobs`
> - lever → `api.lever.co/v0/postings/{token}`
> - ashby → `api.ashbyhq.com/posting-api/job-board/{token}`
> - workday → handled by the engine's employer registry
>
> Explain in one line: "The token is the company's slug in its job-board URL —
> `job-boards.greenhouse.io/stripe` means the token is `stripe`. These boards
> are the highest-success application path."

---

## Prompt F — resilience and security pass (run last)

> Do a resilience and security pass over the entire app without changing
> features:
> - Every page gets a loading skeleton, a designed empty state, and an error
>   state with a retry button.
> - Every Supabase write shows a failure toast on error and rolls back
>   optimistic UI.
> - Realtime subscriptions re-subscribe automatically after network loss.
> - Confirm the only Supabase credential in the client bundle is the anon key —
>   a service-role key must never appear in frontend code.
> - Confirm no route renders data before the auth check resolves.
> - Confirm storage files are fetched via short-lived signed URLs.
> - Remove any `console.log` that prints row data.
>
> Report what you found and what you changed.

---

## After Lovable is done

Everything else runs on your machine:

```bash
applypilot init      # resume, profile, API key   <- the step that makes it real
applypilot doctor    # every line should read OK
applypilot verify    # live-tests every job API, your mail, and Supabase
applypilot sync      # bridge goes live; panel turns green
```

Then prove one application end to end before turning up the volume:

```bash
applypilot run all
applypilot apply --dry-run --limit 1
```
