"use strict";

const authCard = document.getElementById("auth-card");
const triageCard = document.getElementById("triage-card");
const resultsCard = document.getElementById("results-card");
const resultsEl = document.getElementById("results");
const draftedStatEl = document.getElementById("drafted-stat");
const runBtn = document.getElementById("run-btn");
const statusEl = document.getElementById("status");
const triageProgress = document.getElementById("triage-progress");
const triageProgressFill = document.getElementById("triage-progress-fill");
const triageProgressLabel = document.getElementById("triage-progress-label");
const stopTriageBtn = document.getElementById("stop-triage-btn");
const userBox = document.getElementById("user-box");
const userEmailEl = document.getElementById("user-email");
const signoutBtn = document.getElementById("signout-btn");
const runDateInput = document.getElementById("run-date");
const runDateField = document.getElementById("run-date-field");
const scheduleDateInput = document.getElementById("schedule-date");
const scheduleHour = document.getElementById("schedule-hour");
const scheduleMinute = document.getElementById("schedule-minute");
const scheduleAmpm = document.getElementById("schedule-ampm");
const scheduleBtn = document.getElementById("schedule-btn");
const scheduledLine = document.getElementById("scheduled-line");
const scheduledTextEl = document.getElementById("scheduled-text");
const cancelScheduleLink = document.getElementById("cancel-schedule");
const openGmailLink = document.getElementById("open-gmail-link");
const openDraftsLink = document.getElementById("open-drafts-link");
const linksCard = document.getElementById("links-card");
const categoriesCard = document.getElementById("categories-card");
const categoryListEl = document.getElementById("category-list");
const addCategoryBtn = document.getElementById("add-category-btn");
const faqSelect = document.getElementById("faq-select");
const saveCategoriesBtn = document.getElementById("save-categories-btn");
const categoriesStatusEl = document.getElementById("categories-status");
const searchCard = document.getElementById("search-card");
const searchInput = document.getElementById("search-input");
const searchBtn = document.getElementById("search-btn");
const searchResultsEl = document.getElementById("search-results");

const DRAFTED_KEY = "FAQ (drafted)";

// This category is always kept and cannot be removed or renamed in the UI --
// it is the catch-all for emails that don't fit any other category.
const FIXED_CATEGORY = "Low Priority";

// The user's working copy of their category list (edited in the Categories
// card, saved via POST /api/settings/categories).
let currentCategories = [];

// Fixed category order so the dashboard layout stays stable between runs.
const CATEGORY_ORDER = [
  "Needs Action",
  "Red Flag",
  "FAQ",
  "Low Priority",
  "Spam/Newsletter",
];

// True while a background poll loop (manual run or watching a schedule) is
// active. Only one loop runs at a time.
let pollActive = false;

// True while a one-time run is scheduled for later; manual runs are blocked and
// the Run button is replaced by the scheduled banner.
let isScheduled = false;

async function init() {
  buildNeuralTree();

  try {
    const res = await fetch("/api/auth/status");
    if (res.status === 401) {
      showConnect();
      return;
    }
    const data = await res.json();

    if (data.authenticated) {
      showAuthenticated(data.email);
      await loadCategories();
      loadLastSummary();
      await refreshScheduleState();
      startWatchLoop();
    } else {
      showConnect();
    }
  } catch (err) {
    showConnect();
  }
}

function showConnect() {
  authCard.classList.remove("hidden");
  triageCard.classList.add("hidden");
  categoriesCard.classList.add("hidden");
  searchCard.classList.add("hidden");
  linksCard.classList.add("hidden");
  userBox.classList.add("hidden");
}

function showAuthenticated(email) {
  authCard.classList.add("hidden");
  triageCard.classList.remove("hidden");
  categoriesCard.classList.remove("hidden");
  searchCard.classList.remove("hidden");
  linksCard.classList.remove("hidden");
  userBox.classList.remove("hidden");
  userEmailEl.textContent = email || "";

  // Populate the schedule time picker and default both date inputs to today.
  initScheduleControls();
  const today = todayLocalDate();
  if (!runDateInput.value) runDateInput.value = today;
  if (!scheduleDateInput.value) scheduleDateInput.value = today;

  // Point quick links at the specific connected account, not whichever
  // Google account happens to be signed in first in the browser.
  if (email) {
    const encoded = encodeURIComponent(email);
    openGmailLink.href = `https://mail.google.com/mail/?authuser=${encoded}#inbox`;
    openDraftsLink.href = `https://mail.google.com/mail/?authuser=${encoded}#drafts`;
  }
}

async function signOut() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch (err) {
    // Reload regardless of the response.
  }
  window.location.reload();
}

async function loadLastSummary() {
  try {
    const res = await fetch("/api/summary");
    if (res.status === 401) {
      showConnect();
      return;
    }
    const { counts } = await res.json();
    if (counts) {
      renderCounts(counts);
    }
  } catch (err) {
    // No prior summary is fine; ignore.
  }
}

// --- Category settings ---

async function loadCategories() {
  try {
    const res = await fetch("/api/settings/categories");
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    currentCategories = Array.isArray(data.categories)
      ? data.categories.slice()
      : [];
    ensureFixedCategory();
    renderCategoryList();
    rebuildFaqSelect(data.faq_category || "");
  } catch (err) {
    // Ignore; the card just stays empty until the user adds categories.
  }
}

function ensureFixedCategory() {
  const has = currentCategories.some(
    (c) => c.trim().toLowerCase() === FIXED_CATEGORY.toLowerCase()
  );
  if (!has) currentCategories.push(FIXED_CATEGORY);
}

function renderCategoryList() {
  categoryListEl.innerHTML = "";

  currentCategories.forEach((cat, index) => {
    const row = document.createElement("div");
    row.className = "category-row";

    const isFixed =
      cat.trim().toLowerCase() === FIXED_CATEGORY.toLowerCase();

    const input = document.createElement("input");
    input.type = "text";
    input.className = "category-input";
    input.value = cat;
    if (isFixed) {
      // Fixed catch-all: can't be renamed or removed.
      input.readOnly = true;
      input.title =
        "Low Priority is always kept as the catch-all category and can't be renamed or removed.";
    } else {
      input.addEventListener("input", () => {
        currentCategories[index] = input.value;
        rebuildFaqSelect();
      });
    }

    row.appendChild(input);

    if (isFixed) {
      const badge = document.createElement("span");
      badge.className = "category-fixed muted";
      badge.textContent = "Always on";
      row.appendChild(badge);
    } else {
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "btn btn-ghost category-remove";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        currentCategories.splice(index, 1);
        renderCategoryList();
        rebuildFaqSelect();
      });
      row.appendChild(removeBtn);
    }

    categoryListEl.appendChild(row);
  });
}

function rebuildFaqSelect(selected) {
  // Preserve the current selection unless an explicit one is given.
  const target = selected !== undefined ? selected : faqSelect.value;
  faqSelect.innerHTML = "";

  const noneOption = document.createElement("option");
  noneOption.value = "";
  noneOption.textContent = "None";
  faqSelect.appendChild(noneOption);

  const seen = new Set();
  currentCategories.forEach((cat) => {
    const trimmed = cat.trim();
    if (!trimmed || seen.has(trimmed)) return;
    seen.add(trimmed);
    const option = document.createElement("option");
    option.value = trimmed;
    option.textContent = trimmed;
    faqSelect.appendChild(option);
  });

  faqSelect.value = target || "";
  if (faqSelect.value !== (target || "")) {
    // The previously selected category no longer exists; fall back to None.
    faqSelect.value = "";
  }
}

function addCategory() {
  currentCategories.push("");
  renderCategoryList();
  rebuildFaqSelect();

  const inputs = categoryListEl.querySelectorAll(".category-input");
  if (inputs.length) inputs[inputs.length - 1].focus();
}

async function saveCategories() {
  const inputs = categoryListEl.querySelectorAll(".category-input");
  const cleaned = [];
  inputs.forEach((input) => {
    const value = input.value.trim();
    if (value) cleaned.push(value);
  });

  // The fixed catch-all is always saved, even if somehow missing from the UI.
  if (!cleaned.some((c) => c.toLowerCase() === FIXED_CATEGORY.toLowerCase())) {
    cleaned.push(FIXED_CATEGORY);
  }

  if (cleaned.length === 0) {
    categoriesStatusEl.textContent = "Add at least one category.";
    return;
  }

  let faqCategory = faqSelect.value || null;
  if (faqCategory && !cleaned.includes(faqCategory)) {
    faqCategory = null;
  }

  saveCategoriesBtn.disabled = true;
  categoriesStatusEl.textContent = "Saving…";

  try {
    const res = await fetch("/api/settings/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categories: cleaned, faq_category: faqCategory }),
    });
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `Server responded ${res.status}`);
    }
    const data = await res.json();
    currentCategories = Array.isArray(data.categories)
      ? data.categories.slice()
      : cleaned;
    ensureFixedCategory();
    renderCategoryList();
    rebuildFaqSelect(data.faq_category || "");
    categoriesStatusEl.textContent = "Saved.";
  } catch (err) {
    categoriesStatusEl.textContent = `Could not save: ${err.message}`;
  } finally {
    saveCategoriesBtn.disabled = false;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatRunAt(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  let hour = d.getHours();
  const minute = String(d.getMinutes()).padStart(2, "0");
  const ampm = hour >= 12 ? "PM" : "AM";
  hour = hour % 12 || 12;
  const day = d.getDate();
  const month = d.toLocaleString(undefined, { month: "long" });
  const year = d.getFullYear();
  return `${hour}:${minute} ${ampm} on ${day}${ordinalSuffix(day)} ${month} ${year}`;
}

function ordinalSuffix(n) {
  const v = n % 100;
  if (v >= 11 && v <= 13) return "th";
  return { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th";
}

function showScheduled(runAtIso) {
  isScheduled = true;
  scheduledLine.classList.remove("hidden");
  scheduledTextEl.textContent = `Scheduled for ${formatRunAt(runAtIso)}`;
  // A manual run can't be started while one is scheduled: replace the Run
  // Triage button (and its date) with the scheduled message.
  runBtn.classList.add("hidden");
  runDateField.classList.add("hidden");
  setScheduleInputsDisabled(true);
}

function hideScheduled() {
  isScheduled = false;
  scheduledLine.classList.add("hidden");
  runBtn.classList.remove("hidden");
  runDateField.classList.remove("hidden");
  setScheduleInputsDisabled(false);
  // IMPORTANT: do NOT clear the inputs here. refreshScheduleState() calls this
  // on every poll, so clearing would wipe what the user is entering. Only an
  // explicit Cancel resets the fields.
}

function setScheduleInputsDisabled(disabled) {
  scheduleDateInput.disabled = disabled;
  scheduleHour.disabled = disabled;
  scheduleMinute.disabled = disabled;
  scheduleAmpm.disabled = disabled;
  scheduleBtn.disabled = disabled;
}

async function refreshScheduleState() {
  try {
    const res = await fetch("/api/triage/schedule");
    if (!res.ok) return;
    const data = await res.json();
    if (data.run_at) {
      showScheduled(data.run_at);
    } else {
      hideScheduled();
    }
  } catch (err) {
    // Ignore; leave current UI state as-is.
  }
}

function todayLocalDate() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function initScheduleControls() {
  if (scheduleHour.options.length === 0) {
    for (let h = 1; h <= 12; h++) {
      const o = document.createElement("option");
      o.value = String(h);
      o.textContent = String(h);
      scheduleHour.appendChild(o);
    }
  }
  if (scheduleMinute.options.length === 0) {
    for (let m = 0; m < 60; m++) {
      const o = document.createElement("option");
      o.value = String(m);
      o.textContent = String(m).padStart(2, "0");
      scheduleMinute.appendChild(o);
    }
  }
}

async function onRunClick() {
  if (isScheduled) return; // a run is scheduled -> manual run is disabled
  const today = todayLocalDate();
  const date = runDateInput.value || today;
  if (date > today) {
    statusEl.textContent =
      'Run date can\u2019t be in the future \u2014 use "Schedule triage" below for later runs.';
    return;
  }
  await runTriage(date);
}

async function onScheduleClick() {
  const date = scheduleDateInput.value;
  if (!date) {
    statusEl.textContent = "Pick a date to schedule.";
    return;
  }
  const hour12 = parseInt(scheduleHour.value, 10);
  const minute = parseInt(scheduleMinute.value, 10);
  let hour24 = hour12 % 12;
  if (scheduleAmpm.value === "PM") hour24 += 12;
  const localValue = `${date}T${String(hour24).padStart(2, "0")}:${String(
    minute
  ).padStart(2, "0")}`;
  const chosen = new Date(localValue);
  if (Number.isNaN(chosen.getTime()) || chosen.getTime() <= Date.now()) {
    statusEl.textContent = "Please choose a future date and time.";
    return;
  }
  await scheduleRun(localValue, date);
}

async function scheduleRun(localDateTimeValue, dateFilter) {
  scheduleBtn.disabled = true;
  statusEl.textContent = "Scheduling…";

  try {
    const runAtIso = new Date(localDateTimeValue).toISOString();
    const res = await fetch("/api/triage/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_at: runAtIso, date: dateFilter }),
    });
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `Server responded ${res.status}`);
    }
    const data = await res.json();
    showScheduled(data.run_at);
    statusEl.textContent = "";
  } catch (err) {
    statusEl.textContent = `Could not schedule: ${err.message}`;
  } finally {
    scheduleBtn.disabled = false;
  }
}

async function cancelSchedule(event) {
  event.preventDefault();
  if (cancelScheduleLink.classList.contains("disabled")) return;
  try {
    await fetch("/api/triage/schedule/cancel", { method: "POST" });
  } catch (err) {
    // Revert the UI regardless of the response.
  }
  hideScheduled();
  scheduleDateInput.value = todayLocalDate();
}

function setSearchEnabled(enabled) {
  searchInput.disabled = !enabled;
  searchBtn.disabled = !enabled;
}

function setBusy(isBusy) {
  // Disable interactive controls while a run is in progress. Quick links stay
  // clickable; search is unlocked separately once the first chunk lands.
  runBtn.disabled = isBusy;
  runDateInput.disabled = isBusy;
  setScheduleInputsDisabled(isBusy);
  addCategoryBtn.disabled = isBusy;
  saveCategoriesBtn.disabled = isBusy;
  faqSelect.disabled = isBusy;
  categoryListEl.querySelectorAll("input, button").forEach((el) => {
    el.disabled = isBusy;
  });
  cancelScheduleLink.classList.toggle("disabled", isBusy);

  stopTriageBtn.classList.toggle("hidden", !isBusy);
  stopTriageBtn.disabled = false;

  // Search is locked during a run until the first chunk of emails is embedded.
  setSearchEnabled(!isBusy);
}

function showProgressStarting() {
  triageProgress.classList.remove("hidden");
  triageProgressFill.style.width = "0%";
  triageProgressLabel.textContent = "Triage in progress…";
}

function renderProgress(progress) {
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  triageProgress.classList.remove("hidden");
  triageProgressFill.style.width = `${percent}%`;
  triageProgressLabel.textContent =
    percent >= 100 ? "Completed" : `Triage in progress… ${percent}%`;
}

function hideProgress() {
  triageProgress.classList.add("hidden");
}

async function cancelTriage() {
  stopTriageBtn.disabled = true;
  statusEl.textContent = "Stopping…";
  try {
    await fetch("/api/triage/cancel", { method: "POST" });
  } catch (err) {
    // The poll loop will reflect the final (cancelled) state.
  }
}

async function runTriage(dateFilter) {
  setBusy(true);
  statusEl.textContent = "";
  resultsCard.classList.add("hidden");
  showProgressStarting();

  try {
    const options = { method: "POST" };
    if (dateFilter) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ date: dateFilter });
    }
    const res = await fetch("/api/triage", options);
    if (res.status === 401) {
      showConnect();
      statusEl.textContent = "";
      hideProgress();
      return;
    }
    if (res.status === 409) {
      statusEl.textContent = "A triage run is already in progress.";
      await pollUntilDone();
      return;
    }
    if (!res.ok) {
      throw new Error(`Server responded ${res.status}`);
    }
    await pollUntilDone();
  } catch (err) {
    statusEl.textContent = `Triage failed: ${err.message}`;
    hideProgress();
  } finally {
    setBusy(false);
  }
}

async function pollUntilDone() {
  // Poll the background task's status every 2 seconds until it finishes.
  while (true) {
    await sleep(2000);

    const res = await fetch("/api/triage/status");
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) {
      throw new Error(`Status check failed (${res.status})`);
    }
    const progress = await res.json();

    if (progress.first_chunk_done) {
      setSearchEnabled(true);
    }

    if (progress.status === "done") {
      renderProgress({ percent: 100 });
      renderCounts(progress.counts || {});
      statusEl.textContent = "Done.";
      return;
    }
    if (progress.status === "cancelled") {
      renderProgress(progress);
      renderCounts(progress.counts || {});
      statusEl.textContent = "Triage stopped.";
      return;
    }
    if (progress.status === "error") {
      statusEl.textContent = `Triage failed: ${progress.error || "unknown error"}`;
      hideProgress();
      return;
    }

    renderProgress(progress);
  }
}

async function startWatchLoop() {
  // Lightweight background watcher (separate from the manual pollUntilDone):
  // picks up results if a *scheduled* run fires while the page is open, and
  // notices if the schedule got cleared (fired or cancelled elsewhere).
  if (pollActive) return;
  pollActive = true;

  while (pollActive) {
    await sleep(5000);

    try {
      const res = await fetch("/api/triage/status");
      if (res.status === 401) {
        pollActive = false;
        showConnect();
        return;
      }
      if (res.ok) {
        const progress = await res.json();
        if (progress.first_chunk_done) {
          setSearchEnabled(true);
        }
        if (progress.status === "running") {
          renderProgress(progress);
        } else if (progress.status === "done" && progress.counts) {
          renderProgress({ percent: 100 });
          renderCounts(progress.counts);
        } else if (progress.status === "cancelled" && progress.counts) {
          renderCounts(progress.counts);
        }
      }
    } catch (err) {
      // Ignore transient errors and keep watching.
    }

    await refreshScheduleState();
  }
}

function renderCounts(counts) {
  const drafted = counts[DRAFTED_KEY];
  renderDraftedStat(drafted);

  // The "FAQ (drafted)" value is shown as a separate highlighted stat, not as
  // a category bar.
  const entries = Object.entries(counts).filter(([key]) => key !== DRAFTED_KEY);

  if (entries.length === 0) {
    resultsEl.innerHTML = '<p class="muted">No emails were labeled.</p>';
    resultsCard.classList.remove("hidden");
    return;
  }

  // Order known categories first, then any extras alphabetically.
  entries.sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a[0]);
    const ib = CATEGORY_ORDER.indexOf(b[0]);
    const ra = ia === -1 ? CATEGORY_ORDER.length : ia;
    const rb = ib === -1 ? CATEGORY_ORDER.length : ib;
    return ra - rb || a[0].localeCompare(b[0]);
  });

  const max = Math.max(...entries.map(([, n]) => n));

  resultsEl.innerHTML = entries
    .map(([category, count]) => {
      const pct = max > 0 ? (count / max) * 100 : 0;
      return `
        <div class="row">
          <span class="label">${escapeHtml(category)}</span>
          <span class="track"><span class="fill" style="width:${pct}%"></span></span>
          <span class="count">${count}</span>
        </div>`;
    })
    .join("");

  resultsCard.classList.remove("hidden");
}

function renderDraftedStat(drafted) {
  if (drafted === undefined || drafted === null) {
    draftedStatEl.classList.add("hidden");
    draftedStatEl.innerHTML = "";
    return;
  }

  const noun = drafted === 1 ? "reply" : "replies";
  draftedStatEl.innerHTML = `
    <div class="stat-value">${drafted} FAQ ${noun} drafted</div>
    <div class="stat-note">Review and send these from your Gmail Drafts.</div>`;
  draftedStatEl.classList.remove("hidden");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c];
  });
}

// --- Semantic search ---

async function runSearch() {
  const query = searchInput.value.trim();
  if (!query) {
    searchResultsEl.innerHTML =
      '<p class="muted">Type something to search for.</p>';
    return;
  }

  searchResultsEl.innerHTML = '<p class="muted">Searching\u2026</p>';

  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) {
      throw new Error(`Server responded ${res.status}`);
    }
    const data = await res.json();
    renderSearchResults(data.results || []);
  } catch (err) {
    searchResultsEl.innerHTML = `<p class="muted">Search failed: ${escapeHtml(
      err.message
    )}</p>`;
  }
}

function renderSearchResults(results) {
  if (!results.length) {
    searchResultsEl.innerHTML =
      '<p class="muted">No matching emails yet.</p>';
    return;
  }

  searchResultsEl.innerHTML = results
    .map((r) => {
      const subject = escapeHtml(r.subject || "(no subject)");
      const sender = escapeHtml(r.sender || "");
      const snippet = escapeHtml(r.snippet || "");
      const url = escapeHtml(r.gmail_url || "#");
      return `
        <a class="search-result" href="${url}" target="_blank" rel="noopener">
          <span class="search-subject">${subject}</span>
          <span class="search-sender">${sender}</span>
          <span class="search-snippet">${snippet}</span>
        </a>`;
    })
    .join("");
}

// --- Decorative animated background tree (brand watermark) ---
// Ported from a React/Framer-Motion reference to plain DOM/SVG. Purely visual:
// generated once on load and injected into #tree-bg. Unrelated to any triage,
// search, category, or scheduling logic.

function injectTreeStyles() {
  if (document.getElementById("tree-bg-styles")) return;
  const style = document.createElement("style");
  style.id = "tree-bg-styles";
  style.textContent = `
    .tree-bg-svg { width: 100%; height: 100%; display: block; }
    .tree-node {
      transform-box: fill-box;
      transform-origin: center;
      opacity: 0;
    }
    @keyframes treePulse {
      0% { transform: scale(1); opacity: 0.6; }
      50% { transform: scale(1.3); opacity: 1; }
      100% { transform: scale(1); opacity: 0.6; }
    }
  `;
  document.head.appendChild(style);
}

function buildNeuralTree() {
  const container = document.getElementById("tree-bg");
  if (!container || container.dataset.rendered === "true") return;
  container.dataset.rendered = "true";

  injectTreeStyles();

  const SVG_NS = "http://www.w3.org/2000/svg";
  const branchPaths = [];
  const branchNodes = [];

  // Recursively grow branches. depth counts down from 6 (trunk) to 0 (tips).
  function generateBranch(x, y, angle, length, depth, startTime) {
    // Per-depth timing, matching the reference's faster base + tight increment.
    const duration = 0.3 + depth * 0.05;
    const endTime = startTime + duration * 0.8; // overlap slightly

    const x2 = x + length * Math.cos((angle * Math.PI) / 180);
    const y2 = y - length * Math.sin((angle * Math.PI) / 180);

    // Randomized control point gives each branch an organic curve.
    const cx = (x + x2) / 2 + (Math.random() - 0.5) * length * 0.4;
    const cy = (y + y2) / 2 + (Math.random() - 0.5) * length * 0.2;

    branchPaths.push({
      d: `M${x} ${y} Q${cx} ${cy} ${x2} ${y2}`,
      color: depth % 2 === 0 ? "#0b182f" : "#415b3e",
      width: Math.max(0.5, depth * 0.8),
      delay: startTime,
      duration: duration,
    });

    // A node sits at every joint except the very base (the depth-6 start).
    if (depth !== 6) {
      branchNodes.push({
        x: x2,
        y: y2,
        color: Math.random() > 0.6 ? "#415b3e" : "#0b182f",
        size: 3 + Math.random() * 3,
        delay: endTime,
      });
    }

    if (depth > 0) {
      const numBranches = 2;
      const branchAngleSpan = 50 + Math.random() * 30; // 50-80 degrees
      for (let i = 0; i < numBranches; i++) {
        const newAngle =
          angle -
          branchAngleSpan / 2 +
          (branchAngleSpan / (numBranches - 1)) * i +
          (Math.random() - 0.5) * 15;
        generateBranch(x2, y2, newAngle, length * 0.75, depth - 1, endTime);
      }
    }
  }

  // Start lower-left of center and lean to the upper-right so the canopy fans
  // toward the top-right corner, matching the RootStock site layout (tree offset
  // to the side rather than dead center, where the dashboard would cover it).
  generateBranch(780, 920, 60, 195, 6, 0);

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 1440 900");
  svg.setAttribute("preserveAspectRatio", "xMaxYMax slice");
  svg.setAttribute("class", "tree-bg-svg");

  const drawIns = [];
  branchPaths.forEach((p) => {
    const el = document.createElementNS(SVG_NS, "path");
    el.setAttribute("d", p.d);
    el.setAttribute("fill", "none");
    el.setAttribute("stroke", p.color);
    el.setAttribute("stroke-width", String(p.width));
    el.setAttribute("stroke-linecap", "round");
    el.style.opacity = "0";
    svg.appendChild(el);
    drawIns.push({ el, p });
  });

  branchNodes.forEach((nd) => {
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", String(nd.x));
    c.setAttribute("cy", String(nd.y));
    c.setAttribute("r", String(nd.size));
    c.setAttribute("fill", nd.color);
    c.setAttribute("class", "tree-node");
    // Randomized duration and delay so nodes don't all pulse in sync; the
    // delay also staggers when each node first appears.
    const dur = 2 + Math.random() * 2;
    const delay = nd.delay + Math.random();
    c.style.animation = `treePulse ${dur.toFixed(2)}s ease-in-out ${delay.toFixed(
      2
    )}s infinite`;
    svg.appendChild(c);
  });

  container.appendChild(svg);

  // Prepare each path's stroke-dasharray draw-in, staggered by depth via delay.
  drawIns.forEach(({ el, p }) => {
    const len = el.getTotalLength();
    el.style.strokeDasharray = String(len);
    el.style.strokeDashoffset = String(len);
    el.style.transition =
      `stroke-dashoffset ${p.duration}s linear ${p.delay}s, ` +
      `opacity ${p.duration}s linear ${p.delay}s`;
  });

  // Commit the undrawn state (force a reflow), then flip to the drawn state so
  // the CSS transitions actually animate.
  void svg.getBoundingClientRect();
  drawIns.forEach(({ el }) => {
    el.style.strokeDashoffset = "0";
    el.style.opacity = "0.6";
  });
}

runBtn.addEventListener("click", onRunClick);
scheduleBtn.addEventListener("click", onScheduleClick);
signoutBtn.addEventListener("click", signOut);
stopTriageBtn.addEventListener("click", cancelTriage);
cancelScheduleLink.addEventListener("click", cancelSchedule);
addCategoryBtn.addEventListener("click", addCategory);
saveCategoriesBtn.addEventListener("click", saveCategories);
searchBtn.addEventListener("click", runSearch);
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch();
});
searchInput.addEventListener("input", () => {
  // Clearing the query should clear the results too.
  if (searchInput.value.trim() === "") {
    searchResultsEl.innerHTML = "";
  }
});
init();
