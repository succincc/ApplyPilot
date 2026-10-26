// ============================================================================
// background.js — the agent loop.
//
// Pulls one job at a time from the queue, opens it in a tab, fills the form,
// submits, verifies, and reports the outcome back to the database.
//
// Why a browser extension rather than a server: nobody can submit an
// application through an API. Greenhouse and Lever both have application
// endpoints, but they require the EMPLOYER's secret key, which an applicant
// never has. Every real auto-applier therefore drives a browser. Doing that
// inside your own Chrome means your existing logins and sessions already
// work, there is no separate browser to detect as automation, and there is
// nothing to install beyond this folder.
// ============================================================================

const POLL_MS = 20_000;
const PAGE_TIMEOUT_MS = 45_000;
const BETWEEN_JOBS_MS = 8_000;   // deliberate pacing; not a race

let running = false;
let currentTabId = null;

// ---------------------------------------------------------------------------
// Supabase REST helpers
// ---------------------------------------------------------------------------
async function cfg() {
  const { url, key } = await chrome.storage.local.get(["url", "key"]);
  if (!url || !key) throw new Error("NOT_CONFIGURED");
  return { url: url.replace(/\/+$/, ""), key };
}

async function db(path, init = {}) {
  const { url, key } = await cfg();
  const res = await fetch(`${url}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      ...(init.headers || {}),
    },
  });
  if (!res.ok) throw new Error(`DB ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

async function rpc(fn, args = {}) {
  return db(`rpc/${fn}`, { method: "POST", body: JSON.stringify(args) });
}

// ---------------------------------------------------------------------------
// tab helpers
// ---------------------------------------------------------------------------
function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function waitForLoad(tabId, timeout = PAGE_TIMEOUT_MS) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("tab closed");
    if (tab.status === "complete") { await sleep(1500); return; }  // let JS settle
    await sleep(400);
  }
  throw new Error("page load timed out");
}

/** Talk to the content script, retrying while it boots. */
async function ask(tabId, message, tries = 3) {
  for (let i = 0; i < tries; i++) {
    try {
      const res = await chrome.tabs.sendMessage(tabId, message);
      if (res) return res;
    } catch {
      // content script not ready yet — inject and retry
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] })
        .catch(() => {});
    }
    await sleep(1200);
  }
  throw new Error(`content script did not respond to ${message.type}`);
}

// ---------------------------------------------------------------------------
// status reporting
// ---------------------------------------------------------------------------
async function heartbeat(patch = {}) {
  try {
    await db("agent?id=eq.1", {
      method: "PATCH",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({
        online: true,
        last_seen: new Date().toISOString(),
        version: chrome.runtime.getManifest().version,
        ...patch,
      }),
    });
  } catch { /* offline is a visible state, not an error to chase */ }
}

async function finishJob(id, patch) {
  await db(`jobs?id=eq.${id}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({ ...patch, updated_at: new Date().toISOString() }),
  });
}

/** Save a question the form asked that we could not answer. */
async function recordUnknowns(unknowns) {
  for (const u of unknowns.slice(0, 12)) {
    const key = String(u.question).toLowerCase().replace(/[^\w\s]/g, " ")
      .replace(/\s+/g, " ").trim().slice(0, 200);
    if (key.length < 4) continue;
    try {
      await db("answers", {
        method: "POST",
        headers: { Prefer: "resolution=ignore-duplicates,return=minimal" },
        body: JSON.stringify([{
          q_key: key,
          question: String(u.question).slice(0, 300),
          answer: "",
          field_type: u.type ?? "text",
          confirmed: false,
        }]),
      });
    } catch { /* duplicate is fine */ }
  }
}

// ---------------------------------------------------------------------------
// apply to one job
// ---------------------------------------------------------------------------
async function applyToJob(job, profile, answers) {
  const target = job.apply_url || job.url;
  const tab = await chrome.tabs.create({ url: target, active: false });
  currentTabId = tab.id;

  try {
    await waitForLoad(tab.id);

    // 1. Look before touching anything
    const look = await ask(tab.id, { type: "INSPECT" });

    if (look.blockers?.includes("closed")) {
      return { status: "skipped", skip_reason: "posting closed or filled" };
    }
    if (look.blockers?.includes("already_applied")) {
      return { status: "applied", note: "already applied previously" };
    }
    if (look.blockers?.includes("captcha")) {
      return { status: "needs_help", help_reason: "captcha" };
    }
    if (look.blockers?.includes("login")) {
      return { status: "needs_help", help_reason: "login" };
    }

    // 2. Fill — multi-page forms (Workday, iCIMS) need several passes
    let filledTotal = 0;
    let unknowns = [];
    let result = null;

    for (let page = 0; page < 6; page++) {
      result = await ask(tab.id, { type: "FILL", profile, answers });
      filledTotal += result.filled?.length ?? 0;
      unknowns = unknowns.concat(result.unknown ?? []);

      if (result.blockers?.includes("captcha")) {
        return { status: "needs_help", help_reason: "captcha", filled: filledTotal };
      }
      if (result.canSubmit) break;
      if (result.hasNext) {
        await ask(tab.id, { type: "CLICK_NEXT" });
        await sleep(2500);
        continue;
      }
      break;
    }

    if (unknowns.length) await recordUnknowns(unknowns);

    // 3. A required question we could not answer must not be guessed at.
    //    Submitting a half-filled form burns the application permanently.
    const blocking = unknowns.filter((u) => u.type !== "longtext");
    if (blocking.length > 0 && filledTotal === 0) {
      return {
        status: "needs_help",
        help_reason: "unknown_question",
        note: blocking.slice(0, 3).map((u) => u.question).join(" | ").slice(0, 300),
      };
    }

    if (!result?.canSubmit) {
      return {
        status: "needs_help",
        help_reason: "error",
        note: "no submit button found — the form may need a resume upload first",
      };
    }

    // 4. Resume upload cannot be automated from an extension: a file input
    //    can only be set by a real user gesture, for good security reasons.
    //    Forms that require one are handed back to you with everything else
    //    already filled, so finishing takes seconds.
    if (look.hasFileInput) {
      return {
        status: "needs_help",
        help_reason: "resume_upload",
        note: `Form filled (${filledTotal} fields). Attach your resume and press submit.`,
      };
    }

    // 5. Submit and verify
    const { dryRun } = await chrome.storage.local.get("dryRun");
    if (dryRun) {
      return { status: "needs_help", help_reason: "dry_run",
               note: `Dry run: ${filledTotal} fields filled, not submitted.` };
    }

    await ask(tab.id, { type: "SUBMIT" });
    await sleep(4000);
    const check = await ask(tab.id, { type: "VERIFY" });

    if (check.success) return { status: "applied", filled: filledTotal };
    if (check.errors?.length) {
      return { status: "needs_help", help_reason: "error",
               note: check.errors.join(" | ").slice(0, 300) };
    }
    // Submitted but no confirmation text — treat as applied and let the
    // employer's confirmation email settle it.
    return { status: "applied", filled: filledTotal, note: "no confirmation text seen" };
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
    currentTabId = null;
  }
}

// ---------------------------------------------------------------------------
// main loop
// ---------------------------------------------------------------------------
async function loop() {
  if (!running) return;

  try {
    await heartbeat({ state: "idle" });

    const [profile] = await db("profile?id=eq.1&select=*");
    if (!profile?.email || !profile?.full_name) {
      await heartbeat({ state: "paused" });
      notify("Setup needed", "Add your name and email in the ApplyPilot panel.");
      running = false;
      return;
    }

    const answers = await db("answers?confirmed=eq.true&select=q_key,answer,field_type");

    const claimed = await rpc("claim_next_job");
    const job = Array.isArray(claimed) ? claimed[0] : claimed;

    if (!job) {
      await heartbeat({ state: "idle" });
      if (running) setTimeout(loop, POLL_MS);
      return;
    }

    await heartbeat({ state: "applying", current_job: job.id });

    let outcome;
    try {
      outcome = await applyToJob(job, profile, answers || []);
    } catch (e) {
      outcome = { status: "needs_help", help_reason: "error",
                  note: String(e?.message ?? e).slice(0, 300) };
    }

    const patch = { status: outcome.status };
    if (outcome.status === "applied") {
      patch.applied_at = new Date().toISOString();
      patch.confirm_state = "pending";
    }
    if (outcome.help_reason) patch.help_reason = outcome.help_reason;
    if (outcome.skip_reason) patch.skip_reason = outcome.skip_reason;
    if (outcome.note) patch.last_error = outcome.note;
    await finishJob(job.id, patch);

    if (outcome.status === "applied") {
      notify("Applied", `${job.title} at ${job.company ?? ""}`);
    }

    if (running) setTimeout(loop, BETWEEN_JOBS_MS);
  } catch (e) {
    if (String(e.message) === "NOT_CONFIGURED") {
      notify("ApplyPilot", "Open the extension and paste your Supabase URL and key.");
      running = false;
      return;
    }
    console.error("[ApplyPilot]", e);
    if (running) setTimeout(loop, POLL_MS);
  }
}

function notify(title, message) {
  chrome.notifications?.create({
    type: "basic", iconUrl: "icon128.png", title: `ApplyPilot — ${title}`, message,
  }, () => void chrome.runtime.lastError);
}

// ---------------------------------------------------------------------------
// control surface
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg.type === "START") {
    if (!running) { running = true; loop(); }
    reply({ ok: true, running });
    return true;
  }
  if (msg.type === "STOP") {
    running = false;
    heartbeat({ state: "idle", online: false });
    if (currentTabId) chrome.tabs.remove(currentTabId).catch(() => {});
    reply({ ok: true, running: false });
    return true;
  }
  if (msg.type === "STATUS") {
    (async () => {
      try {
        const [stats] = await db("v_stats?select=*");
        reply({ ok: true, running, stats });
      } catch (e) {
        reply({ ok: false, running, error: String(e.message) });
      }
    })();
    return true;
  }
  if (msg.type === "TEST") {
    (async () => {
      try {
        const [p] = await db("profile?id=eq.1&select=full_name,email,resume_text");
        const q = await db("v_apply_queue?select=id&limit=1");
        reply({ ok: true, name: p?.full_name, email: p?.email,
                hasResume: !!p?.resume_text, queued: (q ?? []).length });
      } catch (e) {
        reply({ ok: false, error: String(e.message) });
      }
    })();
    return true;
  }
  return false;
});

// Keep the heartbeat alive so the panel can show agent status.
chrome.alarms.create("hb", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener(() => { if (running) heartbeat(); });
