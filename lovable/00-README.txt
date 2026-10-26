================================================================================
  APPLYPILOT — START HERE
================================================================================

WHAT THIS IS

  A job application system that finds jobs, scores them against your resume,
  rewrites your resume for each one, and submits the applications. Free, with
  no subscription and no credit card anywhere.

  It is three pieces. Each does the one thing it is actually capable of:

    PANEL        Lovable + Supabase.  What you look at and control.
    FUNCTIONS    Supabase Edge.       Finds jobs, scores, tailors. Cloud,
                                      24/7, does not need your computer on.
    EXTENSION    Chrome.              Fills and submits the forms.


WHY AN EXTENSION, AND NOT ALL IN THE WEBSITE

  This is the question that decides the whole design, so it is worth being
  blunt about.

  No API exists for an applicant to submit a job application. Greenhouse and
  Lever both publish application endpoints, but both require the EMPLOYER's
  secret key — the key a company generates for its own integrations. An
  applicant never has it. Reading jobs is open; submitting is not.

  And no web page can fill in a form on a different website. That is the
  browser's core security model, not a missing feature.

  So something has to drive a real browser. The paid competitors all do this:
  Simplify, Teal and LazyApply are Chrome extensions. Running in YOUR Chrome
  means your existing logins already work, there is no second browser to
  detect as automation, and there is nothing to install but a folder.

  What this buys you over the earlier design: no Python, no Node, no CLI, and
  job discovery runs in the cloud instead of only when your laptop is awake.


FILES

  01-schema.sql           the database. Paste into Supabase SQL Editor.
  02-LOVABLE-PROMPT.txt   paste into Lovable. Builds the panel.
  03-DEPLOY.txt           step-by-step setup. 25 minutes.

  edge/discover/index.ts  pulls jobs from 8 free sources, server-side
  edge/score/index.ts     scores with Gemini free tier, tailors the winners

  extension/              load unpacked in Chrome


ORDER

  1. 01-schema.sql into Supabase
  2. 02-LOVABLE-PROMPT.txt into Lovable
  3. Follow 03-DEPLOY.txt for the functions and the extension

  The panel works after step 2, but the RUN buttons will error until step 3
  deploys the functions, and nothing gets submitted until the extension is
  loaded. That is expected, and the panel says so on screen rather than
  failing quietly.


WHAT IT COSTS

  Supabase free      500MB database, 500k function calls/month
  Gemini free        ~1,500 model requests/day, no card required
  Chrome extension   free, loaded from a folder
  Lovable free       builds the panel

  Total: $0/month. The only way to start paying is to click "Enable billing"
  in Google Cloud, which DELETES the free tier rather than extending it.
  Don't.


HOW MANY APPLICATIONS A DAY

  Discovery and scoring are not the constraint — scoring is batched 20 jobs
  per model request, so the daily Gemini quota covers thousands of postings.

  The constraint is the browser: roughly 1-3 minutes per application, in one
  tab at a time, only while Chrome is open. That is 20-40 per hour, so a few
  hundred a day if you leave it running.

  Expect 10-30% to land in "need you" — CAPTCHAs, login walls, and forms
  that require a resume file upload. A file input can only be set by a real
  human click; that is a browser security rule, not a gap in this code.
  Those come back to you with every other field already filled, so clearing
  one takes seconds.


HONEST LIMITS

  * Applying needs Chrome open. Finding and scoring jobs does not.
  * LinkedIn and Indeed prohibit automated applying. This system deliberately
    goes to the employer's own application form instead, which is both
    allowed and far more likely to actually submit.
  * Response rates are single digits no matter what. The point is that
    volume costs you nothing and every reply is tracked.
  * The resume rewriting reorders and re-emphasises. It is instructed never
    to invent an employer, a date, a degree or a metric. Read the first few
    it produces before trusting it with hundreds.
