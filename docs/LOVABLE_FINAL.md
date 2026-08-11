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

-- Your identity: what goes on every application form.
-- Single row (id = 1). The engine seeds it from profile.json on first
-- connection, then the panel becomes the place you edit it.
create table if not exists profile (
  id int primary key default 1 check (id = 1),
  full_name text, preferred_name text,
  email text, phone text,
  address text, city text, province_state text, country text, postal_code text,
  linkedin_url text, github_url text, portfolio_url text,
  salary_expectation text, salary_range_min text, salary_range_max text,
  years_of_experience text, education_level text, target_role text,
  legally_authorized text, require_sponsorship text,
  gender text, race_ethnicity text, veteran_status text, disability_status text,
  resume_text text,
  updated_at timestamptz default now()
);

-- Keep updated_at fresh so the engine knows when to pull changes
create or replace function touch_profile() returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;
drop trigger if exists profile_touch on profile;
create trigger profile_touch before update on profile
  for each row execute function touch_profile();

-- Independent proof a submission actually landed (employer's own email)
alter table jobs add column if not exists confirmation_status text;
alter table jobs add column if not exists confirmed_at timestamptz;

-- Interview prep, generated automatically when an interview email lands
create table if not exists interview_prep (
  id uuid primary key default gen_random_uuid(),
  job_url text unique not null,
  job_id uuid references jobs(id) on delete cascade,
  company text, title text,
  likely_questions text, talking_points text,
  questions_to_ask text, company_notes text,
  created_at timestamptz default now()
);

-- Follow-up email drafts for applications that went silent
create table if not exists followups (
  id uuid primary key default gen_random_uuid(),
  job_url text unique not null,
  job_id uuid references jobs(id) on delete cascade,
  company text, title text,
  to_address text, subject text, body text,
  status text default 'draft',      -- draft | sent | dismissed
  days_since int,
  created_at timestamptz default now()
);

-- Hands-off scheduling: run automatically every N hours (0 = manual only)
alter table search_presets add column if not exists auto_run_hours numeric default 0;

-- Indexes that matter once you have thousands of rows
create index if not exists idx_jobs_status on jobs(status);
create index if not exists idx_jobs_applied_at on jobs(applied_at desc);
create index if not exists idx_emails_received on emails(received_at desc);
create index if not exists idx_emails_category on emails(category);

-- Lock every new table to you (replace the UUID with your auth user id)
alter table questions enable row level security;
alter table emails enable row level security;
alter table target_companies enable row level security;

alter table profile enable row level security;
alter table interview_prep enable row level security;
alter table followups enable row level security;

do $$
declare
  uid text := 'YOUR-AUTH-USER-UUID';
  t text;
begin
  foreach t in array array['questions','emails','target_companies','profile',
                           'interview_prep','followups'] loop
    execute format('drop policy if exists owner_all on %I', t);
    execute format('create policy owner_all on %I for all using (auth.uid() = %L)', t, uid);
  end loop;
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
>
> **5. Free-tier AI meter.** `engine_status.counters` includes `ai_used_today`,
> `ai_remaining`, and `ai_percent_used`. Render a slim progress bar labeled
> "Daily AI budget" showing used/limit, green under 70%, amber 70–90%, red
> above 90%. When `ai_remaining` is 0, show the caption "Daily free-tier quota
> spent — the run resumes automatically tomorrow." When `ai_remaining` is null,
> show "Unlimited (local model)". This is how you stay at zero cost, so it
> should be visible without scrolling.

---

## Prompt B2 — profile & resume editor

> Build a **Settings → My Profile** page over the `profile` table. It is a
> single row with `id = 1` — read it, edit it in place, never create more rows.
>
> Group the fields into collapsible sections:
> - **Identity**: full_name, preferred_name, email, phone
> - **Location**: address, city, province_state, country, postal_code
> - **Links**: linkedin_url, github_url, portfolio_url
> - **Compensation**: salary_expectation, salary_range_min, salary_range_max
> - **Experience**: years_of_experience, education_level, target_role
> - **Work authorization**: legally_authorized, require_sponsorship (both
>   Yes/No selects)
> - **EEO (voluntary)**: gender, race_ethnicity, veteran_status,
>   disability_status — each a select including a "Decline to self-identify"
>   option, which should be the default
>
> Below those, a **Resume** section: a large monospace textarea bound to
> `resume_text`, with a live character count and a note reading "This is the
> text the AI tailors for each job. Keep it complete and factual — the tailoring
> step reorganizes and emphasizes, it never invents."
>
> Each section saves independently with an explicit Save button and a success
> toast; do not autosave on every keystroke. Show "Last updated <relative time>"
> from `updated_at` at the top of the page.
>
> Add a prominent note at the top: "The engine picks these changes up within a
> minute. Email is what receives every application confirmation — make sure it
> is right."
>
> Validation: email must be a valid address, and warn (do not block) if
> salary_expectation is empty, since it is asked on most applications.

---

## Prompt B3 — submission verification

> Applications carry a `confirmation_status` on the `jobs` table:
> `confirmed` (the employer emailed back — the submission definitely landed),
> `pending` (applied recently, too early to expect a reply), or `unconfirmed`
> (applied over 48 hours ago with total silence).
>
> **On job cards and the job detail drawer**, show a small status dot next to
> anything with `status='applied'` or later: green filled = confirmed (tooltip
> "Employer acknowledged — submission verified"), gray hollow = pending
> ("Waiting on acknowledgement"), amber outline = unconfirmed ("No reply after
> 48h — this submission may not have gone through"). In the drawer, show
> `confirmed_at` as a relative time when present.
>
> **On the Dashboard**, add a "Verified" counter beside the outcome counters:
> `confirmed / total applied` as a percentage, with the raw numbers beneath.
>
> **On the Analytics page**, add a **Confirmation rate by source** horizontal
> bar chart: for each `site` with at least 5 applications older than 48 hours,
> the percentage with `confirmation_status='confirmed'`. Sort ascending so the
> worst appears first, and color bars under 40% amber.
>
> Caption it exactly: "Some employers never acknowledge applications, so a low
> number is not proof of failure on its own. Compare sources — if one ATS
> confirms 80% and another confirms 5%, submissions to the second are probably
> failing silently."
>
> This is the one place the system can catch itself being wrong: the apply
> agent reports its own success, but an employer's email is independent proof.

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

## Prompt E2 — interview prep & follow-ups

> Build a **Prep** page with two tabs. This is what happens after an
> application gets a response, and it is the highest-value screen in the app.
>
> **Interviews** tab, over `interview_prep`: one expandable card per row,
> newest first, headed by title and company. Inside, four labeled sections
> rendered as readable lists (the text arrives as newline-separated lines
> beginning with "- "): **Likely questions**, **Your talking points**,
> **Questions to ask them**, and a short **About this role** paragraph from
> `company_notes`. Add a "Copy all" button that copies the whole pack as
> plain text, and a link to the linked job. Empty state: "No interviews yet.
> Prep is generated automatically the moment an interview email lands."
>
> **Follow-ups** tab, over `followups` where `status='draft'`: one card per
> draft showing company, title, "applied {days_since} days ago", the editable
> `subject`, and the editable `body` in a textarea. Three actions per card:
> **Copy** (copies subject + body), **Mark sent** (sets `status='sent'`), and
> **Dismiss** (sets `status='dismissed'`). When `to_address` is present, also
> show a **Open in email** button linking to
> `mailto:{to_address}?subject={subject}&body={body}` (URL-encoded).
>
> Add a clear note at the top of the Follow-ups tab: "These are drafts. Nothing
> is ever sent automatically — read it, edit it, then send." Show a count badge
> in the nav for draft follow-ups plus prep packs you haven't opened.

---

## Prompt E3 — hands-off scheduling

> In the Search Preset form, add an **Automation** field: a select bound to
> `auto_run_hours` with options Manual only (0), Every 4 hours (4), Every 8
> hours (8), Every 12 hours (12), Once a day (24). Label it "Run automatically"
> with helper text: "The engine starts a full discovery-and-apply cycle on this
> schedule with no button press. It still respects your daily apply cap and
> your AI budget."
>
> On the Dashboard, when the active preset has `auto_run_hours > 0`, show a
> small pill next to the Start button reading "Auto: every Nh" so it is obvious
> the system is running on its own.

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
