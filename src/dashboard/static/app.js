const summaryCards = document.getElementById("summary-cards");
const summaryCardTemplate = document.getElementById("summary-card-template");
const timeline = document.getElementById("timeline");
const timelineSource = document.getElementById("timeline-source");
const runStatus = document.getElementById("run-status");
const currentJob = document.getElementById("current-job");
const liveMetrics = document.getElementById("live-metrics");
const jobsBody = document.getElementById("jobs-body");
const jobsCount = document.getElementById("jobs-count");
const jobDetails = document.getElementById("job-details");
const screenshot = document.getElementById("screenshot");
const screenshotMeta = document.getElementById("screenshot-meta");
const refreshJobsButton = document.getElementById("refresh-jobs");
const jobStatusFilter = document.getElementById("job-status-filter");
const jobSearch = document.getElementById("job-search");
const searchConfigForm = document.getElementById("search-config-form");
const appConfigForm = document.getElementById("app-config-form");
const runHistory = document.getElementById("run-history");
const runDetailSummary = document.getElementById("run-detail-summary");
const screenshotHistory = document.getElementById("screenshot-history");
const exportRunButton = document.getElementById("export-run");
const dashboardLoading = document.getElementById("dashboard-loading");

let dashboardConfig = null;
let currentJobs = [];
let selectedRunId = null;
const initialLoadingStartedAt = performance.now();
const minimumInitialLoadingMs = 450;

function setDashboardLoading(isLoading) {
  if (!dashboardLoading) {
    return;
  }

  document.body.classList.toggle("dashboard-loading", isLoading);
  document.body.setAttribute("aria-busy", String(isLoading));
  dashboardLoading.setAttribute("aria-hidden", String(!isLoading));
}

async function clearInitialLoading() {
  const elapsedMs = performance.now() - initialLoadingStartedAt;
  const remainingMs = Math.max(0, minimumInitialLoadingMs - elapsedMs);
  if (remainingMs > 0) {
    await new Promise((resolve) => setTimeout(resolve, remainingMs));
  }
  setDashboardLoading(false);
}

function formatDateTime(value) {
  if (!value) {
    return "-";
  }

  const parsed = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  const hours = String(parsed.getHours()).padStart(2, "0");
  const minutes = String(parsed.getMinutes()).padStart(2, "0");
  const seconds = String(parsed.getSeconds()).padStart(2, "0");

  return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function formatResumePath(path) {
  if (!path) return "-";
  return path.split(/[\\/]/).pop() || path;
}

function truncateText(value, maxLength = 120) {
  if (!value) return "-";
  return value.length > maxLength ? `${value.slice(0, maxLength).trim()}...` : value;
}

function currentRunPath(runId) {
  return runId ? `/runs/${encodeURIComponent(runId)}` : "/";
}

function syncUrlForRun(runId) {
  window.history.replaceState({}, "", currentRunPath(runId));
}

function getInitialSelectedRunId() {
  const match = window.location.pathname.match(/^\/runs\/([^/]+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function renderTimeline(events) {
  timeline.innerHTML = events
    .slice(-40)
    .reverse()
    .map((event) => `
      <article class="timeline-item">
        <p class="timeline-time">${formatDateTime(event.timestamp)}</p>
        <p class="timeline-title">${event.message}</p>
        <pre>${JSON.stringify(event.payload || {}, null, 2)}</pre>
      </article>
    `)
    .join("");
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || response.statusText);
  }
  return response.json();
}

function makeSummaryCards(summary) {
  const cards = [
    ["Run Status", summary.run.status],
    ["Applied", summary.totals.applied],
    ["Skipped", summary.totals.skipped],
    ["Failed", summary.totals.failed],
    ["Interesting", summary.totals.interesting],
    ["Live Discovered", summary.totals.discovered_live],
    ["LLM Calls", summary.totals.llm_calls],
    ["LLM Cost", `$${summary.totals.llm_total_cost}`],
  ];

  summaryCards.innerHTML = "";
  for (const [label, value] of cards) {
    const card = summaryCardTemplate.content.firstElementChild.cloneNode(true);
    card.querySelector(".summary-label").textContent = label;
    card.querySelector(".summary-value").textContent = value ?? "-";
    summaryCards.appendChild(card);
  }
}

function renderLive(snapshot, events) {
  runStatus.textContent = snapshot.run_status || "idle";
  runStatus.dataset.status = snapshot.run_status || "idle";

  if (snapshot.current_job) {
    const stage = snapshot.current_job.stage ? ` (${snapshot.current_job.stage})` : "";
    const detail = snapshot.current_job.message ? ` - ${snapshot.current_job.message}` : "";
    currentJob.textContent = `${snapshot.current_job.title || "Unknown role"} at ${snapshot.current_job.company || "Unknown company"}${stage}${detail}`;
  } else {
    currentJob.textContent = "No active job";
  }

  const metrics = {
    Discovered: snapshot.counters?.discovered || 0,
    Evaluated: snapshot.counters?.evaluated || 0,
    Interesting: snapshot.counters?.interesting || 0,
    Applied: snapshot.counters?.applied || 0,
    Skipped: snapshot.counters?.skipped || 0,
    Failed: snapshot.counters?.failed || 0,
    Page: snapshot.page_num || 0,
    Paused: snapshot.paused ? "Yes" : "No",
  };

  liveMetrics.innerHTML = Object.entries(metrics)
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`)
    .join("");

  renderTimeline(events);
}

function renderJobs(jobs, counts = {}) {
  currentJobs = jobs;
  const filteredCount = counts.filtered_count ?? jobs.length;
  const totalCount = counts.total_count ?? jobs.length;
  jobsCount.textContent = filteredCount === totalCount
    ? `(${totalCount})`
    : `(${filteredCount} / ${totalCount})`;
  jobsBody.innerHTML = jobs
    .map((job, index) => {
      const reason = job.skip_reason || job.interest_reason || "";
      return `
      <tr class="job-row" data-job-index="${index}">
        <td><span class="badge" data-status="${job.status}">${job.status}</span></td>
        <td>${formatDateTime(job.executed_at || job.updated_at)}</td>
        <td><a href="${job.url || "#"}" target="_blank" rel="noreferrer">${job.job_title || "Unknown job"}</a></td>
        <td>${job.company_name || "-"}</td>
        <td>${job.interest_score ?? "-"}</td>
        <td class="reason-preview" title="${reason.replaceAll('"', '&quot;')}">${truncateText(reason)}</td>
      </tr>
    `;
    })
    .join("");

  for (const row of jobsBody.querySelectorAll(".job-row")) {
    row.onclick = () => renderJobDetails(currentJobs[Number(row.dataset.jobIndex)]);
  }

  if (jobs.length > 0) {
    renderJobDetails(jobs[0]);
  } else {
    jobDetails.textContent = "No jobs match the current filter.";
    jobDetails.className = "job-details muted";
  }
}

function renderJobDetails(job) {
  if (!job) {
    jobDetails.textContent = "Select a job from the board to inspect it.";
    jobDetails.className = "job-details muted";
    return;
  }

  jobDetails.className = "job-details";
  jobDetails.innerHTML = `
    <div class="detail-header">
      <span class="badge" data-status="${job.status}">${job.status}</span>
      <h3>${job.job_title || "Unknown job"}</h3>
      <p class="muted">${job.company_name || "Unknown company"}</p>
    </div>
    <dl class="detail-grid">
      <div><dt>URL</dt><dd>${job.url ? `<a href="${job.url}" target="_blank" rel="noreferrer">Open posting</a>` : "-"}</dd></div>
      <div><dt>Executed At</dt><dd>${formatDateTime(job.executed_at || job.updated_at)}</dd></div>
      <div><dt>Applied At</dt><dd>${job.applied_at ? formatDateTime(job.applied_at) : job.applied_at_text || "-"}</dd></div>
      <div><dt>Interest Score</dt><dd>${job.interest_score ?? "-"}</dd></div>
      <div><dt>Skip Reason</dt><dd>${job.skip_reason || "-"}</dd></div>
      <div><dt>Interest Reason</dt><dd>${job.interest_reason || "-"}</dd></div>
      <div><dt>Submitted Resume</dt><dd>${job.submitted_resume_path || "-"}</dd></div>
      <div><dt>Skills</dt><dd>${Array.isArray(job.skills) ? job.skills.join(", ") : job.skills || "-"}</dd></div>
    </dl>
  `;
  jobDetails.scrollTop = 0;
}

function renderRunHistory(runs) {
  runHistory.innerHTML = runs.length
    ? runs.map((run) => `
      <article class="run-card panel ${selectedRunId === run.run_id ? "selected" : ""}" data-run-id="${run.run_id}">
        <div class="section-header compact">
          <strong>${run.run_id}</strong>
          <span class="badge" data-status="${run.status}">${run.status}</span>
        </div>
        <p class="muted">Started: ${formatDateTime(run.started_at)}</p>
        <p class="muted">Finished: ${formatDateTime(run.finished_at)}</p>
        <p class="muted">Last event: ${formatDateTime(run.last_event_at)}</p>
        <p>${run.last_message || "No message"}</p>
        <div class="run-stats">
          <span>Discovered ${run.jobs.discovered}</span>
          <span>Interesting ${run.jobs.interesting}</span>
          <span>Applied ${run.jobs.applied}</span>
          <span>Skipped ${run.jobs.skipped}</span>
          <span>Failed ${run.jobs.failed}</span>
        </div>
      </article>
    `).join("")
    : '<p class="muted">No runs recorded yet.</p>';

  for (const card of runHistory.querySelectorAll(".run-card[data-run-id]")) {
    card.onclick = () => selectRun(card.dataset.runId);
  }
}

function renderRunDetailSummary(run) {
  if (!run) {
    runDetailSummary.textContent = "Select a run to inspect it directly.";
    runDetailSummary.className = "run-detail-summary muted";
    exportRunButton.disabled = true;
    return;
  }

  runDetailSummary.className = "run-detail-summary";
  exportRunButton.disabled = false;
  runDetailSummary.innerHTML = `
    <div class="run-detail-card">
      <strong>Run ID</strong>
      <span>${run.run_id}</span>
    </div>
    <div class="run-detail-card">
      <strong>Status</strong>
      <span>${run.status || "unknown"}</span>
    </div>
    <div class="run-detail-card">
      <strong>Started</strong>
      <span>${formatDateTime(run.started_at)}</span>
    </div>
    <div class="run-detail-card">
      <strong>Finished</strong>
      <span>${formatDateTime(run.finished_at)}</span>
    </div>
    <div class="run-detail-card">
      <strong>Last Event</strong>
      <span>${formatDateTime(run.last_event_at)}</span>
    </div>
    <div class="run-detail-card">
      <strong>Last Message</strong>
      <span>${run.last_message || "-"}</span>
    </div>
    <div class="run-detail-card">
      <strong>Discovered</strong>
      <span>${run.jobs?.discovered ?? 0}</span>
    </div>
    <div class="run-detail-card">
      <strong>Applied</strong>
      <span>${run.jobs?.applied ?? 0}</span>
    </div>
    <div class="run-detail-card">
      <strong>Skipped</strong>
      <span>${run.jobs?.skipped ?? 0}</span>
    </div>
    <div class="run-detail-card">
      <strong>Failed</strong>
      <span>${run.jobs?.failed ?? 0}</span>
    </div>
  `;
}

function renderScreenshotHistory(entries) {
  if (!entries || entries.length === 0) {
    screenshotHistory.textContent = selectedRunId
      ? "No screenshots captured for this run yet."
      : "Select a run to see its screenshots.";
    screenshotHistory.className = "screenshot-history muted";
    return;
  }

  screenshotHistory.className = "screenshot-history";
  screenshotHistory.innerHTML = entries
    .map((entry) => `
      <article class="screenshot-history-item">
        <a href="/api/screenshot-file?path=${encodeURIComponent(entry.path)}" target="_blank" rel="noreferrer">
          <img src="/api/screenshot-file?path=${encodeURIComponent(entry.path)}" alt="${entry.label || "screenshot"}">
        </a>
        <strong>${entry.label || "screenshot"}</strong>
        <span class="muted">${formatDateTime(entry.timestamp)}</span>
        <span class="muted">${entry.company_name || ""} ${entry.job_title ? `- ${entry.job_title}` : ""}</span>
        <span class="muted">${entry.stage || ""}</span>
      </article>
    `)
    .join("");
}

async function selectRun(runId) {
  selectedRunId = runId;
  timelineSource.textContent = `Showing timeline for ${runId}`;
  syncUrlForRun(runId);
  const payload = await fetchJson(`/api/runs/${runId}`);
  renderRunDetailSummary(payload.run);
  renderTimeline(payload.events);
  renderJobs(payload.jobs, payload);
  renderScreenshotHistory(payload.screenshots || []);
  await refreshRuns();
}

function clearRunFilter() {
  selectedRunId = null;
  timelineSource.textContent = "Showing live timeline";
  renderRunDetailSummary(null);
  renderScreenshotHistory([]);
  syncUrlForRun(null);
}

function buildSearchConfigForm(config) {
  const dateConfig = config.date || {};
  searchConfigForm.innerHTML = `
    <label>Positions<textarea name="positions">${(config.positions || []).join("\n")}</textarea></label>
    <label>Locations<textarea name="locations">${(config.locations || []).join("\n")}</textarea></label>
    <label>Company Blacklist<textarea name="company_blacklist">${(config.company_blacklist || []).join("\n")}</textarea></label>
    <label>Title Blacklist<textarea name="title_blacklist">${(config.title_blacklist || []).join("\n")}</textarea></label>
    <label>Location Blacklist<textarea name="location_blacklist">${(config.location_blacklist || []).join("\n")}</textarea></label>
    <label class="checkbox"><input type="checkbox" name="remote" ${config.remote ? "checked" : ""}>Remote</label>
    <label class="checkbox"><input type="checkbox" name="hybrid" ${config.hybrid ? "checked" : ""}>Hybrid</label>
    <label class="checkbox"><input type="checkbox" name="onsite" ${config.onsite ? "checked" : ""}>On-site</label>
    <label class="checkbox"><input type="checkbox" name="apply_once_at_company" ${config.apply_once_at_company ? "checked" : ""}>Apply once at company</label>
    <fieldset>
      <legend>Experience Level</legend>
      ${renderBooleanGroup("experience_level", config.experience_level || {})}
    </fieldset>
    <fieldset>
      <legend>Job Types</legend>
      ${renderBooleanGroup("job_types", config.job_types || {})}
    </fieldset>
    <fieldset>
      <legend>Date Posted</legend>
      <label class="checkbox"><input type="checkbox" name="date.all_time" ${dateConfig.all_time ? "checked" : ""}>All time</label>
      <label class="checkbox"><input type="checkbox" name="date.month" ${dateConfig.month ? "checked" : ""}>Month</label>
      <label class="checkbox"><input type="checkbox" name="date.week" ${dateConfig.week ? "checked" : ""}>Week</label>
      <label class="checkbox"><input type="checkbox" name="date.24_hours" ${dateConfig["24_hours"] ? "checked" : ""}>24 hours</label>
    </fieldset>
  `;
}

function renderBooleanGroup(prefix, values) {
  return Object.entries(values)
    .map(([key, value]) => `<label class="checkbox"><input type="checkbox" name="${prefix}.${key}" ${value ? "checked" : ""}>${key}</label>`)
    .join("");
}

function buildAppConfigForm(config) {
  appConfigForm.innerHTML = Object.entries(config)
    .map(([key, value]) => {
      if (typeof value === "boolean") {
        return `<label class="checkbox"><input type="checkbox" name="${key}" ${value ? "checked" : ""}>${key}</label>`;
      }
      return `<label>${key}<input type="text" name="${key}" value="${value ?? ""}"></label>`;
    })
    .join("");
}

function collectSearchConfig() {
  const data = {
    positions: [],
    locations: [],
    company_blacklist: [],
    title_blacklist: [],
    location_blacklist: [],
    remote: false,
    hybrid: false,
    onsite: false,
    apply_once_at_company: false,
    experience_level: {},
    job_types: {},
    date: { all_time: false, month: false, week: false, "24_hours": false },
  };

  for (const element of searchConfigForm.elements) {
    if (!element.name) continue;
    if (element.type === "textarea") {
      data[element.name] = element.value.split("\n").map((line) => line.trim()).filter(Boolean);
      continue;
    }
    if (element.type === "checkbox") {
      if (element.name.includes(".")) {
        const [group, key] = element.name.split(".");
        data[group][key] = element.checked;
      } else {
        data[element.name] = element.checked;
      }
    }
  }
  return data;
}

function collectAppConfig() {
  const data = {};
  for (const element of appConfigForm.elements) {
    if (!element.name) continue;
    if (element.type === "checkbox") {
      data[element.name] = element.checked;
      continue;
    }
    const originalValue = dashboardConfig.app[element.name];
    if (typeof originalValue === "number") {
      data[element.name] = Number(element.value);
    } else if (originalValue === null) {
      data[element.name] = element.value.trim() ? element.value : null;
    } else {
      data[element.name] = element.value;
    }
  }
  return data;
}

async function refreshSummary() {
  const summary = await fetchJson("/api/summary");
  makeSummaryCards(summary);
}

async function refreshLive() {
  const payload = await fetchJson("/api/live");
  renderLive(payload.snapshot, payload.events);
  await refreshScreenshot();
}

async function refreshJobs() {
  if (refreshJobsButton) {
    refreshJobsButton.disabled = true;
    refreshJobsButton.textContent = "Refreshing...";
  }
  const query = new URLSearchParams();
  if (jobStatusFilter.value) query.set("status", jobStatusFilter.value);
  if (jobSearch.value) query.set("search", jobSearch.value);
  const endpoint = selectedRunId ? `/api/runs/${selectedRunId}/jobs` : "/api/jobs";
  try {
    const payload = await fetchJson(`${endpoint}?${query.toString()}`);
    renderJobs(payload.jobs, payload);
  } finally {
    if (refreshJobsButton) {
      refreshJobsButton.disabled = false;
      refreshJobsButton.textContent = "Refresh Jobs";
    }
  }
}

async function refreshConfig() {
  dashboardConfig = await fetchJson("/api/config");
  buildSearchConfigForm(dashboardConfig.search);
  buildAppConfigForm(dashboardConfig.app);
}

async function refreshRuns() {
  const payload = await fetchJson("/api/runs");
  renderRunHistory(payload.runs);
}

async function refreshScreenshot() {
  const bust = Date.now();
  const response = await fetch(`/api/screenshot?t=${bust}`);
  if (!response.ok) {
    screenshot.removeAttribute("src");
    screenshotMeta.textContent = "No screenshot yet";
    return;
  }
  screenshot.src = `/api/screenshot?t=${bust}`;
  screenshotMeta.textContent = `Last refreshed ${formatDateTime(new Date())}`;
}

async function wireControls() {
  document.getElementById("start-run").onclick = async () => {
    await fetchJson("/api/control/start", { method: "POST" });
    await refreshSummary();
    await refreshLive();
    await refreshRuns();
  };
  document.getElementById("pause-run").onclick = async () => fetchJson("/api/control/pause", { method: "POST" });
  document.getElementById("resume-run").onclick = async () => fetchJson("/api/control/resume", { method: "POST" });
  document.getElementById("stop-run").onclick = async () => fetchJson("/api/control/stop", { method: "POST" });
  document.getElementById("refresh-screenshot").onclick = refreshScreenshot;
  refreshJobsButton.onclick = refreshJobs;
  exportRunButton.onclick = () => {
    if (!selectedRunId) {
      return;
    }
    window.open(`/api/runs/${encodeURIComponent(selectedRunId)}/export`, "_blank", "noopener,noreferrer");
  };
  document.getElementById("clear-run-filter").onclick = async () => {
    clearRunFilter();
    await refreshJobs();
    await refreshLive();
    await refreshRuns();
  };
  document.getElementById("save-search-config").onclick = async () => {
    await fetchJson("/api/config/search", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: collectSearchConfig() }),
    });
    await refreshConfig();
  };
  document.getElementById("save-app-config").onclick = async () => {
    await fetchJson("/api/config/app", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config: collectAppConfig() }),
    });
    await refreshConfig();
  };
}

function wireFilters() {
  jobStatusFilter.onchange = refreshJobs;
  jobSearch.oninput = refreshJobs;
}

function startEventStream() {
  const source = new EventSource("/api/events/stream");
  source.addEventListener("snapshot", (event) => {
    const payload = JSON.parse(event.data);
    if (!selectedRunId) {
      renderLive(payload.snapshot, payload.events);
      refreshJobs();
    }
    refreshSummary();
    refreshRuns();
    refreshScreenshot();
  });
  source.addEventListener("message", (event) => {
    const payload = JSON.parse(event.data);
    if (!selectedRunId) {
      const existing = Array.from(timeline.children).slice(0, 39).map((node) => node.outerHTML).join("");
      const latest = `
        <article class="timeline-item">
          <p class="timeline-time">${formatDateTime(payload.timestamp)}</p>
          <p class="timeline-title">${payload.message}</p>
          <pre>${JSON.stringify(payload.payload || {}, null, 2)}</pre>
        </article>`;
      timeline.innerHTML = latest + existing;
      refreshJobs();
    }
    refreshSummary();
    refreshRuns();
    if (payload.type === "screenshot_updated") refreshScreenshot();
  });
}

async function refreshMeta() {
  const meta = await fetchJson("/api/meta");
  const siteName = meta.site_name || "AI";
  document.title = `${siteName} AI Job Applier Dashboard`;
  const eyebrow = document.getElementById("site-eyebrow");
  if (eyebrow) eyebrow.textContent = `${siteName} AI Job Applier`;
}

async function init() {
  selectedRunId = getInitialSelectedRunId();
  try {
    await wireControls();
    wireFilters();
    await Promise.all([refreshMeta(), refreshSummary(), refreshLive(), refreshJobs(), refreshConfig(), refreshRuns()]);
    if (selectedRunId) {
      await selectRun(selectedRunId);
    } else {
      renderRunDetailSummary(null);
      renderScreenshotHistory([]);
    }
    startEventStream();
  } finally {
    await clearInitialLoading();
  }
}

init().catch((error) => {
  console.error(error);
  alert(error.message);
});
