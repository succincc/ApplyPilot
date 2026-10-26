// ============================================================================
// score — rates unscored jobs against the resume, then tailors the winners.
//
// Batched 20 jobs per model call. Scoring one job per call is what makes
// these systems expensive: discovery can surface thousands of postings a day,
// and at one request each the free tier is gone before anything gets applied
// to. Sending the resume once and scoring twenty postings against it costs a
// twentieth as much and leaves the quota for tailoring, which is the step
// that actually produces an application.
//
// Model: Gemini Flash free tier. No credit card, no billing, ~1,500 requests
// per day. Exceeding it returns HTTP 429 — it never charges.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const MODEL = Deno.env.get("GEMINI_MODEL") ?? "gemini-2.0-flash";
const BATCH = 20;
const MAX_BATCHES = 12;        // ceiling per invocation; cron calls it again

async function gemini(prompt: string, maxTokens = 8192): Promise<string> {
  const key = Deno.env.get("GEMINI_API_KEY");
  if (!key) throw new Error("GEMINI_API_KEY is not set in Edge Function secrets");

  const res = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${key}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.2, maxOutputTokens: maxTokens },
      }),
    },
  );

  if (res.status === 429) {
    throw new Error(
      "RATE_LIMIT: Gemini free-tier quota reached for today. Scoring resumes " +
      "automatically tomorrow — nothing was charged.",
    );
  }
  if (!res.ok) throw new Error(`Gemini HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);

  const data = await res.json();
  const text = data?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) throw new Error("Gemini returned no text");
  return text;
}

/** Pull a JSON array out of a model response that may be fenced or chatty. */
function extractArray(text: string): any[] | null {
  const fenced = text.match(/```(?:json)?\s*(\[[\s\S]*?\])\s*```/);
  const raw = fenced?.[1] ?? text.match(/\[[\s\S]*\]/)?.[0];
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function extractObject(text: string): any | null {
  const fenced = text.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  const raw = fenced?.[1] ?? text.match(/\{[\s\S]*\}/)?.[0];
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

const SCORE_PROMPT = `You rate how well one candidate fits each job posting.

Scoring:
 9-10  Direct experience in nearly everything required.
 7-8   Most requirements met, small gaps that are easily bridged.
 5-6   Real overlap but missing some key requirements.
 3-4   Significant gaps, would need substantial ramp-up.
 1-2   Wrong field or wrong level entirely.

Be realistic about seniority and years of experience. Credit transferable
experience. Do not inflate scores to be encouraging — a wrong score wastes
an application.

Return ONLY a JSON array, one object per job, in the SAME ORDER given, with
every job present exactly once. Keys: "n" (the job number), "score" (1-10
integer), "reason" (one sentence). No text outside the array.`;

const TAILOR_PROMPT = `Rewrite this candidate's resume for this specific job.

ABSOLUTE RULES:
- Never invent employers, job titles, dates, degrees, certifications or metrics.
- Keep every real employer and date exactly as given.
- You may reorder, re-emphasise, and rewrite bullet wording to mirror the
  job's language. You may drop irrelevant detail. You may not add anything
  the candidate did not do.
- Mirror the posting's own vocabulary where it honestly matches their
  experience — that is what gets past keyword screening.

Return ONLY JSON with these keys:
  "summary": 2-3 sentence professional summary aimed at this job
  "skills": object of category -> comma-separated string
  "experience": array of { "company", "title", "dates", "bullets": [string] }
  "education": array of { "school", "degree", "dates" }
No text outside the JSON.`;

function renderResume(data: any, profile: any): string {
  const out: string[] = [];
  out.push(profile.full_name ?? "");
  if (profile.target_role) out.push(profile.target_role);
  const contact = [profile.email, profile.phone,
    [profile.city, profile.state].filter(Boolean).join(", "),
    profile.linkedin_url].filter(Boolean).join(" | ");
  if (contact) out.push(contact);
  out.push("");

  if (data.summary) { out.push("SUMMARY", String(data.summary), ""); }

  if (data.skills && typeof data.skills === "object") {
    out.push("SKILLS");
    for (const [k, v] of Object.entries(data.skills)) out.push(`${k}: ${v}`);
    out.push("");
  }

  if (Array.isArray(data.experience) && data.experience.length) {
    out.push("EXPERIENCE");
    for (const e of data.experience) {
      const head = [e.company, e.title].filter(Boolean).join(" - ");
      out.push(e.dates ? `${head} (${e.dates})` : head);
      for (const b of e.bullets ?? []) out.push(`- ${String(b).replace(/^[-•*]\s*/, "")}`);
      out.push("");
    }
  }

  if (Array.isArray(data.education) && data.education.length) {
    out.push("EDUCATION");
    for (const e of data.education) {
      const line = [e.school, e.degree].filter(Boolean).join(" - ");
      out.push(e.dates ? `${line} (${e.dates})` : line);
    }
  }
  return out.join("\n").trim();
}

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
  const { data: run } = await supabase.from("runs").insert({ kind: "score" }).select().single();

  const stats = { scored: 0, tailored: 0, errors: 0 };
  let note = "";

  try {
    const { data: profile } = await supabase.from("profile").select("*").eq("id", 1).single();
    if (!profile?.resume_text?.trim()) {
      throw new Error("No resume yet. Upload one in Settings → Profile before scoring.");
    }
    const resume = profile.resume_text.slice(0, 8000);

    // ---------------- score ----------------
    for (let b = 0; b < MAX_BATCHES; b++) {
      const { data: batch } = await supabase
        .from("jobs").select("id,title,company,location,salary_text,description")
        .is("score", null).eq("status", "new")
        .order("found_at", { ascending: false })
        .limit(BATCH);
      if (!batch?.length) break;

      const listing = batch.map((j, i) =>
        `--- JOB ${i + 1} ---\nTITLE: ${j.title}\nCOMPANY: ${j.company ?? "?"}\n` +
        `LOCATION: ${j.location ?? "?"}\nSALARY: ${j.salary_text ?? "not listed"}\n` +
        `DESCRIPTION:\n${(j.description ?? "").slice(0, 1800)}`
      ).join("\n\n");

      let parsed: any[] | null = null;
      try {
        const text = await gemini(
          `${SCORE_PROMPT}\n\nCANDIDATE RESUME:\n${resume}\n\n` +
          `=== ${batch.length} JOBS ===\n\n${listing}`,
          Math.min(8192, batch.length * 160 + 512),
        );
        parsed = extractArray(text);
      } catch (e) {
        if (String(e.message).startsWith("RATE_LIMIT")) { note = e.message; break; }
        throw e;
      }

      // A batch is only trusted when every job came back. A partial response
      // would attach the wrong score to the wrong job and silently mis-rank
      // the whole queue, which is worse than not scoring at all.
      if (!parsed || parsed.length !== batch.length) {
        stats.errors++;
        note = "A scoring batch came back incomplete and was skipped.";
        continue;
      }

      const byIndex = new Map<number, any>();
      for (const item of parsed) {
        const n = Number(item?.n);
        if (Number.isInteger(n) && n >= 1 && n <= batch.length) byIndex.set(n, item);
      }
      if (byIndex.size !== batch.length) { stats.errors++; continue; }

      for (let i = 0; i < batch.length; i++) {
        const r = byIndex.get(i + 1);
        const score = Math.max(1, Math.min(10, Math.round(Number(r.score) || 1)));
        // The DB trigger routes this to queued / review / skipped.
        await supabase.from("jobs").update({
          score, score_reason: String(r.reason ?? "").slice(0, 500),
          scored_at: new Date().toISOString(),
        }).eq("id", batch[i].id);
        stats.scored++;
      }
    }

    // ---------------- tailor the queued ones ----------------
    if (!note.startsWith("RATE_LIMIT")) {
      const { data: queued } = await supabase
        .from("jobs").select("id,title,company,description")
        .eq("status", "queued").is("tailored_resume", null)
        .order("score", { ascending: false }).limit(10);

      for (const job of queued ?? []) {
        try {
          const text = await gemini(
            `${TAILOR_PROMPT}\n\nJOB: ${job.title} at ${job.company ?? ""}\n` +
            `POSTING:\n${(job.description ?? "").slice(0, 4000)}\n\n` +
            `CANDIDATE RESUME:\n${resume}`,
            3072,
          );
          const data = extractObject(text);
          if (!data?.experience) { stats.errors++; continue; }
          await supabase.from("jobs").update({
            tailored_resume: renderResume(data, profile),
            tailored_at: new Date().toISOString(),
          }).eq("id", job.id);
          stats.tailored++;
        } catch (e) {
          if (String(e.message).startsWith("RATE_LIMIT")) { note = e.message; break; }
          stats.errors++;
        }
      }
    }

    const message = note ||
      (stats.scored === 0 && stats.tailored === 0
        ? "Nothing to score — no new jobs waiting. Run discover first."
        : `Scored ${stats.scored}, tailored ${stats.tailored}`);

    await supabase.from("runs").update({
      status: "done", processed: stats.scored, errors: stats.errors,
      detail: stats, message, ended_at: new Date().toISOString(),
    }).eq("id", run!.id);

    return new Response(JSON.stringify({ ok: true, ...stats, message }), {
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
