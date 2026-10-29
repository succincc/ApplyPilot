// ============================================================================
// apply.mjs — the cloud applier.
//
// Runs on GitHub Actions with Playwright. This is what makes the whole system
// work from a phone: you press a button in the panel, GitHub runs this, and
// applications get submitted. Your computer is never involved.
//
// Why GitHub Actions: it is the only free way to get a real browser running
// in the cloud on a schedule. Public repositories get unlimited runner
// minutes. No resume or personal data lives in this repo — everything is
// pulled from Supabase at runtime using a secret, so the repo can be public
// and still hold nothing about you.
//
// The field-matching logic below is deliberately the same approach as the
// Chrome extension: identify fields by the LABEL a human reads, not by CSS
// selectors, because every ATS uses different markup but all of them show a
// person "First Name".
// ============================================================================

import { chromium } from "playwright";

const SUPABASE_URL = process.env.SUPABASE_URL;
const SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;
const MAX_JOBS = parseInt(process.env.MAX_JOBS || "25", 10);
const DRY_RUN = process.env.DRY_RUN === "true";

if (!SUPABASE_URL || !SERVICE_KEY) {
  console.error("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY secret.");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Supabase REST
// ---------------------------------------------------------------------------
async function db(path, init = {}) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`DB ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const rpc = (fn, args = {}) =>
  db(`rpc/${fn}`, { method: "POST", body: JSON.stringify(args) });

const patchJob = (id, patch) =>
  db(`jobs?id=eq.${id}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({ ...patch, updated_at: new Date().toISOString() }),
  });

// ---------------------------------------------------------------------------
// Field matching — shared logic with the extension.
//
// Word-boundary matching is not a nicety here. Plain substring matching put
// "FL" into "Are you legally authorized to work in the United States?"
// (because "state" is inside "States") and "Tampa" into "Race / ethnicity"
// (because "city" is inside "ethnicity"). Both went onto live applications.
// ---------------------------------------------------------------------------
function has(label, ...needles) {
  return needles.some((n) => {
    const escaped = n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`(?<![a-z])${escaped}(?![a-z])`, "i").test(label);
  });
}

/** Questions the form is ASKING. Checked before field-name matching. */
function answerQuestion(label, p) {
  if (has(label, "authorized to work", "legally authorized", "work authorization",
                 "eligible to work", "right to work")) return p.work_authorized;
  if (has(label, "sponsorship", "visa", "h-1b", "h1b", "work permit")) return p.needs_sponsorship;
  if (has(label, "18 years", "over 18", "age of majority", "at least 18")) return p.over_18;
  if (has(label, "background check", "drug screen", "drug test")) return p.can_background_check;
  if (has(label, "relocate", "relocation")) return p.willing_to_relocate;
  if (has(label, "veteran", "protected veteran")) return p.veteran_status;
  if (has(label, "disability", "disabled")) return p.disability_status;
  if (has(label, "gender", "sex", "pronouns")) return p.gender;
  if (has(label, "race", "ethnicity", "hispanic", "latino")) return p.race_ethnicity;
  if (has(label, "previously worked", "former employee", "worked here before")) return "No";
  if (has(label, "convicted", "felony", "criminal", "conviction")) return "No";
  if (has(label, "non-compete", "noncompete")) return "No";
  return null;
}

function fieldValue(label, p) {
  if (has(label, "first name", "given name", "forename")) return p.first_name;
  if (has(label, "last name", "surname", "family name")) return p.last_name;
  if (has(label, "preferred name", "nickname")) return p.preferred_name || p.first_name;
  if (has(label, "full name", "your name", "legal name")) return p.full_name;
  if (has(label, "email", "e-mail")) return p.email;
  if (has(label, "phone", "mobile", "telephone", "cell")) return p.phone;
  if (has(label, "linkedin")) return p.linkedin_url;
  if (has(label, "github")) return p.github_url;
  if (has(label, "portfolio", "website", "personal site")) return p.portfolio_url;
  if (has(label, "street", "address line 1", "address 1")) return p.address;
  if (has(label, "address") && !has(label, "email", "e-mail")) return p.address;
  if (has(label, "city", "town")) return p.city;
  if (has(label, "state", "province", "region") && !has(label, "united states")) return p.state;
  if (has(label, "zip", "postal", "postcode")) return p.postal_code;
  if (has(label, "country")) return p.country;
  if (has(label, "location") && !has(label, "relocate")) {
    return [p.city, p.state].filter(Boolean).join(", ");
  }
  if (has(label, "current company", "current employer")) return p.current_company;
  if (has(label, "current title", "current role", "job title", "position title")) return p.target_role;
  if (has(label, "years of experience", "years experience", "how many years")) return p.years_experience;
  if (has(label, "school", "university", "college")) return p.school;
  if (has(label, "degree", "education level", "highest level")) return p.education_level;
  if (has(label, "desired salary", "salary expectation", "expected salary",
                 "expected compensation", "salary requirement", "desired compensation",
                 "target salary")) {
    return p.salary_expectation != null ? String(p.salary_expectation) : null;
  }
  if (has(label, "notice period")) return "2 weeks";
  if (has(label, "start date", "available", "when can you start", "earliest")) return p.earliest_start;
  if (has(label, "how did you hear", "referral source", "referred by")) return "Company website";
  return null;
}

// ---------------------------------------------------------------------------
// Read a control's label the way a person reads it
// ---------------------------------------------------------------------------
const LABEL_SCRIPT = `(el) => {
  const parts = [];
  if (el.id) {
    const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
    if (lab) parts.push(lab.innerText);
  }
  const wrap = el.closest('label');
  if (wrap) parts.push(wrap.innerText);
  for (const a of ['aria-label','placeholder','name','id','data-qa','title']) {
    const v = el.getAttribute && el.getAttribute(a);
    if (v) parts.push(v);
  }
  const lb = el.getAttribute && el.getAttribute('aria-labelledby');
  if (lb) for (const id of lb.split(/\\s+/)) {
    const n = document.getElementById(id); if (n) parts.push(n.innerText);
  }
  let probe = el.parentElement;
  for (let d = 0; probe && d < 3; d++, probe = probe.parentElement) {
    const t = Array.from(probe.childNodes)
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent.trim())
      .filter(t => t.length > 2 && t.length < 160).join(' ');
    if (t) { parts.push(t); break; }
  }
  return parts.join(' ').replace(/\\s+/g,' ').replace(/\\*/g,'').trim().toLowerCase();
}`;

// ---------------------------------------------------------------------------
// Apply to one job
// ---------------------------------------------------------------------------
async function applyToJob(context, job, profile, answers) {
  const page = await context.newPage();
  const answerMap = new Map((answers || []).map((a) => [a.q_key, a.answer]));
  const lookupSaved = (label) => {
    if (answerMap.has(label)) return answerMap.get(label);
    for (const [k, v] of answerMap) {
      if (k.length > 12 && (label.includes(k) || k.includes(label))) return v;
    }
    return null;
  };

  const unknown = [];
  let filled = 0;

  try {
    await page.goto(job.apply_url || job.url, {
      waitUntil: "domcontentloaded", timeout: 45000,
    });
    await page.waitForTimeout(2500);

    // --- blockers, before touching anything ---
    const body = (await page.innerText("body").catch(() => "")).toLowerCase();
    if (/no longer accepting|position (has been )?filled|posting (is )?closed|expired/.test(body)) {
      return { status: "skipped", skip_reason: "posting closed" };
    }
    if (/already applied|you have applied/.test(body)) {
      return { status: "applied", note: "already applied previously" };
    }
    const captcha = await page.$(".g-recaptcha, .h-captcha, .cf-turnstile, iframe[src*='recaptcha'], iframe[src*='hcaptcha']");
    if (captcha) return { status: "needs_help", help_reason: "captcha" };
    if (await page.$("input[type=password]")) {
      return { status: "needs_help", help_reason: "login" };
    }

    // --- resume file: write it to disk and attach it ---
    // A cloud browser CAN set a file input (unlike a Chrome extension, where
    // only a human gesture can). This is the main thing the cloud agent does
    // that the extension cannot.
    const fileInputs = await page.$$("input[type=file]");
    if (fileInputs.length && job.tailored_resume) {
      const { writeFileSync } = await import("node:fs");
      const path = `/tmp/resume_${job.id}.txt`;
      writeFileSync(path, job.tailored_resume, "utf8");
      for (const input of fileInputs.slice(0, 1)) {
        await input.setInputFiles(path).catch(() => {});
      }
      await page.waitForTimeout(3000);   // ATS parse the upload
    }

    // --- fill, across multiple pages if needed ---
    for (let step = 0; step < 6; step++) {
      const controls = await page.$$("input, select, textarea");

      for (const el of controls) {
        try {
          const type = await el.getAttribute("type");
          if (["hidden", "submit", "button", "file", "image", "reset"].includes(type)) continue;
          if (!(await el.isVisible().catch(() => false))) continue;
          if (!(await el.isEnabled().catch(() => false))) continue;

          const name = (await el.getAttribute("name")) || "";
          if (/honeypot|bot[-_]?field|leave[-_]?blank/i.test(name)) continue;

          const label = await el.evaluate(LABEL_SCRIPT);
          if (!label) continue;

          const tag = await el.evaluate((e) => e.tagName);

          // checkboxes: only consent boxes
          if (type === "checkbox") {
            if (has(label, "agree", "consent", "acknowledge", "certify",
                           "terms", "privacy", "accurate")) {
              if (!(await el.isChecked())) { await el.check({ timeout: 3000 }).catch(() => {}); filled++; }
            }
            continue;
          }

          if (type === "radio") {
            const want = answerQuestion(label, profile) ?? lookupSaved(label);
            if (!want) continue;
            const val = (await el.getAttribute("value")) || "";
            if (label.includes(String(want).toLowerCase()) ||
                val.toLowerCase() === String(want).toLowerCase()) {
              await el.check({ timeout: 3000 }).catch(() => {});
              filled++;
            }
            continue;
          }

          if (tag === "SELECT") {
            const current = await el.inputValue().catch(() => "");
            if (current) continue;
            const want = answerQuestion(label, profile) ?? fieldValue(label, profile) ?? lookupSaved(label);
            if (!want) {
              unknown.push({ question: label.slice(0, 200), type: "select" });
              continue;
            }
            const ok = await el.selectOption({ label: String(want) }).then(() => true).catch(() => false);
            if (!ok) {
              // fall back to a loose text match over the options
              const matched = await el.evaluate((sel, target) => {
                const t = String(target).toLowerCase();
                const o = Array.from(sel.options).find(
                  (o) => o.value && o.text.toLowerCase().includes(t));
                if (o) { sel.value = o.value;
                  sel.dispatchEvent(new Event("change", { bubbles: true })); return true; }
                return false;
              }, want);
              if (matched) filled++;
              else unknown.push({ question: label.slice(0, 200), type: "select" });
            } else filled++;
            continue;
          }

          // text-ish
          const existing = await el.inputValue().catch(() => "");
          if (existing && existing.trim()) continue;
          const want = answerQuestion(label, profile) ?? fieldValue(label, profile) ?? lookupSaved(label);
          if (want) {
            await el.fill(String(want), { timeout: 5000 }).catch(() => {});
            filled++;
          } else if (tag === "TEXTAREA") {
            unknown.push({ question: label.slice(0, 200), type: "longtext" });
          } else if (await el.getAttribute("required") !== null) {
            unknown.push({ question: label.slice(0, 200), type: "text" });
          }
        } catch { /* one bad field must not end the application */ }
      }

      // submit if we can, otherwise advance
      const submit = await page.$(
        'button:has-text("Submit"), button:has-text("Submit Application"), ' +
        'input[type=submit], button:has-text("Send Application")');
      if (submit) break;

      const next = await page.$('button:has-text("Next"), button:has-text("Continue")');
      if (next) { await next.click().catch(() => {}); await page.waitForTimeout(2500); continue; }
      break;
    }

    if (unknown.length) await recordUnknowns(unknown);

    const submit = await page.$(
      'button:has-text("Submit"), button:has-text("Submit Application"), ' +
      'input[type=submit], button:has-text("Send Application")');

    if (!submit) {
      return { status: "needs_help", help_reason: "error",
               note: `No submit button found after filling ${filled} fields.` };
    }

    if (DRY_RUN) {
      return { status: "needs_help", help_reason: "dry_run",
               note: `Dry run: filled ${filled} fields, did not submit.` };
    }

    await submit.click({ timeout: 10000 });
    await page.waitForTimeout(5000);

    const after = (await page.innerText("body").catch(() => "")).toLowerCase();
    const ok = /thank you|application (has been )?(received|submitted|sent)|we('ve| have) received|successfully (applied|submitted)|thanks for applying/.test(after);

    if (ok) return { status: "applied", filled };

    const errors = await page.$$eval('[role=alert], .error, .field-error',
      (els) => els.map((e) => e.innerText.trim()).filter((t) => t && t.length < 200).slice(0, 5),
    ).catch(() => []);

    if (errors.length) {
      return { status: "needs_help", help_reason: "error", note: errors.join(" | ").slice(0, 300) };
    }
    return { status: "applied", filled, note: "submitted, no confirmation text seen" };
  } finally {
    await page.close().catch(() => {});
  }
}

async function recordUnknowns(unknowns) {
  for (const u of unknowns.slice(0, 12)) {
    const key = String(u.question).toLowerCase()
      .replace(/[^\w\s]/g, " ").replace(/\s+/g, " ").trim().slice(0, 200);
    if (key.length < 4) continue;
    await db("answers", {
      method: "POST",
      headers: { Prefer: "resolution=ignore-duplicates,return=minimal" },
      body: JSON.stringify([{
        q_key: key, question: String(u.question).slice(0, 300),
        answer: "", field_type: u.type ?? "text", confirmed: false,
      }]),
    }).catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
(async () => {
  const [profileRow] = await db("profile?id=eq.1&select=*");
  if (!profileRow?.email || !profileRow?.full_name) {
    console.error("Profile incomplete — set your name and email in the panel.");
    process.exit(1);
  }

  const p = { ...profileRow };
  const names = (p.full_name || "").trim().split(/\s+/);
  p.first_name = names[0] || "";
  p.last_name = names.length > 1 ? names[names.length - 1] : "";

  const answers = await db("answers?confirmed=eq.true&select=q_key,answer") ?? [];

  const [run] = await db("runs", {
    method: "POST", headers: { Prefer: "return=representation" },
    body: JSON.stringify([{ kind: "apply" }]),
  });

  const browser = await chromium.launch({
    args: ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
  });
  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    viewport: { width: 1440, height: 900 },
    locale: "en-US",
  });

  const tally = { applied: 0, needs_help: 0, skipped: 0, errors: 0 };

  try {
    for (let i = 0; i < MAX_JOBS; i++) {
      const claimed = await rpc("claim_next_job");
      const job = Array.isArray(claimed) ? claimed[0] : claimed;
      if (!job) { console.log("Queue empty."); break; }

      console.log(`[${i + 1}] ${job.title} @ ${job.company ?? "?"}`);

      let outcome;
      try {
        outcome = await applyToJob(context, job, p, answers);
      } catch (e) {
        outcome = { status: "needs_help", help_reason: "error",
                    note: String(e?.message ?? e).slice(0, 300) };
        tally.errors++;
      }

      const patch = { status: outcome.status };
      if (outcome.status === "applied") {
        patch.applied_at = new Date().toISOString();
        patch.confirm_state = "pending";
      }
      if (outcome.help_reason) patch.help_reason = outcome.help_reason;
      if (outcome.skip_reason) patch.skip_reason = outcome.skip_reason;
      if (outcome.note) patch.last_error = outcome.note;
      await patchJob(job.id, patch);

      tally[outcome.status] = (tally[outcome.status] ?? 0) + 1;
      console.log(`    -> ${outcome.status}${outcome.note ? ": " + outcome.note : ""}`);

      await new Promise((r) => setTimeout(r, 3000 + Math.random() * 4000));
    }
  } finally {
    await browser.close().catch(() => {});
    const message =
      `applied ${tally.applied}, need you ${tally.needs_help ?? 0}, skipped ${tally.skipped ?? 0}`;
    await db(`runs?id=eq.${run.id}`, {
      method: "PATCH", headers: { Prefer: "return=minimal" },
      body: JSON.stringify({
        status: "done", processed: tally.applied, errors: tally.errors,
        detail: tally, message, ended_at: new Date().toISOString(),
      }),
    }).catch(() => {});
    console.log(message);
  }
})();
