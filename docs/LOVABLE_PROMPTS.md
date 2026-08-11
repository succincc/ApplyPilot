# Lovable Build Prompts — Mission Control

Paste these into Lovable **in order**, one at a time. Each prompt is dense on purpose:
Lovable's free tier caps prompts per day, so every prompt builds a complete slice. Test the
slice, fix anything broken with a short follow-up prompt, then move on. Do **Prompt 0's
manual steps yourself** in the Supabase dashboard — auth lockdown is not something to
delegate to AI.

**Ground rules for the whole build (repeat to Lovable if it ever drifts):**
- This app is a private, single-user control panel. It only reads/writes Supabase.
- It must NEVER attempt to scrape job boards, call job APIs, or submit applications —
  an external engine does that. The app's job is to display data and write commands/settings.
- Mobile-first: it will mostly be driven from a phone.
- No landing page, no marketing pages, no signup flow. Login → app.

---

## Prompt 0 — manual setup (you, not Lovable)

1. Create a Supabase project (free tier). In **Authentication → Providers**: enable Email,
   **disable signups**. Manually create one user: `<your email>`. Note the user's UUID.
2. In the SQL editor, run the full schema from `docs/BRIDGE_SPEC.md` §2 (all seven tables),
   then enable RLS on every table with a single policy per table:
   `for all using (auth.uid() = '<your-user-uuid>')`.
3. Create a private Storage bucket named `artifacts`.
4. Enable Realtime for `jobs`, `engine_status`, `commands`, `emails`.
5. In Lovable: create the project, connect it to this Supabase project.

---

## Prompt 1 — shell, auth, dashboard

> Build a private single-user job-search command center called "ApplyPilot Mission
> Control". It is connected to an existing Supabase project whose schema already exists —
> do not create or modify tables; read the schema and generate types from it.
>
> **Auth:** email+password login against Supabase Auth only. No signup, no social auth,
> no forgot-password page (a link to Supabase's recovery is fine). Unauthenticated users
> see only the login form. After login, land on the Dashboard.
>
> **Layout:** dark theme, dense, mobile-first. Bottom tab bar on mobile / left sidebar on
> desktop with: Dashboard, Jobs, Review, Questions, Inbox, Analytics, Settings. A
> persistent status pill in the header showing engine state from the `engine_status`
> table via Supabase Realtime: green "Engine online · <current_stage>" when
> `last_heartbeat` is under 2 minutes old, gray "Engine offline" otherwise.
>
> **Dashboard page:**
> - Big primary **Start Run** button: opens a sheet to pick a row from `search_presets`
>   (default: the one with `is_active=true`), then inserts into `commands`:
>   `{command:'start_run', payload:{preset_id}}`. While the engine is running
>   (`engine_status.current_stage != 'idle'`), the button becomes **Stop** (inserts
>   `{command:'stop'}`).
> - Live counter cards from `engine_status.counters` (jsonb): Discovered, Scored,
>   Applied today, Needs attention. Update via Realtime.
> - **Attention Queue** section: jobs where `status='needs_attention'`, each card showing
>   title, company, `attention_reason` as a colored badge (captcha / login_wall /
>   unknown_question / error), a link opening `application_url` in a new tab, and a
>   "Mark resolved" button that inserts `{command:'mark_resolved', payload:{job_id}}`
>   and sets the job's status to `queued`.
> - "Today" strip: last 10 jobs with `status='applied'` ordered by `applied_at` desc.

---

## Prompt 2 — search preset builder

> Add a **Settings → Search Presets** section managing the `search_presets` table. List
> existing presets with an "Active" radio (setting one active sets `is_active=false` on
> all others). Create/edit opens a full-screen form with these controls, mapped exactly
> to the columns:
>
> - **Job titles**: tag input grouped under three tiers (Tier 1 exact targets, Tier 2
>   strong matches, Tier 3 wide net) → `queries` jsonb `[{"query":"...","tier":1}]`.
> - **Locations**: repeatable rows of {location text, remote toggle, radius slider
>   5–100 miles} → `locations` jsonb.
> - **Employment type**: checkboxes Full-time / Part-time / Contract / Temp → `job_types`.
> - **Salary floor**: number input with a helper note "jobs that don't list salary still
>   pass" → `salary_floor`.
> - **Posted within**: select 24h/72h/7d/14d → `hours_old` (24/72/168/336).
> - **Sources**: two checkbox groups — Boards (indeed, linkedin, glassdoor,
>   zip_recruiter, google) → `boards`; Extra sources (workday, ats_registry, usajobs,
>   adzuna, remote_boards) → `extra_sources`.
> - **Blocklists**: tag inputs for `exclude_titles` and `exclude_companies`; a toggle
>   list of "no-go patterns" prefilled from the column default (commission-only etc.),
>   editable → `no_go_patterns`.
> - **Automation**: sliders for `min_score_auto_apply` (1–10, default 6, label
>   "Auto-apply at or above"), `review_band_low` (label "Send to Review at or above"),
>   and `daily_apply_cap` (10–300, default 100).
>
> Validate: at least one query and one location. Duplicating a preset copies all fields
> with " (copy)" appended to the name.

---

## Prompt 3 — jobs pipeline board + job detail

> Add the **Jobs** page: a kanban board over the `jobs` table with columns Discovered,
> Queued, Applying, Applied, Interview, Offer, Rejected (statuses `discovered`, `queued`,
> `applying`, `applied`, `interview`, `offer`, and `rejected`+`rejected_by_me`+`failed`
> merged into Rejected with sub-badges). Realtime updates. On mobile, columns become
> horizontally swipeable full-width panes with a column picker.
>
> Card: title, company, location, `fit_score` as a colored chip (8–10 green, 6–7 lime,
> 4–5 amber, ≤3 gray), site badge, relative time. Drag between columns (or a move menu
> on mobile) updates `status`.
>
> Filters bar above the board: text search (title/company), score range, site
> multi-select, date range. Counts per column in the headers.
>
> Clicking a card opens a **Job detail** drawer: full description (rendered, scrollable),
> score reasoning, salary text, links to the posting and `application_url`, signed-URL
> download buttons for `resume_pdf_path` and `cover_letter_path` from the `artifacts`
> storage bucket, timeline (discovered → applied timestamps), linked emails (from
> `emails` where `job_id` matches, newest first with category badges), and action
> buttons: **Apply now** (insert command `apply_one` with the job url), **Skip** (status
> → `rejected_by_me`), **Block company** (adds company to the active preset's
> `exclude_companies` and sets status → `rejected_by_me`).

---

## Prompt 4 — review queue (swipe)

> Add the **Review** page: jobs with `status='review'`, presented one at a time as a
> full-screen card — title, company, location, salary, score chip, score reasoning, and
> the first ~40 lines of the description (expandable). Two big buttons: **Apply**
> (status → `queued`) and **Pass** (status → `rejected_by_me`). Support swipe right =
> apply, swipe left = pass on touch devices, and keyboard arrows on desktop. Show
> "N remaining" and an undo snackbar after each action. Empty state: "Review queue
> clear — the bot is handling everything above your threshold."

---

## Prompt 5 — question bank

> Add the **Questions** page over the `questions` table, two tabs:
>
> **Needs review** (`confidence='low'`, newest first): each row shows `question_raw`,
> the AI's stored `answer`, `times_used`, and the source job link. Inline edit of the
> answer; a **Confirm** button sets `confidence='high'` (with or without edits). Bulk
> "confirm all shown".
>
> **Confirmed** (`confidence='high'`): searchable table of question → answer →
> times_used → last_used_at, inline-editable answers, delete with confirm.
>
> Header explains in one line: "The engine answers from this bank first; anything it had
> to guess lands in Needs review."

---

## Prompt 6 — inbox

> Add the **Inbox** page over the `emails` table: a categorized feed, newest first, with
> filter chips: ⚡ Action needed, 🎯 Interview, 💰 Offer, ❌ Rejection, 📋 Confirmation,
> Other — each with unread counts (`is_read=false`). Action-needed and Interview are
> pinned above the fold by default.
>
> Row: category color-bar, from, subject, snippet, relative time, linked-job chip
> (company + status) when `job_id` is set. Click expands the full `body_text` in a
> drawer, marks `is_read=true`, and offers: open the linked job, **Re-link** (search
> jobs by company name and set `job_id`), and **Re-categorize** (moves it and — visual
> only — the row updates). New `interview`/`offer` emails should also trigger a browser
> notification when the tab is open (Realtime insert subscription filtered to those
> categories).

---

## Prompt 7 — analytics + target companies

> Add the **Analytics** page computed from `jobs` and `emails`:
> - Funnel bar: Discovered → Applied → Response (jobs with any linked email) →
>   Interview → Offer, with conversion % between stages.
> - Applications per day, last 30 days (bar chart).
> - Response rate by source (`site`), by fit-score band (≤3 / 4–5 / 6–7 / 8–10), and by
>   Tier-1 title keyword — three small horizontal bar charts. A short caption under
>   each: "Point volume where the green bars are."
> - Median days from applied → first response.
>
> Also add **Settings → Target Companies** over the `target_companies` table: add rows
> with company name, ATS select (greenhouse / lever / ashby / workday), board token
> (with a hint showing the resulting URL pattern per ATS), and an enabled toggle. This
> feeds the engine's direct-ATS discovery.

---

## After the build

- Test everything against real data by running the engine's `applypilot sync` (Phase 2
  of the roadmap) — the panel is only as alive as the bridge.
- Use Lovable's GitHub export so the panel's code lives in your own repo; if Lovable's
  hosting ever becomes a constraint, deploy the same app to Vercel/Netlify free tier
  unchanged.
- Keep follow-up prompts surgical ("On the Jobs board, the score chip colors are
  inverted — 8 should be green") — one defect per prompt gets the best results.
