// popup.js — connection setup and the start/stop control.

const $ = (id) => document.getElementById(id);

function say(text, kind = "ok") {
  const el = $("msg");
  el.textContent = text;
  el.className = kind;
}

async function refresh() {
  const { url, key, dryRun } = await chrome.storage.local.get(["url", "key", "dryRun"]);
  $("url").value = url ?? "";
  $("key").value = key ?? "";
  $("dry").checked = !!dryRun;

  if (!url || !key) {
    $("setup").open = true;
    say("Paste your Supabase URL and anon key to connect.", "warn");
    $("start").disabled = true;
    return;
  }

  chrome.runtime.sendMessage({ type: "STATUS" }, (res) => {
    if (chrome.runtime.lastError) return;
    if (!res?.ok) {
      say(res?.error ?? "Could not reach the database.", "err");
      return;
    }
    const s = res.stats ?? {};
    $("stats").style.display = "grid";
    $("s_queued").textContent = s.queued ?? 0;
    $("s_today").textContent = s.applied_today ?? 0;
    $("s_help").textContent = s.needs_help ?? 0;

    $("dot").className = res.running ? "dot live" : "dot";
    $("state").textContent = res.running ? "applying" : "idle";
    $("start").style.display = res.running ? "none" : "block";
    $("stop").style.display = res.running ? "block" : "none";
    $("start").disabled = false;

    if (!res.running && (s.queued ?? 0) === 0) {
      say("Queue is empty. Run discovery in the panel first.", "warn");
    }
  });
}

$("save").onclick = async () => {
  const url = $("url").value.trim().replace(/\/+$/, "");
  const key = $("key").value.trim();
  if (!url.startsWith("https://") || !key) {
    say("Need a full https:// URL and the anon key.", "err");
    return;
  }
  await chrome.storage.local.set({ url, key });
  say("Testing…", "warn");
  chrome.runtime.sendMessage({ type: "TEST" }, (res) => {
    if (!res?.ok) {
      say(`Connection failed: ${res?.error ?? "unknown"}`, "err");
      return;
    }
    if (!res.name || !res.email) {
      say("Connected, but your profile has no name/email yet. Fill it in the panel.", "warn");
      return;
    }
    if (!res.hasResume) {
      say(`Connected as ${res.name}. No resume uploaded yet — add one in the panel.`, "warn");
      return;
    }
    say(`Connected as ${res.name}. ${res.queued} job(s) queued.`, "ok");
    $("setup").open = false;
    refresh();
  });
};

$("dry").onchange = async (e) => {
  await chrome.storage.local.set({ dryRun: e.target.checked });
  say(e.target.checked
    ? "Dry run on: forms get filled but never submitted."
    : "Dry run off: applications will be submitted.", "warn");
};

$("start").onclick = () => {
  chrome.runtime.sendMessage({ type: "START" }, () => {
    say("Running. You can close this popup — it keeps going.", "ok");
    setTimeout(refresh, 600);
  });
};

$("stop").onclick = () => {
  chrome.runtime.sendMessage({ type: "STOP" }, () => {
    say("Stopped.", "warn");
    setTimeout(refresh, 400);
  });
};

refresh();
setInterval(refresh, 5000);
