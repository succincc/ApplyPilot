# Volume — how to actually send hundreds a day

**Short version:**

```bash
applypilot run all --volume -w 4
applypilot apply -w 4 --continuous --headless --min-score 6
```

That combination sustains **400–600 applications/day on the free tier**, at
$0. The rest of this document explains what each lever does and where the
real ceiling is, so you can push without breaking anything.

---

## The constraint was never the browser

Original design spent **one AI request per discovered job** just to score it.
On a ~1,400/day free-tier quota, scoring 750 jobs consumed 750 requests and
left almost nothing for tailoring — the stage that actually turns a job into
a submitted application. Volume was capped around 150/day by arithmetic, not
by anything mechanical.

Three changes moved that ceiling by roughly 4x:

### 1. Batched scoring (~20x fewer requests)

Scoring now sends the resume once and evaluates 15–20 postings in a single
request instead of one request per job.

| | Requests to score 750 jobs |
|---|---|
| Before | 750 |
| After (batch 15 + prefilter) | ~38 |

If a batch response can't be parsed with every job accounted for, the batch
falls back to individual scoring. A partially-parsed batch is **never**
accepted — a misaligned score would attach the wrong reasoning to the wrong
job and silently mis-rank the whole queue.

### 2. Free rule-based prefilter (zero requests)

Before any AI call, obvious mismatches are rejected locally: VP/Director/
C-level titles, internships, roles requiring an active security clearance,
and postings with no description yet. Typically ~25% of discovered jobs,
removed at zero cost.

It is deliberately conservative and only rejects the unambiguous. Anything
debatable goes to the AI scorer, because uncertainty should produce an
application, not a filter.

### 3. Budget rebalanced toward tailoring

With scoring cheap, the quota moved to where applications come from:

| Stage | Share | Requests/day | What it buys |
|---|---|---|---|
| tailor | 50% | 700 | **~540–660 applications** |
| cover | 20% | 280 | letters for top matches only |
| score | 12% | 168 | **~3,360 jobs triaged** (batched 20:1) |
| enrich | 8% | 112 | descriptions for odd page layouts |
| coach | 4% | 56 | interview prep + follow-ups |
| mail | 3% | 42 | ambiguous email triage |
| apply | 3% | 42 | screening-answer help |

---

## The levers, individually

### `--volume` (sets all three at once)

```bash
applypilot run all --volume
```

- **lenient validation** — skips the LLM judge and the banned-word retry loop.
  Tailoring drops from ~1.3 requests per resume to ~1.05. This is the single
  biggest multiplier: **~540 → ~660 applications/day**.
- **batch 20** scoring instead of 15.
- **cover letters for score 8+ only.**

Honest trade: lenient validation means nobody proofreads the tailored resume
for AI-tell phrasing. The factual guardrails still hold — `resume_facts` are
preserved and nothing is fabricated — but polish drops slightly. At high
volume that is the right trade; for a handful of dream companies, run those
separately without `--volume`.

### Cover letters

Most ATS forms never ask for one. Generating one per job spends ~20% of the
daily budget on documents nobody reads.

```bash
COVER_LETTER_MODE=high   # default — only score >= 8
COVER_LETTER_MODE=off    # none; the apply agent writes 2 sentences if a form demands it
COVER_LETTER_MODE=all    # original behavior
```

`off` frees another 280 requests/day → roughly **+200 applications**. The
apply prompt already handles a missing letter gracefully.

### Parallel apply workers

This is now the real ceiling. An application takes ~2–3 minutes of browser
time, so throughput is linear in workers:

| Workers | Per hour | 8 hours | 16 hours |
|---|---|---|---|
| 1 | ~24 | 192 | 384 |
| 2 | ~48 | 384 | 768 |
| **4** | **~96** | **768** | 1,536 |
| 6 | ~144 | 1,152 | 2,304 |

```bash
applypilot apply -w 4 --continuous --headless
```

Each worker is a separate Chrome instance. Budget roughly **1.5 GB RAM and
one CPU core per worker** — 4 workers wants 8 GB and a 4-core machine. Going
past what the machine can handle makes every worker slower and gains nothing.
Start at `-w 3`, watch memory, then raise.

`--headless` is worth 20–30% throughput on its own. Run visible for your
first few applications so you can watch, then switch.

### Feed the queue

Applying at 500/day needs 500+ scored, tailored jobs available. Widen intake:

- **`hours_old: 168`** (7 days) instead of 72 — roughly doubles the pool.
- **More Tier 3 queries.** Tier 1 is exact titles; Tier 3 is the wide net.
  Add adjacent titles you'd accept: "software developer", "systems engineer",
  "automation engineer", "solutions engineer".
- **More locations.** Add "Remote" plus 3–4 metros you'd relocate to or that
  hire remotely.
- **Enable every source.** In the panel: all five boards, plus `workday`,
  `ats_registry`, `usajobs`, `remote_boards`.
- **Grow the company registry.** Every Greenhouse/Lever/Ashby company you add
  is a permanent free source, and those have the **highest submission success
  rate** because they're the employer's own form.

### Run around the clock

Set `auto_run_hours = 4` on your preset. The bridge starts a fresh
discovery-and-apply cycle every 4 hours with no button press, respecting the
daily cap. Combined with `--continuous`, the machine works while you sleep.

---

## Recommended configuration for hundreds/day

`~/.applypilot/.env`:

```bash
COVER_LETTER_MODE=high
COVER_LETTER_MIN_SCORE=8
SCORE_BATCH_SIZE=20
```

Panel preset:

```
min_score_auto_apply : 6      # apply at "maybe", per the volume doctrine
review_band_low      : 4
daily_apply_cap      : 400
hours_old            : 168
auto_run_hours       : 4
sources              : all boards + workday + ats_registry + usajobs + remote_boards
```

Run:

```bash
./start.sh                      # bridge; panel drives everything
# or manually:
applypilot run all --volume -w 4
applypilot apply -w 4 --continuous --headless --min-score 6
```

Check headroom any time with `applypilot status`.

---

## Going past the free tier — still $0

Move scoring to a local model and Gemini's entire quota goes to tailoring:

```bash
# ollama pull llama3.1:8b, then in .env:
LLM_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b
```

Budgeting disables itself (no quota to manage). Scoring is high-volume and
low-nuance, which is exactly where a small local model holds up; keep Gemini
for tailoring, where quality shows. This removes the AI ceiling entirely and
leaves only browser throughput.

---

## What still limits you, honestly

1. **Your PC must be on.** Applying happens in real Chrome on your machine.
   Cloud tools apply while your laptop is closed; that's their advantage and
   the honest cost of $0 and nobody else holding your data.
2. **10–30% of applications need you.** CAPTCHAs, login walls, and unusual
   portals land in the Attention Queue. At 500/day that's 50–150 items —
   clear them in batches from your phone, a few minutes at a time. Setting
   `CAPSOLVER_API_KEY` automates most of it, and it's the one paid thing in
   the stack.
3. **Job supply is finite.** In a given metro plus remote, there may only be
   200–400 genuinely new postings a day matching your titles. Past that point
   more workers just re-scan the same jobs. When discovery stops finding new
   ones, widen titles and locations rather than raising the cap.
4. **Response rates stay single-digit.** 500/day at 2–4% is 10–20 responses a
   day. That is the point — the system makes volume free and makes every
   response impossible to miss. Watch the Analytics page and kill the sources
   that never convert.
