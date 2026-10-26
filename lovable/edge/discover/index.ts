// ============================================================================
// discover — pulls jobs from every free public source, server-side.
//
// Runs on Supabase Edge Functions (Deno). Server-side matters: there is no
// CORS, no browser fingerprint, and no bot detection on these endpoints —
// they are public JSON feeds meant to be read by machines.
//
// Sources (all free, most need no key at all):
//   greenhouse  boards-api.greenhouse.io      per company, full descriptions
//   lever       api.lever.co                  per company, full descriptions
//   ashby       api.ashbyhq.com               per company, incl. compensation
//   workable    apply.workable.com            per company
//   remotive    remotive.com/api              global remote feed
//   remoteok    remoteok.com/api              global remote feed
//   arbeitnow   arbeitnow.com/api             global feed
//   usajobs     data.usajobs.gov              US federal (free key)
//
// Jobs from the per-company ATS sources are the highest value: they link
// straight at the employer's own application form, which is both the most
// reliable thing to automate and the least likely to be a reposted ghost ad.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const UA = "ApplyPilot/1.0 (job search automation; contact via app)";
const TIMEOUT_MS = 15_000;

type Job = {
  url: string;
  apply_url?: string;
  title: string;
  company?: string;
  location?: string;
  remote?: boolean;
  salary_text?: string;
  description?: string;
  source: string;
  ats?: string;
  posted_at?: string;
};

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

async function getJSON(url: string, headers: Record<string, string> = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": UA, Accept: "application/json", ...headers },
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** Strip HTML to readable text. Job descriptions arrive as HTML from most ATS. */
function clean(html?: string | null): string {
  if (!html) return "";
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|h[1-6])>/gi, "\n")
    .replace(/<li[^>]*>/gi, "• ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&#39;|&rsquo;/g, "'").replace(/&quot;|&ldquo;|&rdquo;/g, '"')
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Parse "$95,000-$120,000", "95k-120k", "$50/hr" into annual min/max. */
function parseSalary(text?: string | null): { min?: number; max?: number } {
  if (!text) return {};
  const t = String(text).toLowerCase();
  const hourly = /hour|hr|hourly/.test(t);
  const nums: number[] = [];
  for (const m of t.matchAll(/(\d[\d,]*\.?\d*)\s*(k)?/g)) {
    let v = parseFloat(m[1].replace(/,/g, ""));
    if (isNaN(v)) continue;
    if (m[2]) v *= 1000;
    if (hourly && v >= 5 && v <= 500) v *= 2080;
    else if (v < 1000) continue;
    if (v >= 1000 && v <= 10_000_000) nums.push(Math.round(v));
  }
  if (!nums.length) return {};
  return { min: Math.min(...nums), max: Math.max(...nums) };
}

const REMOTE_RE = /\b(remote|anywhere|work from home|wfh|distributed|telecommute)\b/i;

// --------------------------------------------------------------------------
// per-company ATS sources
// --------------------------------------------------------------------------

async function greenhouse(token: string, name: string): Promise<Job[]> {
  const data = await getJSON(
    `https://boards-api.greenhouse.io/v1/boards/${token}/jobs?content=true`,
  );
  return (data.jobs ?? []).map((j: any) => ({
    url: j.absolute_url,
    apply_url: j.absolute_url,
    title: j.title,
    company: name,
    location: j.location?.name,
    remote: REMOTE_RE.test(j.location?.name ?? ""),
    description: clean(j.content),
    source: "greenhouse",
    ats: "greenhouse",
    posted_at: j.updated_at,
  }));
}

async function lever(token: string, name: string): Promise<Job[]> {
  const data = await getJSON(`https://api.lever.co/v0/postings/${token}?mode=json`);
  return (data ?? []).map((j: any) => ({
    url: j.hostedUrl,
    apply_url: j.applyUrl ?? j.hostedUrl,
    title: j.text,
    company: name,
    location: j.categories?.location,
    remote: REMOTE_RE.test(j.categories?.location ?? "") ||
            REMOTE_RE.test(j.workplaceType ?? ""),
    salary_text: j.salaryRange
      ? `${j.salaryRange.min}-${j.salaryRange.max} ${j.salaryRange.currency ?? ""}`
      : undefined,
    description: clean(j.descriptionPlain ?? j.description),
    source: "lever",
    ats: "lever",
    posted_at: j.createdAt ? new Date(j.createdAt).toISOString() : undefined,
  }));
}

async function ashby(token: string, name: string): Promise<Job[]> {
  const data = await getJSON(
    `https://api.ashbyhq.com/posting-api/job-board/${token}?includeCompensation=true`,
  );
  return (data.jobs ?? []).map((j: any) => ({
    url: j.jobUrl,
    apply_url: j.applyUrl ?? j.jobUrl,
    title: j.title,
    company: name,
    location: j.location,
    remote: !!j.isRemote || REMOTE_RE.test(j.location ?? ""),
    salary_text: j.compensation?.compensationTierSummary,
    description: clean(j.descriptionPlain ?? j.descriptionHtml),
    source: "ashby",
    ats: "ashby",
    posted_at: j.publishedAt,
  }));
}

async function workable(token: string, name: string): Promise<Job[]> {
  const data = await getJSON(
    `https://apply.workable.com/api/v1/widget/accounts/${token}?details=true`,
  );
  return (data.jobs ?? []).map((j: any) => ({
    url: j.url ?? j.application_url,
    apply_url: j.application_url ?? j.url,
    title: j.title,
    company: name,
    location: [j.city, j.state, j.country].filter(Boolean).join(", "),
    remote: !!j.telecommuting,
    description: clean(j.description),
    source: "workable",
    ats: "workable",
    posted_at: j.published_on,
  }));
}

// --------------------------------------------------------------------------
// global feeds
// --------------------------------------------------------------------------

async function remotive(q?: string): Promise<Job[]> {
  const url = new URL("https://remotive.com/api/remote-jobs");
  url.searchParams.set("limit", "200");
  if (q) url.searchParams.set("search", q);
  const data = await getJSON(url.toString());
  return (data.jobs ?? []).map((j: any) => ({
    url: j.url,
    apply_url: j.url,
    title: j.title,
    company: j.company_name,
    location: j.candidate_required_location || "Remote",
    remote: true,
    salary_text: j.salary || undefined,
    description: clean(j.description),
    source: "remotive",
    posted_at: j.publication_date,
  }));
}

async function remoteok(): Promise<Job[]> {
  const data = await getJSON("https://remoteok.com/api");
  return (data ?? [])
    .filter((j: any) => j && typeof j === "object" && j.position)
    .map((j: any) => ({
      url: j.url ?? j.apply_url,
      apply_url: j.apply_url ?? j.url,
      title: j.position,
      company: j.company,
      location: j.location || "Remote",
      remote: true,
      salary_text: j.salary_min
        ? `$${j.salary_min}-$${j.salary_max ?? j.salary_min}`
        : undefined,
      description: clean(j.description),
      source: "remoteok",
      posted_at: j.date,
    }));
}

async function arbeitnow(): Promise<Job[]> {
  const data = await getJSON("https://www.arbeitnow.com/api/job-board-api");
  return (data.data ?? []).map((j: any) => ({
    url: j.url,
    apply_url: j.url,
    title: j.title,
    company: j.company_name,
    location: j.location || (j.remote ? "Remote" : undefined),
    remote: !!j.remote,
    description: clean(j.description),
    source: "arbeitnow",
    posted_at: j.created_at ? new Date(j.created_at * 1000).toISOString() : undefined,
  }));
}

async function usajobs(q: string, loc?: string): Promise<Job[]> {
  const key = Deno.env.get("USAJOBS_API_KEY");
  const email = Deno.env.get("USAJOBS_EMAIL");
  if (!key || !email) return [];

  const url = new URL("https://data.usajobs.gov/api/search");
  url.searchParams.set("Keyword", q);
  url.searchParams.set("ResultsPerPage", "100");
  if (loc && !REMOTE_RE.test(loc)) url.searchParams.set("LocationName", loc);

  const data = await getJSON(url.toString(), {
    Host: "data.usajobs.gov",
    "User-Agent": email,
    "Authorization-Key": key,
  });

  return (data.SearchResult?.SearchResultItems ?? []).map((item: any) => {
    const d = item.MatchedObjectDescriptor ?? {};
    const pay = (d.PositionRemuneration ?? [])[0] ?? {};
    const salary = pay.MinimumRange
      ? `$${Math.round(+pay.MinimumRange).toLocaleString()}-$${Math.round(+(pay.MaximumRange ?? pay.MinimumRange)).toLocaleString()}`
      : undefined;
    return {
      url: d.PositionURI,
      apply_url: (d.ApplyURI ?? [])[0] ?? d.PositionURI,
      title: d.PositionTitle,
      company: d.OrganizationName,
      location: (d.PositionLocation ?? [])[0]?.LocationName,
      remote: false,
      salary_text: salary,
      description: clean(d.UserArea?.Details?.JobSummary ?? d.QualificationSummary),
      source: "usajobs",
      ats: "usajobs",
      posted_at: d.PublicationStartDate,
    };
  });
}

// --------------------------------------------------------------------------
// filtering — free, runs before anything costs a token
// --------------------------------------------------------------------------

function wordHit(haystack: string, needle: string): boolean {
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(?<!\\w)${escaped}(?!\\w)`, "i").test(haystack);
}

function keep(job: Job, cfg: any): { ok: boolean; why?: string } {
  const title = (job.title ?? "").toLowerCase();
  const blob = `${title}\n${(job.description ?? "").slice(0, 4000)}`.toLowerCase();

  if (!job.url || !job.title) return { ok: false, why: "missing url or title" };

  for (const bad of cfg.exclude_titles ?? []) {
    if (bad && wordHit(title, bad.toLowerCase())) {
      return { ok: false, why: `excluded title: ${bad}` };
    }
  }
  for (const bad of cfg.exclude_companies ?? []) {
    if (bad && (job.company ?? "").toLowerCase().includes(bad.toLowerCase())) {
      return { ok: false, why: `blocked company: ${job.company}` };
    }
  }
  for (const pat of cfg.no_go_patterns ?? []) {
    if (pat && blob.includes(pat.toLowerCase())) {
      return { ok: false, why: `no-go pattern: ${pat}` };
    }
  }

  // Title relevance: at least one target title word must appear. Without this
  // the global feeds flood the queue with thousands of unrelated postings.
  const titles: string[] = cfg.titles ?? [];
  if (titles.length) {
    const hit = titles.some((t: string) => {
      const words = t.toLowerCase().split(/\s+/).filter((w) => w.length > 2);
      if (!words.length) return false;
      const matched = words.filter((w) => title.includes(w)).length;
      return matched / words.length >= 0.5;   // half the words of any target title
    });
    if (!hit) return { ok: false, why: "title not relevant to targets" };
  }

  // Salary floor — jobs with no stated salary always pass, because most
  // postings omit it and filtering them would delete the majority of the market.
  const { min, max } = parseSalary(job.salary_text);
  const floor = cfg.salary_floor ?? 0;
  if (floor > 0 && min != null) {
    if ((max ?? min) < floor) return { ok: false, why: `below salary floor` };
  }

  // Location
  const locs: string[] = cfg.locations ?? [];
  if (locs.length && job.location) {
    const loc = job.location.toLowerCase();
    const remoteOk = cfg.remote_ok !== false;
    const isRemote = job.remote || REMOTE_RE.test(loc);

    if (!(isRemote && remoteOk)) {
      // Compare only against real places. A "Remote" entry in the config
      // describes remote jobs, not a city — matching it against an on-site
      // job let every location through, which silently defeated the filter.
      const cities = locs
        .filter((l) => !REMOTE_RE.test(l))
        .map((l) => l.toLowerCase().split(",")[0].trim())
        .filter(Boolean);

      if (cities.length === 0) {
        // Config is remote-only and this job is not remote.
        return { ok: false, why: `not remote: ${job.location}` };
      }
      if (!cities.some((city) => loc.includes(city))) {
        return { ok: false, why: `location: ${job.location}` };
      }
    }
  }

  return { ok: true };
}

// --------------------------------------------------------------------------
// handler
// --------------------------------------------------------------------------

Deno.serve(async (req) => {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  };
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: run } = await supabase
    .from("runs").insert({ kind: "discover" }).select().single();

  const stats = { found: 0, kept: 0, inserted: 0, filtered: 0, errors: 0 };
  const bySource: Record<string, number> = {};
  const problems: string[] = [];

  try {
    const { data: cfg } = await supabase
      .from("search_config").select("*").eq("id", 1).single();
    if (!cfg) throw new Error("search_config row missing");
    if (!cfg.titles?.length) {
      throw new Error(
        "No job titles configured. Add at least one in Settings → Search, " +
        "or every source returns thousands of irrelevant postings.",
      );
    }

    const enabled: string[] = cfg.sources ?? [];
    const all: Job[] = [];

    // --- per-company ATS boards (highest value) ---
    const { data: companies } = await supabase
      .from("companies").select("*").eq("enabled", true);

    const fetchers: Record<string, (t: string, n: string) => Promise<Job[]>> = {
      greenhouse, lever, ashby, workable,
    };

    // Bounded concurrency: polite to the APIs, still fast.
    const list = (companies ?? []).filter((c) => enabled.includes(c.ats));
    const BATCH = 6;
    for (let i = 0; i < list.length; i += BATCH) {
      const slice = list.slice(i, i + BATCH);
      const results = await Promise.allSettled(
        slice.map((c) => fetchers[c.ats]?.(c.token, c.name) ?? Promise.resolve([])),
      );
      results.forEach((r, idx) => {
        const c = slice[idx];
        if (r.status === "fulfilled") {
          all.push(...r.value);
          bySource[c.ats] = (bySource[c.ats] ?? 0) + r.value.length;
          supabase.from("companies")
            .update({ last_pulled: new Date().toISOString(), job_count: r.value.length })
            .eq("id", c.id).then(() => {});
        } else {
          stats.errors++;
          problems.push(`${c.ats}:${c.token} — ${r.reason?.message ?? r.reason}`);
        }
      });
    }

    // --- global feeds ---
    const globals: Array<[string, () => Promise<Job[]>]> = [];
    if (enabled.includes("remotive")) {
      for (const t of (cfg.titles ?? []).slice(0, 3)) {
        globals.push([`remotive`, () => remotive(t)]);
      }
    }
    if (enabled.includes("remoteok")) globals.push(["remoteok", remoteok]);
    if (enabled.includes("arbeitnow")) globals.push(["arbeitnow", arbeitnow]);
    if (enabled.includes("usajobs")) {
      for (const t of (cfg.titles ?? []).slice(0, 3)) {
        globals.push(["usajobs", () => usajobs(t, (cfg.locations ?? [])[0])]);
      }
    }

    const gres = await Promise.allSettled(globals.map(([, fn]) => fn()));
    gres.forEach((r, i) => {
      const name = globals[i][0];
      if (r.status === "fulfilled") {
        all.push(...r.value);
        bySource[name] = (bySource[name] ?? 0) + r.value.length;
      } else {
        stats.errors++;
        problems.push(`${name} — ${r.reason?.message ?? r.reason}`);
      }
    });

    stats.found = all.length;

    // --- filter + dedupe ---
    const seen = new Set<string>();
    const rows: any[] = [];
    for (const j of all) {
      if (!j.url || seen.has(j.url)) continue;
      seen.add(j.url);
      const verdict = keep(j, cfg);
      if (!verdict.ok) { stats.filtered++; continue; }
      const { min, max } = parseSalary(j.salary_text);
      rows.push({
        url: j.url,
        apply_url: j.apply_url ?? j.url,
        title: j.title,
        company: j.company,
        location: j.location,
        remote: !!j.remote,
        salary_text: j.salary_text,
        salary_min: min, salary_max: max,
        description: (j.description ?? "").slice(0, 20000),
        source: j.source,
        ats: j.ats,
        posted_at: j.posted_at,
        status: "new",
      });
    }
    stats.kept = rows.length;

    // --- insert, ignoring anything already seen ---
    for (let i = 0; i < rows.length; i += 500) {
      const chunk = rows.slice(i, i + 500);
      const { data, error } = await supabase
        .from("jobs")
        .upsert(chunk, { onConflict: "url", ignoreDuplicates: true })
        .select("id");
      if (error) { stats.errors++; problems.push(`insert: ${error.message}`); }
      else stats.inserted += data?.length ?? 0;
    }

    const message = stats.inserted > 0
      ? `${stats.inserted} new jobs from ${Object.keys(bySource).length} sources`
      : stats.found === 0
        ? "No sources returned any jobs. Check Settings → Companies has entries, " +
          "and that your enabled sources are correct."
        : `Found ${stats.found} postings but kept 0 — every one was filtered out. ` +
          `Most likely your job titles are too specific, or your exclude list is too broad.`;

    await supabase.from("runs").update({
      status: "done", found: stats.found, processed: stats.inserted,
      errors: stats.errors,
      detail: { by_source: bySource, filtered: stats.filtered, problems: problems.slice(0, 20) },
      message, ended_at: new Date().toISOString(),
    }).eq("id", run!.id);

    return new Response(JSON.stringify({ ok: true, ...stats, by_source: bySource, message }), {
      headers: { ...cors, "Content-Type": "application/json" },
    });
  } catch (e) {
    await supabase.from("runs").update({
      status: "error", message: String(e?.message ?? e),
      errors: stats.errors + 1, ended_at: new Date().toISOString(),
    }).eq("id", run!.id);
    return new Response(JSON.stringify({ ok: false, error: String(e?.message ?? e) }), {
      status: 500, headers: { ...cors, "Content-Type": "application/json" },
    });
  }
});
