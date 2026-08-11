# Go Live — from fake data to real applications

## First: the numbers in your panel are fake

The 142 discovered / 98 scored / 37 applied today, plus both Attention Queue
cards (Initech, Umbrella Inc) are the **seed rows from HARDENING.md §1** that you
pasted into Supabase so the panel wouldn't look empty while you tested it. They
are fictional companies. **Zero applications have been submitted.** Nothing is
running.

That is exactly why the panel says "Engine offline" — there is no engine running,
and until `applypilot init` has been run there is no resume, no profile, and
nothing to apply *with*. This document closes that gap.

(Small panel bug worth one Lovable prompt: the Run control shows a red **Stop**
button while the engine is offline, because the seed row has
`current_stage='apply'`. Fix: *"On the Dashboard, when the engine is offline
(heartbeat older than 2 minutes), always show the Start button — never Stop —
regardless of current_stage."*)

---

## Step 1 — Give the system a resume and a you

On the computer that will run the engine:

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init
```

`applypilot init` is the step that has never been run. It collects:

| What it asks for | Where it goes | Why it matters |
|---|---|---|
| Your resume (text/PDF) | `~/.applypilot/resume.txt` + `resume.pdf` | Source for every tailored resume and every uploaded PDF. **Without this nothing can be submitted.** |
| Name, email, phone, address | `profile.json` → `personal` | Fills every application form |
| Work authorization | `profile.json` → `work_authorization` | Answered truthfully on every form |
| Salary expectations | `profile.json` → `compensation` | Drives the salary decision tree |
| EEO defaults | `profile.json` → `eeo_voluntary` | Race / veteran / disability — answered once, reused everywhere |
| Gemini API key | `~/.applypilot/.env` | Free at [aistudio.google.com](https://aistudio.google.com) |

Then verify:

```bash
applypilot doctor     # every line should read OK; aim for Tier 3
```

---

## Step 2 — Set the email that receives everything

In `~/.applypilot/profile.json`:

```json
"personal": {
  "email": "succincc@gmail.com",
  ...
}
```

**This one field is how you get application confirmations.** There is no separate
notification system to build — the "Thanks for your application" emails come from
the *employer's* ATS (Greenhouse, Workday, Lever…), automatically, because that
is the email the bot types into their form. Set it here and confirmations start
arriving on their own.

That makes those emails more than receipts: a confirmation from Greenhouse is
**independent proof the application actually landed**, separate from the bot's own
claim that it succeeded. The Inbox screen's "Confirmation" category is your audit
trail — if the bot reports 40 applied and only 12 confirmations arrive, that gap
is the real signal, and it is worth investigating.

Same field also matters for logins: Workday and similar portals email a
verification code during account creation. The apply agent has read-only Gmail
access (already configured in the launcher's MCP setup) to fetch those codes and
keep going hands-free. One-time Google OAuth consent on first run.

---

## Step 3 — Connect the engine to your panel

Add to `~/.applypilot/.env`:

```bash
SUPABASE_URL=https://<your-project-ref>.supabase.co
SUPABASE_SERVICE_KEY=<service_role key>
```

Both from Supabase → Settings → API. Use the **service_role** key (it stays on
your machine, never in the browser).

Clear the fake rows (HARDENING.md §4), then start the bridge:

```bash
applypilot sync
```

The panel's status pill turns green within 30 seconds. Now the Start button is
live: pressing it writes a `start_run` command, `sync` picks it up within 15
seconds, renders your active preset into `searches.yaml`, and launches the real
pipeline.

**The bridge refuses to start a run if setup is incomplete.** No resume, no
profile, placeholder email, or no API key → the command is marked `error`, and
the panel's stage shows `blocked: setup incomplete` with the exact reasons. It
will never silently report success while submitting nothing.

---

## Step 4 — Prove one real application before trusting volume

```bash
applypilot run all          # discover -> enrich -> score -> tailor -> cover
applypilot apply --dry-run --limit 1
```

Dry run fills a real form on a real posting and stops before clicking Submit.
Watch the Chrome window. If the fields are correct, remove `--dry-run` and let
one real application go through. Check `succincc@gmail.com` for the confirmation.

**That confirmation email is the moment the machine is real.** Everything before
it is setup; everything after it is volume.

Then raise the throttle: press Start in the panel and let the daily cap
(default 100) do the work.

---

## What now happens automatically

- **Screening questions** — every question the agent answers is logged to the
  question bank (`QLOG` lines parsed out of each run). Repeats reuse the stored
  answer verbatim; new ones land in the panel's Questions tab marked low
  confidence. Confirm once, and your answer is authoritative forever — the agent
  can never overwrite a human-confirmed answer.
- **CAPTCHAs** — solved automatically when `CAPSOLVER_API_KEY` is set; otherwise
  the job is parked in the Attention Queue with reason `captcha` and the run
  continues to the next job. Volume never stalls on one blocked form.
- **Logins** — the agent signs in or creates accounts with the credentials in
  `profile.json`, fetching email verification codes from Gmail. SSO (Google /
  Microsoft sign-in) is deliberately refused and parked instead.
- **Human decisions** — approvals and rejections you make in the panel sync back
  down to the engine and always win over automated status.
