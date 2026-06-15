/**
 * Main dashboard logic: rendering, polling, actions, SSE log viewer, filter.
 * Uses key-based DOM patching instead of full innerHTML replacement to
 * preserve toggle state, scroll position, and button states.
 */

// ── Constants ───────────────────────────────────────────

const ICONS = { done: "\u2713", running: "\u21bb", pending: "\u00b7", failed: "\u2717", skipped: "\u2014" };
const BUILD_STEPS = ["engineer", "review", "pr", "ci-watch"];
const STEP_LABELS = { engineer: "ENG", review: "REV", pr: "PR", "ci-watch": "CI" };
const INTERACTIVE_PHASES = new Set(["pm", "debate", "designer", "architect"]);
const PHASE_NOTIFY_LABELS = {
  pm: "Product Manager is waiting for your input",
  debate: "PRD Reviewer is waiting for discussion",
  designer: "Designer is waiting for collaboration",
  architect: "Architect is waiting for guidance",
};

// ── Agent names (joke mode) ─────────────────────────────

let agentNamesOn = false;

const AGENT_PHASE_NAMES = {
  pm: "John",
  debate: "Sandra",
  designer: "Hasan",
  architect: "Ale",
  "cross-review": "Caroline",
  "cross-review-fix": "Kevin",
};

const AGENT_BUILDER_NAMES = {
  "any-llm": "Nathan",
  "otari-ai": "Dimitris",
  otari: "Toto",
  "otari-sdk-ts": "Mario",
  "otari-sdk-rust": "Raz",
  "otari-sdk-go": "Peter",
  "otari-sdk-python": "Nathan",
};

function toggleAgentNames() {
  agentNamesOn = !agentNamesOn;
  const btn = document.getElementById("agent-names-btn");
  if (btn) btn.classList.toggle("active", agentNamesOn);
  if (prevData) render(prevData);
}

function agentLabel(phase, originalLabel) {
  if (!agentNamesOn) return originalLabel;
  const name = AGENT_PHASE_NAMES[phase];
  return name ? name : originalLabel;
}

function agentRepoName(repo) {
  if (!agentNamesOn) return repo;
  return AGENT_BUILDER_NAMES[repo] || repo;
}

// ── State ───────────────────────────────────────────────

let prevData = null;
const expandedLogs = new Set();  // tracks "slug-repo" keys for expanded log tails
const notified = new Set();
let consecutiveFailures = 0;
let activeLogModal = null;  // { slug, repo, eventSource }
let openMenuSlug = null;    // slug of currently open resume dropdown

// ── Helpers ─────────────────────────────────────────────

function fmtSize(b) {
  if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
  if (b >= 1024) return (b / 1024).toFixed(1) + " KB";
  return b + " B";
}

function fmtElapsed(isoStart) {
  if (!isoStart) return "";
  const secs = Math.floor((Date.now() - new Date(isoStart).getTime()) / 1000);
  if (secs < 0) return "0s";
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h" + Math.floor((secs % 3600) / 60) + "m";
}

function fmtDuration(startIso, endIso) {
  if (!startIso || !endIso) return "";
  const secs = Math.floor((new Date(endIso) - new Date(startIso)) / 1000);
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  return Math.floor(secs / 3600) + "h" + Math.floor((secs % 3600) / 60) + "m";
}

function stepIndex(stepName) {
  if (!stepName) return -1;
  if (stepName === "done" || stepName.endsWith("-done")) return BUILD_STEPS.length;
  if (stepName.startsWith("fix-pr")) return BUILD_STEPS.length - 1;
  if (stepName.startsWith("investigate") || stepName.startsWith("build-check") || stepName.startsWith("build-fix")) return 0;
  for (let i = 0; i < BUILD_STEPS.length; i++) {
    if (stepName.startsWith(BUILD_STEPS[i]) || stepName.startsWith("ci-fix")) {
      return stepName.startsWith("ci-fix") ? 3 : i;
    }
  }
  return -1;
}

function simpleHash(obj) {
  return JSON.stringify(obj);
}

// ── Notifications ───────────────────────────────────────

if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission();
}

function checkNotifications(features) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  for (const f of features) {
    const phase = f.current_phase;
    if (!phase || !INTERACTIVE_PHASES.has(phase)) continue;
    const ps = (f.phases && f.phases[phase]) || {};
    if (ps.status !== "running") continue;
    const key = f.slug + ":" + phase;
    if (notified.has(key)) continue;
    notified.add(key);
    const label = PHASE_NOTIFY_LABELS[phase] || (phase + " is running");
    new Notification("any-llm-world", {
      body: f.slug + ": " + label,
      icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>\ud83d\udd14</text></svg>",
      tag: key,
    });
  }
}

function updateNotifStatus() {
  const el = document.getElementById("notif-status");
  if (!("Notification" in window)) { el.innerHTML = ""; return; }
  if (Notification.permission === "granted") {
    el.innerHTML = '<span class="notif-banner notif-ok">Notifications on</span>';
  } else if (Notification.permission === "default") {
    el.innerHTML = '<span class="notif-banner" onclick="Notification.requestPermission().then(updateNotifStatus)">Enable notifications</span>';
  } else {
    el.innerHTML = '<span class="notif-banner" style="opacity:0.5;cursor:default">Notifications blocked</span>';
  }
}

// ── Per-repo log toggle ─────────────────────────────────

function toggleRepoLog(slug, repo) {
  const key = slug + "-" + repo;
  const isExpanded = expandedLogs.has(key);
  if (isExpanded) {
    expandedLogs.delete(key);
  } else {
    expandedLogs.add(key);
  }

  // Update the log tail row visibility.
  const logRow = document.getElementById("log-row-" + slug + "-" + repo);
  if (logRow) logRow.style.display = isExpanded ? "none" : "";

  // Update the chevron text.
  const chevron = document.getElementById("chevron-" + slug + "-" + repo);
  if (chevron) chevron.textContent = isExpanded ? "\u25b8" : "\u25be";
}

// ── Filter ──────────────────────────────────────────────

function applyFilter() {
  const input = document.getElementById("filter-input");
  const query = (input ? input.value : "").toLowerCase().trim();
  const url = new URL(window.location);
  if (query) { url.searchParams.set("filter", query); } else { url.searchParams.delete("filter"); }
  history.replaceState(null, "", url);
  document.querySelectorAll(".card").forEach(card => {
    const slug = card.dataset.slug || "";
    card.classList.toggle("hidden", query !== "" && !slug.includes(query));
  });
}

function initFilter() {
  const params = new URLSearchParams(window.location.search);
  const initial = params.get("filter") || "";
  const input = document.getElementById("filter-input");
  if (input && initial) input.value = initial;
}

// ── API actions ─────────────────────────────────────────

async function fixPRs(slug, repo) {
  const body = { slug };
  if (repo) body.repo = repo;
  const btnId = repo ? "fix-pr-btn-" + slug + "-" + repo : "fix-pr-btn-" + slug;
  const btn = document.getElementById(btnId);
  if (btn) { btn.disabled = true; btn.textContent = "Starting..."; }
  try {
    const res = await fetch("/api/fix-prs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("Fix PRs failed: " + (data.error || "Unknown error"));
      if (btn) { btn.disabled = false; btn.textContent = "Fix PRs"; }
    }
  } catch (e) {
    alert("Fix PRs request failed: " + e);
    if (btn) { btn.disabled = false; btn.textContent = "Fix PRs"; }
  }
}

async function stopFixPRs(slug) {
  try {
    const res = await fetch("/api/stop-fix-prs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("Stop failed: " + (data.error || "Unknown error"));
    }
  } catch (e) { alert("Stop request failed: " + e); }
}

async function ciCheck(slug, repo) {
  const btn = document.getElementById("ci-btn-" + slug + "-" + repo);
  // Optimistic disable — the next status poll will keep the disabled running
  // state alive for as long as the ci-check tmux session exists.
  if (btn) {
    btn.disabled = true;
    btn.classList.add("btn-running");
    btn.textContent = "\u21bb Checking CI\u2026";
  }
  try {
    const res = await fetch("/api/ci-check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, repo }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("CI check failed: " + (data.error || "Unknown error"));
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("btn-running");
        btn.textContent = "\u21bb CI";
      }
    }
  } catch (e) {
    alert("CI check request failed: " + e);
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("btn-running");
      btn.textContent = "\u21bb CI";
    }
  }
}

async function rebasePR(slug, repo) {
  const btn = document.getElementById("rebase-btn-" + slug + "-" + repo);
  // Optimistic disable — the next status poll will render the running state
  // authoritatively from the live tmux session list.
  if (btn) {
    btn.disabled = true;
    btn.classList.add("btn-running");
    btn.textContent = "\u21bb Rebasing\u2026";
  }
  try {
    const res = await fetch("/api/rebase", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, repo }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("Rebase failed: " + (data.error || "Unknown error"));
      if (btn) {
        btn.disabled = false;
        btn.classList.remove("btn-running");
        btn.textContent = "\u21bb Rebase";
      }
    }
  } catch (e) {
    alert("Rebase request failed: " + e);
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("btn-running");
      btn.textContent = "\u21bb Rebase";
    }
  }
}

async function resumePipeline(slug, phase) {
  openMenuSlug = null;
  try {
    const res = await fetch("/api/resume", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug, phase }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("Resume failed: " + (data.error || "Unknown error"));
    }
  } catch (e) { alert("Resume request failed: " + e); }
}

async function cancelFeature(slug) {
  if (!confirm("Cancel all running pipelines for " + slug + "?")) return;
  try {
    const res = await fetch("/api/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }),
    });
    if (!res.ok) {
      const data = await res.json();
      alert("Cancel failed: " + (data.error || "Unknown error"));
    }
  } catch (e) { alert("Cancel request failed: " + e); }
}

function toggleResumeMenu(slug) {
  openMenuSlug = openMenuSlug === slug ? null : slug;
  document.querySelectorAll(".resume-menu").forEach(el => {
    el.classList.toggle("open", el.dataset.slug === openMenuSlug);
  });
}

// Close resume menu on click outside
document.addEventListener("click", function(e) {
  if (!e.target.closest(".resume-dropdown")) {
    openMenuSlug = null;
    document.querySelectorAll(".resume-menu").forEach(el => el.classList.remove("open"));
  }
});

// ── Log viewer modal ────────────────────────────────────

function openLogModal(slug, repo) {
  closeLogModal();
  const overlay = document.getElementById("log-modal-overlay");
  overlay.classList.add("open");
  document.getElementById("log-modal-title").textContent = repo + " logs";
  const body = document.getElementById("log-modal-body");
  body.textContent = "Connecting...";
  const footer = document.getElementById("log-modal-footer-status");
  footer.innerHTML = '<span class="log-live-indicator">Connecting</span>';

  const es = new EventSource("/api/logs/" + encodeURIComponent(slug) + "/" + encodeURIComponent(repo) + "/stream");
  activeLogModal = { slug, repo, eventSource: es };

  es.addEventListener("initial", function(e) {
    body.textContent = e.data;
    body.scrollTop = body.scrollHeight;
    footer.innerHTML = '<span class="log-live-indicator">Live</span>';
  });
  es.addEventListener("append", function(e) {
    body.textContent += e.data + "\n";
    body.scrollTop = body.scrollHeight;
  });
  es.addEventListener("idle", function() {
    footer.innerHTML = '<span style="color:var(--yellow)">Idle (log not actively written)</span>';
  });
  es.onerror = function() {
    footer.innerHTML = '<span style="color:var(--muted)">Disconnected</span>';
    es.close();
  };
}

function closeLogModal() {
  if (activeLogModal && activeLogModal.eventSource) {
    activeLogModal.eventSource.close();
  }
  activeLogModal = null;
  const overlay = document.getElementById("log-modal-overlay");
  if (overlay) overlay.classList.remove("open");
}

function refreshLogModal() {
  if (!activeLogModal) return;
  openLogModal(activeLogModal.slug, activeLogModal.repo);
}

// ── Rendering ───────────────────────────────────────────

function renderHistory(history, repoId) {
  if (!history || !history.length) return "";
  const hasFixLoop = history.some(h => h.step && h.step.includes("fix"));
  const items = history.map(h => {
    const dur = fmtDuration(h.started_at, h.finished_at);
    const name = (h.step || "?").toUpperCase().replace(/-/g, " ").replace("ENGINEER", "ENG").replace("REVIEW", "REV");
    return '<span class="h-step">' + name + (dur ? ' <span class="h-dur">' + dur + "</span>" : "") + "</span>";
  }).join('<span class="h-arrow">\u2192</span>');

  const toggleId = "hist-" + repoId.replace(/[^a-zA-Z0-9]/g, "-");
  const label = history.length + " step" + (history.length !== 1 ? "s" : "") + (hasFixLoop ? " (fix loop)" : "");
  return '<span class="hist-toggle" onclick="event.stopPropagation();var el=document.getElementById(\'' + toggleId + '\');el.style.display=el.style.display===\'none\'?\'\':\'none\'">\u25b8 ' + label + "</span>" +
    '<div id="' + toggleId + '" class="step-history" style="display:none">' + items + "</div>";
}

function buildPhaseBar(f, all_phases, phases_by_type, phase_labels) {
  const applicable = f.applicable_phases || phases_by_type[f.triage_type] || all_phases;
  const repoProgress = f.repo_progress || {};
  const doneCount = Object.values(repoProgress).filter(p => p.step === "done").length;
  const totalRepos = (f.repos || []).length;

  return all_phases.map(p => {
    if (!applicable.includes(p)) return "";
    const ps = (f.phases && f.phases[p]) || {};
    const st = ps.status || "pending";
    let label = agentLabel(p, phase_labels[p] || p);
    if (p === "build" && totalRepos > 0) {
      label += ' <span class="build-count">' + doneCount + "/" + totalRepos + "</span>";
    }
    return '<div class="phase phase-' + st + '">' + ICONS[st] + " " + label + "</div>";
  }).join("");
}

function buildRepoRows(f) {
  const prInfo = f.pr_info || {};
  const logTails = f.log_tails || {};
  const repoProgress = f.repo_progress || {};
  if (!f.repos || !f.repos.length) return "";

  return f.repos.map(r => {
    const pr = prInfo[r] || {};
    const prLink = pr.url ? '<a href="' + pr.url + '" target="_blank">#' + pr.url.split("/").pop() + "</a>" : "";
    const ciSt = pr.ci || "none";
    const log = logTails[r] || {};
    const logLines = (log.last_lines || []).map(escapeHtml).join("\n");
    const logSize = log.size_bytes ? '<span class="log-size">' + fmtSize(log.size_bytes) + "</span>" : "";
    const logOnclick = 'openLogModal(\'' + f.slug + '\',\'' + r + '\')';
    const logHtml = logLines ? '<div class="log-tail" onclick="' + logOnclick + '" title="Click to open full log">' + logLines + "</div>" : "";

    const logKey = f.slug + "-" + r;
    const isExpanded = expandedLogs.has(logKey);
    const chevronHtml = logLines
      ? '<span class="log-chevron" id="chevron-' + f.slug + '-' + r + '" onclick="event.stopPropagation();toggleRepoLog(\'' + f.slug + '\',\'' + r + '\')">' + (isExpanded ? "\u25be" : "\u25b8") + "</span>"
      : "";

    const progress = repoProgress[r] || {};
    const currentStep = progress.step || "";
    const history = progress.history || [];
    const liveCI = (prInfo[r] || {}).ci;
    const isMerged = (prInfo[r] || {}).merged;
    const effectiveStep = isMerged ? "done" : (liveCI === "pass" && currentStep.startsWith("ci")) ? "done" : currentStep;
    const effectiveIdx = stepIndex(effectiveStep);

    const stepsHtml = BUILD_STEPS.map((s, i) => {
      let cls = "step-pending", icon = "";
      if (effectiveStep === "done" || effectiveStep.endsWith("-done") || i < effectiveIdx) {
        cls = "step-done"; icon = "\u2713 ";
      } else if (i === effectiveIdx) {
        cls = "step-running"; icon = "\u21bb ";
      }
      return '<span class="step ' + cls + '">' + icon + STEP_LABELS[s] + "</span>";
    }).join("");

    const elapsed = (effectiveStep && effectiveStep !== "done" && !effectiveStep.endsWith("-done"))
      ? '<span class="repo-elapsed">' + fmtElapsed(progress.started_at) + "</span>" : "";
    const histHtml = renderHistory(history, f.slug + "-" + r);

    // Per-repo action buttons
    const activeSessions = f.tmux_sessions || [];
    const rebaseRunning = activeSessions.includes("rebase-" + f.slug + "-" + r);
    const ciCheckRunning = activeSessions.includes("ci-check-" + f.slug + "-" + r);
    let repoActions = "";
    if (pr.url) {
      repoActions += '<button class="action-btn btn-small" id="fix-pr-btn-' + f.slug + '-' + r + '" onclick="fixPRs(\'' + f.slug + '\',\'' + r + '\')">Fix</button>';
    }
    if (pr.url && (ciSt === "fail" || ciSt === "pending" || ciCheckRunning)) {
      const ciCls = "action-btn btn-small" + (ciCheckRunning ? " btn-running" : "");
      const ciLabel = ciCheckRunning ? "\u21bb Checking CI\u2026" : "\u21bb CI";
      const ciDisabled = ciCheckRunning ? " disabled" : "";
      repoActions += '<button class="' + ciCls + '" id="ci-btn-' + f.slug + '-' + r + '"' + ciDisabled + ' onclick="ciCheck(\'' + f.slug + '\',\'' + r + '\')">' + ciLabel + '</button>';
    }
    if (pr.url) {
      const rbCls = "action-btn btn-small" + (rebaseRunning ? " btn-running" : "");
      const rbLabel = rebaseRunning ? "\u21bb Rebasing\u2026" : "\u21bb Rebase";
      const rbDisabled = rebaseRunning ? " disabled" : "";
      repoActions += '<button class="' + rbCls + '" id="rebase-btn-' + f.slug + '-' + r + '"' + rbDisabled + ' onclick="rebasePR(\'' + f.slug + '\',\'' + r + '\')">' + rbLabel + '</button>';
    }

    return "<tr id=\"row-" + f.slug + "-" + r + "\">" +
      "<td>" + chevronHtml + "<strong>" + agentRepoName(r) + "</strong> " + (agentNamesOn ? '<span class="agent-repo-hint">' + r + "</span> " : "") + logSize + "</td>" +
      '<td><div class="repo-steps">' + stepsHtml + elapsed + histHtml + "</div></td>" +
      "<td>" + prLink + repoActions + "</td>" +
      "<td>" + (pr.url ? (pr.merged ? '<span class="status-dot dot-merged"></span>merged' : (pr.needs_rebase ? '<span class="status-dot dot-rebase"></span>needs rebase · ' : '') + '<span class="status-dot dot-' + ciSt + '"></span>' + ciSt) : "") + "</td>" +
      "</tr>" +
      (logHtml ? '<tr id="log-row-' + f.slug + '-' + r + '" style="display:' + (isExpanded ? "" : "none") + '"><td colspan="4">' + logHtml + "</td></tr>" : "");
  }).join("");
}

// -- Cost UI commented out for now --
// function buildCostHtml(costs) {
//   if (!costs || !costs.total_cost || costs.total_cost <= 0) return "";
//   const repoCosts = Object.entries(costs.by_repo || {})
//     .sort((a, b) => b[1].cost - a[1].cost)
//     .map(([name, d]) => '<span class="cost-repo">' + name + ": $" + d.cost.toFixed(2) + "</span>")
//     .join(" &middot; ");
//   const outputTok = costs.total_output_tokens || 0;
//   const tokStr = outputTok >= 1000000 ? (outputTok / 1000000).toFixed(1) + "M" :
//     outputTok >= 1000 ? (outputTok / 1000).toFixed(1) + "K" : outputTok;
//   return '<div class="cost-bar">' +
//     '<span class="cost-total">$' + costs.total_cost.toFixed(2) + "</span>" +
//     '<span class="cost-detail">' + tokStr + " output tokens &middot; " +
//     (costs.sessions || 0) + " sessions &middot; " +
//     (costs.messages || 0) + " messages</span>" +
//     (repoCosts ? '<div style="width:100%">' + repoCosts + "</div>" : "") +
//     "</div>";
// }
function buildCostHtml(costs) { return ""; }

function buildCardActions(f, all_phases, phases_by_type) {
  const slug = f.slug;
  const buildPhase = (f.phases && f.phases.build) || {};
  const showFixBtn = buildPhase.status === "running" || buildPhase.status === "done";
  const fixPrRunning = (f.tmux_sessions || []).some(s => s.startsWith("fix-pr-"));
  const hasActiveTmux = (f.tmux_sessions || []).length > 0;

  let html = '<a href="/docs/' + encodeURIComponent(slug) + '" class="action-btn" style="text-decoration:none">Docs</a>';

  if (showFixBtn) {
    html += '<button class="action-btn" id="fix-pr-btn-' + slug + '" onclick="fixPRs(\'' + slug + '\')">Fix PRs</button>';
    if (fixPrRunning) {
      html += '<button class="action-btn btn-stop" onclick="stopFixPRs(\'' + slug + '\')">Stop Fixing</button>';
    }
  }

  // Resume dropdown
  const applicable = f.applicable_phases || phases_by_type[f.triage_type] || all_phases;
  const currentPhase = f.current_phase || "";
  const anyFailed = Object.values(f.phases || {}).some(p => p.status === "failed");
  const isPaused = !hasActiveTmux && (anyFailed || ["done", ""].includes(buildPhase.status));
  if (isPaused && applicable.length > 0) {
    html += '<div class="resume-dropdown">';
    html += '<button class="action-btn" onclick="event.stopPropagation();toggleResumeMenu(\'' + slug + '\')">Resume \u25be</button>';
    html += '<div class="resume-menu" data-slug="' + slug + '">';
    for (const phase of applicable) {
      html += '<button class="resume-menu-item" onclick="resumePipeline(\'' + slug + '\',\'' + phase + '\')">' + phase + "</button>";
    }
    html += "</div></div>";
  }

  // Cancel button
  if (hasActiveTmux) {
    html += '<button class="action-btn btn-danger" onclick="cancelFeature(\'' + slug + '\')">Cancel</button>';
  }

  return html;
}

function buildCard(f, all_phases, phases_by_type, phase_labels) {
  const slug = f.slug;
  const typeClass = "type-" + f.triage_type;
  const tmuxHtml = (f.tmux_sessions || []).map(s => '<span class="tmux-badge">' + s + "</span>").join("");

  return '<div class="card" id="card-' + slug + '" data-slug="' + slug + '">' +
    '<div class="card-header">' +
    '<span class="card-title">' + slug + "</span>" +
    '<span class="card-type ' + typeClass + '">' + f.triage_type + "</span>" +
    '<div class="card-actions">' + buildCardActions(f, all_phases, phases_by_type) + "</div>" +
    "</div>" +
    '<div class="phases" data-section="phases">' + buildPhaseBar(f, all_phases, phases_by_type, phase_labels) + "</div>" +
    '<table class="repos"><tr><th>Repo</th><th>Status</th><th>PR</th><th>CI</th></tr>' +
    '<tbody data-section="repos">' + buildRepoRows(f) + "</tbody></table>" +
    (tmuxHtml ? '<div class="tmux-badges" data-section="tmux">tmux: ' + tmuxHtml + "</div>" : '<div data-section="tmux"></div>') +
    '<div data-section="costs">' + buildCostHtml(f.costs) + "</div>" +
    "</div>";
}

// ── Key-based DOM patching ──────────────────────────────

function patchSection(card, sectionName, newHtml) {
  const el = card.querySelector('[data-section="' + sectionName + '"]');
  if (!el) return;
    // Only update if content changed.
    if (el.innerHTML !== newHtml) {
    // Preserve expanded history toggles and log tail visibility.
    if (sectionName === "repos") {
      const expandedHist = new Set();
      el.querySelectorAll(".step-history").forEach(sh => {
        if (sh.style.display !== "none") expandedHist.add(sh.id);
      });
      el.innerHTML = newHtml;
      expandedHist.forEach(id => {
        const restored = document.getElementById(id);
        if (restored) restored.style.display = "";
      });
      // Restore per-repo log tail visibility from expandedLogs state.
      expandedLogs.forEach(key => {
        const logRow = document.getElementById("log-row-" + key);
        if (logRow) logRow.style.display = "";
        const chevron = document.getElementById("chevron-" + key);
        if (chevron) chevron.textContent = "\u25be";
      });
    } else {
      el.innerHTML = newHtml;
    }
  }
}

function render(data) {
  const { features, phase_labels, phases_by_type, all_phases } = data;
  const app = document.getElementById("app");

  if (!features.length) {
    app.innerHTML = '<div class="empty">No features in progress.<br>Start one with: <code>uv run orchestrate.py --issue &lt;url&gt;</code></div>';
    prevData = data;
    return;
  }

  // Build a set of current slugs.
  const currentSlugs = new Set(features.map(f => f.slug));

  // Remove non-card children (e.g. the initial "Loading..." placeholder).
  Array.from(app.children).forEach(child => {
    if (!child.classList.contains("card")) child.remove();
  });

  // Remove cards for features no longer present.
  app.querySelectorAll(".card").forEach(card => {
    if (!currentSlugs.has(card.dataset.slug)) card.remove();
  });

  // Update or create cards.
  for (const f of features) {
    const existing = document.getElementById("card-" + f.slug);
    if (existing) {
      // Patch individual sections.
      patchSection(existing, "phases", buildPhaseBar(f, all_phases, phases_by_type, phase_labels));
      patchSection(existing, "repos", buildRepoRows(f));

      const tmuxHtml = (f.tmux_sessions || []).map(s => '<span class="tmux-badge">' + s + "</span>").join("");
      patchSection(existing, "tmux", tmuxHtml ? "tmux: " + tmuxHtml : "");
      patchSection(existing, "costs", buildCostHtml(f.costs));

      // Update card actions (these change based on tmux state).
      const actionsEl = existing.querySelector(".card-actions");
      const newActions = buildCardActions(f, all_phases, phases_by_type);
      if (actionsEl && actionsEl.innerHTML !== newActions) {
        actionsEl.innerHTML = newActions;
      }
    } else {
      // New card -- append.
      app.insertAdjacentHTML("beforeend", buildCard(f, all_phases, phases_by_type, phase_labels));
    }
  }

  prevData = data;
}

// ── Error banner ────────────────────────────────────────

function showError(msg) {
  const banner = document.getElementById("error-banner");
  if (banner) {
    banner.textContent = msg;
    banner.classList.add("visible");
  }
}

function hideError() {
  const banner = document.getElementById("error-banner");
  if (banner) banner.classList.remove("visible");
}

// ── Polling ─────────────────────────────────────────────

async function refresh() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    consecutiveFailures = 0;
    hideError();
    render(data);
    checkNotifications(data.features || []);
    document.getElementById("updated").textContent = new Date().toLocaleTimeString();
    applyFilter();
  } catch (e) {
    consecutiveFailures++;
    console.error("Refresh failed:", e);
    if (consecutiveFailures >= 3) {
      showError("Server unreachable -- last attempt " + new Date().toLocaleTimeString());
    } else {
      showError("Refresh failed -- retrying...");
    }
  }
}

// ── Init ────────────────────────────────────────────────

updateNotifStatus();
initFilter();
refresh();
setInterval(refresh, 5000);
