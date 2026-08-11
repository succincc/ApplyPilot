# Cost — where the money isn't

**Short version: this system costs $0/month, and the "AI budget" is a count of
API requests per day, not dollars.** Nothing in ApplyPilot bills you, and
nothing can start billing you without you personally clicking a button in
Google Cloud that you should never click.

---

## The "budget" is a speed limit, not a bill

Gemini's free tier allows roughly 1,500 API requests per day. ApplyPilot
tracks how many it has used today and stops at ~1,400 so it never runs off
the end.

That guard is not protecting your wallet — it is protecting your **runs**.
Here is what it prevents:

Scoring fires one request per *discovered* job. A wide discovery day finds
800 jobs, so scoring alone spends 800 requests. Tailoring then starts, gets
HTTP 429 (rate limited), and the client falls into exponential backoff. The
result is a run that burns hours, looks busy, and submits nothing.

With the budget, scoring stops at its reserved share, tailoring and cover
letters keep the quota they need, and anything left over resumes tomorrow.

## Why exceeding the limit cannot cost you anything

| | What actually happens |
|---|---|
| Credit card required for the free tier? | **No.** You cannot be charged on an account with no payment method attached. |
| Exceed the daily limit? | HTTP **429** — "try again later." Google refuses the request. It does not bill you. |
| Auto-upgrade to paid? | **No.** There is no automatic upgrade path. |
| Free tier expiry? | None. It does not expire. |

## The one thing that WOULD cost money

**Do not click "Enable billing" on the Google account holding your API key.**

Enabling billing does not "add headroom on top of the free tier" — it
**deletes** the free tier for that project. Every request becomes billable
from that moment, with no free allowance at all. The free tier only exists
while billing is off.

If you ever see a prompt offering to raise your quota by adding a card, that
is the paid tier. Decline it. ApplyPilot is designed to live inside the free
limits permanently.

(Note that `OPENAI_API_KEY` is a genuinely paid option and is supported only
for people who already have credits. The default path — Gemini — is free.)

## What 1,400 requests/day actually buys

Per-stage reservations and roughly what they produce:

| Stage | Reserved | What that covers |
|---|---|---|
| score | 672 | ~670 jobs triaged against your resume |
| tailor | 280 | ~215 tailored resumes (allowing for validation retries) |
| cover | 196 | ~165 cover letters |
| enrich | 112 | AI description extraction for unusual page layouts |
| coach | 56 | interview prep packs + follow-up drafts |
| mail | 42 | ambiguous email classification only |
| apply | 42 | screening-answer help during submission |

The binding constraint is tailoring, which supports roughly **150–200
applications per day**. That is comfortably above the 50–150/day target, and
above what JobCopilot's paid Elite plan allows (50/day).

Check your usage any time:

```bash
applypilot status      # per-stage table with today's usage
```

The panel shows the same thing as a progress bar on the Dashboard.

## If you ever want more than the free tier gives

Run a local model instead — genuinely unlimited, still $0:

```bash
# Install Ollama (ollama.com), then:
ollama pull llama3.1:8b
# In ~/.applypilot/.env:
LLM_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b
```

Budgeting disables itself automatically when `LLM_URL` is set, because there
is no quota to manage. The trade is quality: a local 8B model writes weaker
cover letters than Gemini. A reasonable hybrid is Gemini for tailoring and
cover letters, a local model for scoring — scoring is high-volume and
low-nuance, which is exactly where a small model holds up.

## Full cost table

| Component | Provider | Cost |
|---|---|---|
| Control panel build | Lovable free tier | $0 |
| Panel hosting | Lovable, or export to Vercel/Netlify | $0 |
| Database, storage, auth, realtime | Supabase free tier | $0 |
| AI (scoring, tailoring, letters, prep, triage) | Gemini free tier | $0 |
| Job discovery | Free public APIs + scraping | $0 |
| Email | IMAP with an app password | $0 |
| Auto-apply compute | Your own PC and Chrome | $0 |
| CAPTCHA solving | You, via the Attention Queue | $0 |

The only optional paid item in the entire stack is `CAPSOLVER_API_KEY`, and
it is off by default. Without it, CAPTCHA-blocked applications land in the
Attention Queue for you to clear in a batch — which is why the system needs
no payment method anywhere.
