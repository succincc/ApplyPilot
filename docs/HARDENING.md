# Hardening & Proof Checklist — make sure Mission Control actually works

The panel is finished when Lovable says so. It's **proven** when it passes this document.
Order matters: seed fake data first (§1), because an empty panel hides every bug — you
can't tell "working" from "silently broken" when every list is legitimately empty.

---

## 1. Seed data — light the panel up before the engine exists

Paste this into the **Supabase SQL editor** (your `applypilot` project). It creates a
realistic snapshot: an online engine, jobs in every status, attention cases, low-confidence
questions, categorized emails. Safe to re-run; safe to delete later (§4).

```sql
-- Engine looks online, mid-run
insert into engine_status (id, online, last_heartbeat, current_stage, run_started_at, counters)
values (1, true, now(), 'apply', now() - interval '2 hours',
        '{"discovered": 142, "scored": 98, "applied_today": 37, "attention": 2}')
on conflict (id) do update set online = excluded.online,
  last_heartbeat = excluded.last_heartbeat, current_stage = excluded.current_stage,
  run_started_at = excluded.run_started_at, counters = excluded.counters;

-- Jobs across every status the UI has to render
insert into jobs (url, title, company, location, salary_text, site, fit_score, score_reasoning, status, attention_reason, skip_reason, applied_at, discovered_at) values
('https://boards.greenhouse.io/acme/jobs/1001', 'Backend Engineer', 'Acme Corp', 'Tampa, FL', '$95k–$120k', 'greenhouse', 8, 'Strong match: Python, FastAPI, PostgreSQL all present.', 'applied', null, null, now() - interval '1 day', now() - interval '2 days'),
('https://jobs.lever.co/globex/2002', 'Full Stack Developer', 'Globex', 'Remote', '$85k–$110k', 'lever', 7, 'Good match; React + Python overlap.', 'interview', null, null, now() - interval '5 days', now() - interval '6 days'),
('https://initech.wd5.myworkdayjobs.com/3003', 'Software Engineer II', 'Initech', 'Orlando, FL', null, 'workday', 6, 'Moderate: stack partially overlaps.', 'needs_attention', 'captcha', null, null, now() - interval '3 hours'),
('https://umbrella.wd1.myworkdayjobs.com/3004', 'Platform Engineer', 'Umbrella Inc', 'Remote', '$130k', 'workday', 9, 'Excellent match.', 'needs_attention', 'unknown_question', null, null, now() - interval '1 hour'),
('https://www.indeed.com/viewjob?jk=4005', 'Python Developer', 'Hooli', 'Miami, FL', '$90k', 'indeed', 5, 'Borderline: junior-leaning.', 'review', null, null, null, now() - interval '4 hours'),
('https://www.indeed.com/viewjob?jk=4006', 'Systems Engineer', 'Stark Industries', 'Tampa, FL', null, 'indeed', 4, 'Weak-moderate match.', 'review', null, null, null, now() - interval '5 hours'),
('https://www.linkedin.com/jobs/view/5007', 'Sales Engineer — Commission', 'Vandelay', 'Remote', 'Commission', 'linkedin', 7, 'Skipped by rule.', 'skipped', null, 'no_go_pattern: commission only', null, now() - interval '6 hours'),
('https://boards.greenhouse.io/pied/jobs/6008', 'DevOps Engineer', 'Pied Piper', 'Remote', '$105k', 'greenhouse', 8, 'Strong match.', 'queued', null, null, null, now() - interval '30 minutes'),
('https://jobs.ashbyhq.com/dunder/7009', 'Data Engineer', 'Dunder Mifflin', 'Remote', null, 'ashby', 6, 'Decent match.', 'applied', null, null, now() - interval '3 days', now() - interval '4 days'),
('https://jobs.lever.co/wonka/8010', 'Cloud Engineer', 'Wonka Industries', 'Remote', '$115k', 'lever', 7, 'Good match.', 'rejected', null, null, now() - interval '10 days', now() - interval '11 days')
on conflict (url) do nothing;

-- Question bank: 2 needing review, 2 confirmed
insert into questions (question_normalized, question_raw, answer, confidence, times_used, source_job_url) values
('are you willing to undergo a background check', 'Are you willing to undergo a background check?', 'Yes', 'low', 1, 'https://initech.wd5.myworkdayjobs.com/3003'),
('what is your desired salary', 'What is your desired salary?', '95000', 'low', 3, 'https://boards.greenhouse.io/acme/jobs/1001'),
('are you legally authorized to work in the united states', 'Are you legally authorized to work in the United States?', 'Yes', 'high', 14, null),
('will you now or in the future require sponsorship', 'Will you now or in the future require sponsorship for employment visa status?', 'No', 'high', 12, null)
on conflict (question_normalized) do nothing;

-- Emails across categories, two linked to jobs
insert into emails (gmail_id, from_address, subject, snippet, body_text, received_at, category, classified_by, job_id, is_read) values
('seed-001', 'no-reply@hire.lever.co', 'Interview availability — Globex Full Stack Developer', 'We''d love to schedule a phone screen…', 'Hi, thanks for applying to Globex. We''d love to schedule a 30-minute phone screen. Please pick a time…', now() - interval '2 days', 'interview', 'rules', (select id from jobs where url = 'https://jobs.lever.co/globex/2002'), false),
('seed-002', 'no-reply@greenhouse.io', 'Update on your application — Wonka Industries', 'Unfortunately we have decided…', 'Thank you for your interest. Unfortunately, we have decided to move forward with other candidates…', now() - interval '9 days', 'rejection', 'rules', (select id from jobs where url = 'https://jobs.lever.co/wonka/8010'), true),
('seed-003', 'assessments@hackerrank.com', 'Action required: complete your Acme Corp assessment', 'You have 5 days to complete…', 'Acme Corp has invited you to complete a coding assessment. Deadline in 5 days…', now() - interval '20 hours', 'action_needed', 'rules', (select id from jobs where url = 'https://boards.greenhouse.io/acme/jobs/1001'), false),
('seed-004', 'donotreply@myworkday.com', 'Application received — Dunder Mifflin', 'Thank you for applying…', 'This confirms we received your application for Data Engineer…', now() - interval '3 days', 'confirmation', 'rules', (select id from jobs where url = 'https://jobs.ashbyhq.com/dunder/7009'), true)
on conflict (gmail_id) do nothing;

-- One active preset
insert into search_presets (name, is_active, queries, locations, job_types, salary_floor, hours_old, boards, exclude_titles, exclude_companies)
select 'Main hunt', true,
  '[{"query":"software engineer","tier":1},{"query":"backend developer","tier":1},{"query":"python developer","tier":2},{"query":"developer","tier":3}]',
  '[{"location":"Tampa, FL","remote":false,"radius_miles":25},{"location":"Remote","remote":true,"radius_miles":100}]',
  '{fulltime}', 80000, 72, '{indeed,linkedin,glassdoor,zip_recruiter,google}',
  '{intern,director,clearance}', '{}'
where not exists (select 1 from search_presets);
```

## 2. The smoke test — pass/fail, phone in hand

Walk every line. Anything that fails becomes **one surgical Lovable prompt** ("On the
Jobs board, dragging a card doesn't update status in Supabase — fix the update call").

**Dashboard**
- [ ] Status pill shows green "Engine online · apply".
- [ ] Counters read 142 / 98 / 37 / 2.
- [ ] Attention Queue shows Initech (captcha) and Umbrella (unknown_question) with the right badges.
- [ ] "Mark resolved" on one → its status flips to `queued` **and** a `mark_resolved` row appears in `commands` (check the table in Supabase).
- [ ] Press **Start Run** → a `start_run` row appears in `commands` with the preset id in payload. **This is the panel's single most important behavior — the whole system hangs off this write.**

**Jobs board**
- [ ] All 10 seeded jobs appear in the right columns; the skipped Vandelay job shows its skip reason somewhere visible.
- [ ] Drag/move a card → `status` actually changes in the Supabase table (verify in Table editor, not just on screen).
- [ ] Job detail drawer: description, score reasoning, linked email shows on Acme (the assessment) and Globex (the interview).
- [ ] While the drawer is open, edit that job's title in Supabase Table editor → the UI updates within seconds (Realtime works).

**Review** — [ ] Shows Hooli and Stark; swipe right moves one to `queued`, swipe left to `rejected_by_me`; undo works.

**Questions** — [ ] Two rows under Needs review; editing the salary answer and confirming flips `confidence` to `high` in the table.

**Inbox** — [ ] Four emails, correct categories, unread counts (2); opening one sets `is_read=true`; linked-job chips navigate.

**Presets** — [ ] Open "Main hunt", change radius to 50, save, reload the page → the change persisted (round-trip test).

**Auth (the one that actually matters)**
- [ ] Log out → every route redirects to login; nothing renders data.
- [ ] Open the site in a private/incognito window, no login: blank/login only.
- [ ] Try to register a new account → impossible (signups disabled).
- [ ] In Supabase: **Table Editor → each table → RLS enabled** shows ON for all 7. If any table shows "RLS disabled", fix it *now* — that's a public database.

## 3. Hardening prompts to paste into Lovable (in this order)

**H1 — resilience pass:**
> Do a resilience pass over the whole app without changing any features: every page gets a
> loading skeleton, a designed empty state, and an error state with a retry button. Every
> Supabase write shows a failure toast if it errors and rolls back optimistic UI. Realtime
> subscriptions must re-subscribe automatically after network loss (test by toggling
> airplane mode). The engine status pill must flip to "offline" on its own when
> last_heartbeat goes stale, without a page refresh.

**H2 — security self-audit:**
> Security audit, report then fix: confirm the only Supabase credential in the client
> bundle is the anon key (never a service key); confirm every table is accessed only
> through RLS; confirm storage downloads use short-lived signed URLs; confirm there are no
> unauthenticated routes or components rendering data before the auth check resolves;
> remove any console.log of data. List what you found and what you changed.

**H3 — stale-command guard:**
> On the Dashboard: if a `commands` row is still `pending` or `picked_up` 10+ minutes
> after creation while the engine heartbeat is stale, show it as "not picked up — engine
> offline?" with a Cancel button (sets status to `error`). Prevent duplicate `start_run`
> rows: if one is already pending, the Start button shows "Queued — waiting for engine"
> instead of inserting again.

Then, if you built only Prompts 1–3: add **4 (Review), 5 (Questions), 6 (Inbox),
7 (Analytics)** one at a time, running the matching §2 section after each.

## 4. Cleanup + the real proof

When the smoke test passes, delete the fakes:

```sql
delete from emails where gmail_id like 'seed-%';
delete from jobs where company in ('Acme Corp','Globex','Initech','Umbrella Inc','Hooli','Stark Industries','Vandelay','Pied Piper','Dunder Mifflin','Wonka Industries');
delete from questions where confidence = 'low' and times_used <= 3 and question_normalized in ('are you willing to undergo a background check','what is your desired salary');
update engine_status set online = false, current_stage = 'idle', counters = '{}' where id = 1;
```

Then the honest final state: **a proven cockpit wired to nothing.** The panel writes
`start_run` commands that no one reads yet. The last load-bearing piece is Phase 2 of the
roadmap — the `applypilot sync` daemon in this repo that picks up commands, runs the
pipeline, and streams real jobs into these same tables. Panel proven + engine dry-run
proven + sync daemon = the machine is alive. Nothing else on any list matters until sync
exists.
