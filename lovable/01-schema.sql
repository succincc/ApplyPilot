-- ============================================================================
-- APPLYPILOT — COMPLETE DATABASE SCHEMA
-- Paste into Supabase SQL Editor, or let Lovable run it as a migration.
-- Safe to re-run: every statement is idempotent.
-- ============================================================================

create extension if not exists pg_cron;
create extension if not exists pg_net;
create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- profile — one row. Everything that gets typed onto an application form.
-- ---------------------------------------------------------------------------
create table if not exists profile (
  id                int primary key default 1 check (id = 1),
  full_name         text,
  preferred_name    text,
  email             text,
  phone             text,
  address           text,
  city              text,
  state             text,
  country           text default 'United States',
  postal_code       text,
  linkedin_url      text,
  github_url        text,
  portfolio_url     text,

  -- Work authorization. TEXT, never boolean: these are typed onto legal
  -- forms and "True" is not an answer to "are you authorized to work?"
  work_authorized   text default 'Yes',
  needs_sponsorship text default 'No',
  veteran_status    text default 'I am not a protected veteran',
  disability_status text default 'I do not wish to answer',
  gender            text default 'Decline to self-identify',
  race_ethnicity    text default 'Decline to self-identify',
  over_18           text default 'Yes',
  can_background_check text default 'Yes',

  target_role       text,
  years_experience  text,
  education_level   text,
  salary_expectation int,
  salary_min        int,
  earliest_start    text default 'Immediately',
  willing_to_relocate text default 'No',

  resume_text       text,
  resume_file_path  text,
  resume_file_name  text,
  resume_word_count int,
  resume_error      text,

  updated_at        timestamptz default now()
);
insert into profile (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- search_config — one row. What to look for.
-- ---------------------------------------------------------------------------
create table if not exists search_config (
  id               int primary key default 1 check (id = 1),
  titles           text[] default '{}',          -- ["customer success manager", ...]
  locations        text[] default '{Remote}',
  remote_ok        boolean default true,
  salary_floor     int default 0,
  exclude_titles   text[] default '{intern,internship,director,vice president,vp,chief,senior director,principal}',
  exclude_companies text[] default '{}',
  no_go_patterns   text[] default '{commission only,commission-only,100% commission,unlimited earning potential,door to door,door-to-door,1099 only,multi-level marketing,pyramid,be your own boss,purchase your own leads}',
  auto_apply_score int default 6,                -- >= this auto-applies
  review_score     int default 4,                -- >= this goes to review
  daily_cap        int default 100,
  sources          text[] default '{greenhouse,lever,ashby,remotive,remoteok,arbeitnow,workable,usajobs}',
  updated_at       timestamptz default now()
);
insert into search_config (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- companies — ATS boards to pull directly. The highest-yield source.
-- ---------------------------------------------------------------------------
create table if not exists companies (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  ats         text not null check (ats in ('greenhouse','lever','ashby','workable','smartrecruiters')),
  token       text not null,
  enabled     boolean default true,
  last_pulled timestamptz,
  job_count   int default 0,
  created_at  timestamptz default now(),
  unique (ats, token)
);

-- ---------------------------------------------------------------------------
-- jobs — the pipeline
-- ---------------------------------------------------------------------------
create table if not exists jobs (
  id            uuid primary key default gen_random_uuid(),
  url           text unique not null,
  apply_url     text,
  title         text not null,
  company       text,
  location      text,
  remote        boolean default false,
  salary_text   text,
  salary_min    int,
  salary_max    int,
  description   text,
  source        text,                    -- greenhouse | lever | remotive | ...
  ats           text,                    -- which ATS the apply form uses

  score         int,
  score_reason  text,
  scored_at     timestamptz,

  tailored_resume text,
  tailored_at   timestamptz,

  status        text not null default 'new',
  -- new | scored | review | queued | applying | applied | needs_help
  -- | interview | offer | rejected | skipped | failed
  skip_reason   text,
  help_reason   text,                    -- captcha | login | unknown_question | error

  applied_at    timestamptz,
  confirmed_at  timestamptz,
  confirm_state text,                    -- confirmed | pending | unconfirmed

  attempts      int default 0,
  last_error    text,
  posted_at     timestamptz,
  found_at      timestamptz default now(),
  updated_at    timestamptz default now()
);

create index if not exists idx_jobs_status   on jobs(status);
create index if not exists idx_jobs_score    on jobs(score desc nulls last);
create index if not exists idx_jobs_applied  on jobs(applied_at desc);
create index if not exists idx_jobs_found    on jobs(found_at desc);
create index if not exists idx_jobs_company  on jobs using gin (company gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- answers — screening-question memory. Answer once, reused forever.
-- ---------------------------------------------------------------------------
create table if not exists answers (
  id          uuid primary key default gen_random_uuid(),
  q_key       text unique not null,      -- normalized question text
  question    text not null,             -- as first seen
  answer      text not null,
  field_type  text default 'text',       -- text | select | radio | checkbox | number
  confirmed   boolean default false,     -- false = AI guessed, needs review
  times_used  int default 0,
  last_used   timestamptz,
  created_at  timestamptz default now()
);
create index if not exists idx_answers_key on answers using gin (q_key gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- emails — classified inbox
-- ---------------------------------------------------------------------------
create table if not exists emails (
  id          uuid primary key default gen_random_uuid(),
  message_id  text unique not null,
  from_addr   text,
  subject     text,
  snippet     text,
  body        text,
  received_at timestamptz,
  category    text,   -- interview | offer | rejection | action_needed | confirmation | other
  job_id      uuid references jobs(id) on delete set null,
  is_read     boolean default false,
  created_at  timestamptz default now()
);
create index if not exists idx_emails_received on emails(received_at desc);
create index if not exists idx_emails_category on emails(category);

-- ---------------------------------------------------------------------------
-- runs — log of every discover/score cycle, so nothing fails silently
-- ---------------------------------------------------------------------------
create table if not exists runs (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,            -- discover | score | tailor | inbox
  status      text default 'running',   -- running | done | error
  found       int default 0,
  processed   int default 0,
  errors      int default 0,
  detail      jsonb default '{}',
  message     text,
  started_at  timestamptz default now(),
  ended_at    timestamptz
);
create index if not exists idx_runs_started on runs(started_at desc);

-- ---------------------------------------------------------------------------
-- agent — heartbeat from the Chrome extension
-- ---------------------------------------------------------------------------
create table if not exists agent (
  id           int primary key default 1 check (id = 1),
  online       boolean default false,
  last_seen    timestamptz,
  state        text default 'idle',     -- idle | applying | paused
  current_job  uuid references jobs(id) on delete set null,
  applied_today int default 0,
  version      text
);
insert into agent (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- VIEWS the UI reads directly
-- ---------------------------------------------------------------------------
create or replace view v_stats as
select
  count(*)                                               as total,
  count(*) filter (where status = 'new')                 as new,
  count(*) filter (where status = 'review')              as review,
  count(*) filter (where status = 'queued')              as queued,
  count(*) filter (where status = 'applied')             as applied,
  count(*) filter (where status = 'needs_help')          as needs_help,
  count(*) filter (where status = 'interview')           as interviews,
  count(*) filter (where status = 'offer')               as offers,
  count(*) filter (where status = 'rejected')            as rejected,
  count(*) filter (where applied_at::date = current_date) as applied_today,
  count(*) filter (where confirm_state = 'confirmed')    as confirmed
from jobs;

-- The queue the Chrome extension pulls from. Ordered best-first.
create or replace view v_apply_queue as
select j.id, j.url, j.apply_url, j.title, j.company, j.ats, j.score,
       j.tailored_resume, j.description
from jobs j
where j.status = 'queued'
  and j.attempts < 3
order by j.score desc nulls last, j.found_at asc;

-- ---------------------------------------------------------------------------
-- AUTO-ROUTING: a scored job lands in the right lane with no extra code.
-- ---------------------------------------------------------------------------
create or replace function route_scored_job() returns trigger as $$
declare cfg record;
begin
  if new.score is null or new.score = old.score then return new; end if;
  select * into cfg from search_config where id = 1;

  if new.score >= cfg.auto_apply_score then
    new.status := 'queued';
  elsif new.score >= cfg.review_score then
    new.status := 'review';
  else
    new.status := 'skipped';
    new.skip_reason := coalesce(new.skip_reason, 'score below review threshold');
  end if;
  new.updated_at := now();
  return new;
end $$ language plpgsql;

drop trigger if exists trg_route_scored on jobs;
create trigger trg_route_scored
  before update of score on jobs
  for each row execute function route_scored_job();

-- Keep updated_at honest everywhere
create or replace function touch_updated() returns trigger as $$
begin new.updated_at := now(); return new; end $$ language plpgsql;

drop trigger if exists trg_touch_jobs on jobs;
create trigger trg_touch_jobs before update on jobs
  for each row execute function touch_updated();
drop trigger if exists trg_touch_profile on profile;
create trigger trg_touch_profile before update on profile
  for each row execute function touch_updated();

-- ---------------------------------------------------------------------------
-- ATOMIC CLAIM: stops two extension tabs grabbing the same job.
-- ---------------------------------------------------------------------------
create or replace function claim_next_job()
returns table (id uuid, url text, apply_url text, title text, company text,
               ats text, score int, tailored_resume text, description text)
language plpgsql as $$
declare cap int; done int;
begin
  select daily_cap into cap from search_config where id = 1;
  select count(*) into done from jobs where applied_at::date = current_date;
  if done >= cap then return; end if;

  return query
  with picked as (
    select j.id from jobs j
    where j.status = 'queued' and j.attempts < 3
    order by j.score desc nulls last, j.found_at asc
    limit 1
    for update skip locked
  )
  update jobs j
     set status = 'applying', attempts = j.attempts + 1, updated_at = now()
    from picked
   where j.id = picked.id
  returning j.id, j.url, j.apply_url, j.title, j.company, j.ats,
            j.score, j.tailored_resume, j.description;
end $$;

-- ---------------------------------------------------------------------------
-- ANSWER LOOKUP: exact match, then fuzzy. Used by the extension.
-- ---------------------------------------------------------------------------
create or replace function find_answer(q text)
returns table (answer text, confirmed boolean, similarity real)
language sql stable as $$
  select a.answer, a.confirmed, similarity(a.q_key, lower(trim(q))) as sim
  from answers a
  where a.q_key = lower(trim(q)) or similarity(a.q_key, lower(trim(q))) > 0.75
  order by (a.q_key = lower(trim(q))) desc, sim desc
  limit 1;
$$;

-- ---------------------------------------------------------------------------
-- ROW LEVEL SECURITY — single private user
-- ---------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['profile','search_config','companies','jobs',
                           'answers','emails','runs','agent']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists owner_all on %I', t);
    execute format(
      'create policy owner_all on %I for all to authenticated using (true) with check (true)', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- SEED: well-known ATS boards so discovery returns jobs on day one.
-- Replace these with companies you actually want. Each is a free public feed.
-- ---------------------------------------------------------------------------
insert into companies (name, ats, token) values
  ('Stripe','greenhouse','stripe'),
  ('Databricks','greenhouse','databricks'),
  ('Instacart','greenhouse','instacart'),
  ('Robinhood','greenhouse','robinhood'),
  ('Doximity','greenhouse','doximity'),
  ('Cloudflare','greenhouse','cloudflare'),
  ('Reddit','greenhouse','reddit'),
  ('Gusto','greenhouse','gusto'),
  ('Airtable','greenhouse','airtable'),
  ('Discord','greenhouse','discord'),
  ('Netflix','lever','netflix'),
  ('Plaid','lever','plaid'),
  ('Attentive','lever','attentive'),
  ('Ramp','ashby','ramp'),
  ('Linear','ashby','linear'),
  ('Deel','ashby','deel')
on conflict (ats, token) do nothing;
