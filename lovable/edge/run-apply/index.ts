// ============================================================================
// run-apply — fires the cloud agent from the panel.
//
// The applier runs on GitHub Actions. This function is the trigger: it sends
// a repository_dispatch event, which starts the workflow.
//
// It exists as an Edge Function rather than a fetch from the browser because
// it needs a GitHub token, and a token in the frontend bundle is a token
// anyone who opens devtools can use to push code to your repository.
// ============================================================================

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

Deno.serve(async (req) => {
  const cors = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  };
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status, headers: { ...cors, "Content-Type": "application/json" },
    });

  const token = Deno.env.get("GITHUB_TOKEN");
  const repo = Deno.env.get("GITHUB_REPO");          // e.g. "succincc/applypilot-agent"

  if (!token || !repo) {
    return json({
      ok: false,
      error: "Cloud agent not configured. Set GITHUB_TOKEN and GITHUB_REPO as " +
             "Edge Function secrets — see 04-CLOUD-AGENT.txt.",
    }, 400);
  }

  let maxJobs = 25;
  let dryRun = false;
  try {
    const body = await req.json();
    if (Number.isInteger(body?.max_jobs)) maxJobs = Math.min(100, Math.max(1, body.max_jobs));
    dryRun = body?.dry_run === true;
  } catch { /* defaults are fine */ }

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  // Don't start a run with nothing to do — a workflow that finds an empty
  // queue looks identical to a workflow that is broken.
  const { count } = await supabase
    .from("jobs").select("id", { count: "exact", head: true }).eq("status", "queued");

  if (!count) {
    return json({
      ok: false,
      error: "Queue is empty. Run discovery, then scoring — scoring is what " +
             "moves good jobs into the queue.",
    }, 400);
  }

  const res = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": "ApplyPilot",
    },
    body: JSON.stringify({
      event_type: "apply",
      client_payload: { max_jobs: maxJobs, dry_run: dryRun },
    }),
  });

  // GitHub answers 204 with no body on success.
  if (res.status === 204) {
    await supabase.from("runs").insert({
      kind: "apply", status: "running",
      message: `cloud agent dispatched — up to ${maxJobs} job(s)${dryRun ? " (dry run)" : ""}`,
    });
    return json({
      ok: true,
      message: `Cloud agent started on ${count} queued job(s). ` +
               `It takes a minute or two to boot; progress appears here as it works.`,
    });
  }

  const detail = (await res.text()).slice(0, 300);
  const hint =
    res.status === 404
      ? "Repository not found, or the token cannot see it. Check GITHUB_REPO is " +
        "owner/name and the token has Actions: Read and write on that repo."
      : res.status === 401 || res.status === 403
        ? "GitHub rejected the token. It may be expired or missing the Actions permission."
        : "";

  return json({ ok: false, error: `GitHub ${res.status}. ${hint} ${detail}`.trim() }, 502);
});
