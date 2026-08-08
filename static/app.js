"use strict";

const authCard = document.getElementById("auth-card");
const triageCard = document.getElementById("triage-card");
const runBtn = document.getElementById("run-btn");
const statusEl = document.getElementById("status");
const triageProgress = document.getElementById("triage-progress");
const triageProgressFill = document.getElementById("triage-progress-fill");
const triageProgressLabel = document.getElementById("triage-progress-label");
const stopTriageBtn = document.getElementById("stop-triage-btn");
const userBox = document.getElementById("user-box");
const userEmailEl = document.getElementById("user-email");
const signoutBtn = document.getElementById("signout-btn");
const sortRange = document.getElementById("sort-range");
const runDateInput = document.getElementById("run-date");
const runRangeField = document.getElementById("run-range-field");
const runHint = document.querySelector(".run-hint");
const scheduleDateInput = document.getElementById("schedule-date");
const scheduleHour = document.getElementById("schedule-hour");
const scheduleMinute = document.getElementById("schedule-minute");
const scheduleAmpm = document.getElementById("schedule-ampm");
const scheduleBtn = document.getElementById("schedule-btn");
const scheduledLine = document.getElementById("scheduled-line");
const scheduledTextEl = document.getElementById("scheduled-text");
const autoIntervalEl = document.getElementById("auto-interval");
const autoStatusEl = document.getElementById("auto-status");
const cancelScheduleLink = document.getElementById("cancel-schedule");
const openGmailLink = document.getElementById("open-gmail-link");
const openDraftsLink = document.getElementById("open-drafts-link");
const linksCard = document.getElementById("links-card");
const labelsCard = document.getElementById("labels-card");
const labelsList = document.getElementById("labels-list");
const labelsHow = document.getElementById("labels-how");
const categoriesCard = document.getElementById("categories-card");
const categoryListEl = document.getElementById("category-list");
const addCategoryBtn = document.getElementById("add-category-btn");
const faqSelect = document.getElementById("faq-select");
const saveCategoriesBtn = document.getElementById("save-categories-btn");
const categoriesStatusEl = document.getElementById("categories-status");
const learnedRulesCard = document.getElementById("learned-rules-card");
const learnedRulesListEl = document.getElementById("learned-rules-list");
const learnedRulesEmptyEl = document.getElementById("learned-rules-empty");
const searchCard = document.getElementById("search-card");
const searchInput = document.getElementById("search-input");
const searchBtn = document.getElementById("search-btn");
const searchResultsEl = document.getElementById("search-results");
const searchLockMsg = document.getElementById("search-lock-msg");
const undoRow = document.getElementById("undo-row");
const undoBtn = document.getElementById("undo-btn");
const undoStatusEl = document.getElementById("undo-status");
const digestCard = document.getElementById("digest-card");
const digestBody = document.getElementById("digest-body");
const commandCard = document.getElementById("command-card");
const commandInput = document.getElementById("command-input");
const commandPreviewBtn = document.getElementById("command-preview-btn");
const commandPreviewEl = document.getElementById("command-preview");
const commandActionsEl = document.getElementById("command-actions");
const commandRunBtn = document.getElementById("command-run-btn");
const commandCancelBtn = document.getElementById("command-cancel-btn");
const commandStatusEl = document.getElementById("command-status");
const priorityCard = document.getElementById("priority-card");
const priorityListEl = document.getElementById("priority-list");
const priorityEmptyEl = document.getElementById("priority-empty");

const DRAFTED_KEY = "FAQ (drafted)";

// This category is always kept and cannot be removed or renamed in the UI --
// it is the catch-all for emails that don't fit any other category.
const FIXED_CATEGORY = "Others";

// The user's working copy of their category list (edited in the Categories
// card, saved via POST /api/settings/categories).
let currentCategories = [];

// Parallel to currentCategories: the per-category prompt/description (may be an
// empty string) describing what belongs in each category.
let currentPrompts = [];

// Fixed category order so the dashboard layout stays stable between runs.
const CATEGORY_ORDER = [
  "Needs Action",
  "Red Flag",
  "FAQ",
  "Others",
  "Spam/Newsletter",
];

// True while a background poll loop (manual run or watching a schedule) is
// active. Only one loop runs at a time.
let pollActive = false;

// True while a one-time run is scheduled for later; manual runs are blocked and
// the Run button is replaced by the scheduled banner.
let isScheduled = false;

// True once the user has processed/fetched mail (embeddings exist); search stays
// locked until then.
let searchAvailable = false;

// While now < this timestamp, schedule-state polling is ignored so a just-issued
// manual cancel can't be flipped back by an in-flight/next poll.
let suppressScheduleRefreshUntil = 0;

async function init() {
  buildNeuralTree();

  if (new URLSearchParams(location.search).has("auth_error")) {
    const p = document.querySelector("#auth-card .muted");
    if (p) {
      p.textContent =
        "Sign in didn't complete. Click Connect Gmail and finish the Google consent in one go, in the same tab.";
      p.style.color = "#a8321f";
    }
  }

  try {
    const res = await fetch("/api/auth/status");
    if (res.status === 401) {
      showConnect();
      return;
    }
    const data = await res.json();

    if (data.authenticated) {
      showAuthenticated(data.email);
      searchAvailable = !!data.has_search_data;
      updateSearchLock();
      loadLabelGuide();
      await loadCategories();
      loadLearnedRules();
      loadLastSummary();
      loadPriority();
      loadDigest();
      loadUndoStatus();
      await refreshScheduleState();
      loadAutoTriage();
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
  learnedRulesCard.classList.add("hidden");
  searchCard.classList.add("hidden");
  linksCard.classList.add("hidden");
  labelsCard.classList.add("hidden");
  userBox.classList.add("hidden");
  if (digestCard) digestCard.classList.add("hidden");
  if (commandCard) commandCard.classList.add("hidden");
  if (priorityCard) priorityCard.classList.add("hidden");
}

function showAuthenticated(email) {
  authCard.classList.add("hidden");
  triageCard.classList.remove("hidden");
  categoriesCard.classList.remove("hidden");
  learnedRulesCard.classList.remove("hidden");
  searchCard.classList.remove("hidden");
  linksCard.classList.remove("hidden");
  labelsCard.classList.remove("hidden");
  userBox.classList.remove("hidden");
  if (digestCard) digestCard.classList.remove("hidden");
  if (commandCard) commandCard.classList.remove("hidden");
  if (priorityCard) priorityCard.classList.remove("hidden");
  userEmailEl.textContent = email || "";

  // Populate the schedule time picker and default the dates to today.
  initScheduleControls();
  if (!runDateInput.value) runDateInput.value = todayLocalDate();
  if (!scheduleDateInput.value) scheduleDateInput.value = todayLocalDate();

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
    await res.json();
  } catch (err) {
    // No prior summary is fine; ignore.
  }
}

async function loadLabelGuide() {
  try {
    const res = await fetch("/api/labels/guide");
    if (!res.ok) return;
    const data = await res.json();
    labelsHow.textContent = data.how || "";
    labelsList.innerHTML = (data.labels || [])
      .map(
        (l) => `
        <div class="label-guide-row">
          <span class="label-guide-name">${escapeHtml(l.name)}</span>
          <span class="label-guide-desc">${escapeHtml(l.description)}</span>
        </div>`
      )
      .join("");
  } catch (err) {
    // Non-critical; leave the guide as-is on failure.
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
    const prompts = data.category_prompts || {};
    currentPrompts = currentCategories.map((name) => prompts[name] || "");
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
  if (!has) {
    currentCategories.push(FIXED_CATEGORY);
    currentPrompts.push("");
  }
}

function renderCategoryList() {
  categoryListEl.innerHTML = "";

  currentCategories.forEach((cat, index) => {
    const row = document.createElement("div");
    row.className = "category-row";

    const isFixed =
      cat.trim().toLowerCase() === FIXED_CATEGORY.toLowerCase();

    // Top line: the category name plus its Remove button / "Always on" badge.
    const nameLine = document.createElement("div");
    nameLine.className = "category-name-line";

    const input = document.createElement("input");
    input.type = "text";
    input.className = "category-input";
    input.value = cat;
    if (isFixed) {
      // Fixed catch-all: can't be renamed or removed.
      input.readOnly = true;
      input.title =
        "Others is always kept as the catch-all category and can't be renamed or removed.";
    } else {
      input.addEventListener("input", () => {
        currentCategories[index] = input.value;
        rebuildFaqSelect();
      });
    }

    nameLine.appendChild(input);

    if (isFixed) {
      const badge = document.createElement("span");
      badge.className = "category-fixed muted";
      badge.textContent = "Always on";
      nameLine.appendChild(badge);
    } else {
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "btn btn-ghost category-remove";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", () => {
        currentCategories.splice(index, 1);
        currentPrompts.splice(index, 1);
        renderCategoryList();
        rebuildFaqSelect();
      });
      nameLine.appendChild(removeBtn);
    }

    row.appendChild(nameLine);

    // A short prompt describing what belongs in this category. Optional, but it
    // makes client/user-specific categories classify far more accurately.
    const prompt = document.createElement("textarea");
    prompt.className = "category-prompt";
    prompt.rows = 2;
    prompt.placeholder = isFixed
      ? "Catch-all for anything that doesn't fit the categories above."
      : "Describe what belongs in this category (optional)";
    prompt.value = currentPrompts[index] || "";
    prompt.addEventListener("input", () => {
      currentPrompts[index] = prompt.value;
    });
    row.appendChild(prompt);

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
  currentPrompts.push("");
  renderCategoryList();
  rebuildFaqSelect();

  const inputs = categoryListEl.querySelectorAll(".category-input");
  if (inputs.length) inputs[inputs.length - 1].focus();
}

async function saveCategories() {
  const rows = categoryListEl.querySelectorAll(".category-row");
  const cleaned = [];
  const prompts = {};
  rows.forEach((row) => {
    const nameEl = row.querySelector(".category-input");
    const promptEl = row.querySelector(".category-prompt");
    const name = nameEl ? nameEl.value.trim() : "";
    if (!name) return;
    cleaned.push(name);
    const prompt = promptEl ? promptEl.value.trim() : "";
    if (prompt) prompts[name] = prompt;
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
      body: JSON.stringify({
        categories: cleaned,
        faq_category: faqCategory,
        category_prompts: prompts,
      }),
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
    const savedPrompts = data.category_prompts || prompts;
    currentPrompts = currentCategories.map((name) => savedPrompts[name] || "");
    ensureFixedCategory();
    renderCategoryList();
    rebuildFaqSelect(data.faq_category || "");
    categoriesStatusEl.textContent = "Saved.";
    loadLabelGuide();
  } catch (err) {
    categoriesStatusEl.textContent = `Could not save: ${err.message}`;
  } finally {
    saveCategoriesBtn.disabled = false;
  }
}

// --- Learned rules (self-learning) ---

async function loadLearnedRules() {
  try {
    const res = await fetch("/api/rules/learned");
    if (!res.ok) return;
    const data = await res.json();
    renderLearnedRules(data.rules || []);
  } catch (err) {
    // Ignore; the section just stays empty.
  }
}

function renderLearnedRules(rules) {
  learnedRulesListEl.innerHTML = "";
  if (!rules.length) {
    learnedRulesEmptyEl.classList.remove("hidden");
    return;
  }
  learnedRulesEmptyEl.classList.add("hidden");

  rules.forEach((rule) => {
    const row = document.createElement("div");
    row.className = "learned-rule-row" + (rule.active ? " active" : "");

    const info = document.createElement("div");
    info.className = "learned-rule-info";
    const typeLabel = rule.match_type === "domain" ? "domain" : "sender";
    info.innerHTML =
      `<span class="learned-rule-match">${escapeHtml(rule.match_value)}</span>` +
      `<span class="muted"> (${typeLabel}) &rarr; </span>` +
      `<span class="learned-rule-cat">${escapeHtml(rule.category)}</span>` +
      `<span class="muted learned-rule-meta"> · seen ${rule.hits}× · ${
        rule.active ? "auto-sorting" : "learning"
      }</span>`;
    row.appendChild(info);

    const actions = document.createElement("div");
    actions.className = "learned-rule-actions";

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-ghost";
    toggleBtn.textContent = rule.active ? "Turn off" : "Turn on";
    toggleBtn.addEventListener("click", () =>
      updateLearnedRule(rule, rule.active ? "disable" : "enable")
    );
    actions.appendChild(toggleBtn);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn btn-ghost";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => updateLearnedRule(rule, "delete"));
    actions.appendChild(removeBtn);

    row.appendChild(actions);
    learnedRulesListEl.appendChild(row);
  });
}

async function updateLearnedRule(rule, action) {
  try {
    const res = await fetch("/api/rules/learned", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        match_type: rule.match_type,
        match_value: rule.match_value,
        category: rule.category,
        action,
      }),
    });
    if (res.status === 401) {
      showConnect();
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    renderLearnedRules(data.rules || []);
  } catch (err) {
    // Ignore transient errors.
  }
}

// --- Priority inbox ---

async function loadPriority() {
  try {
    const res = await fetch("/api/priority");
    if (!res.ok) return;
    const data = await res.json();
    renderPriority(data.items || []);
  } catch (err) {
    // Non-critical.
  }
}

function renderPriority(items) {
  if (!priorityListEl) return;
  if (!items.length) {
    priorityListEl.innerHTML = "";
    if (priorityEmptyEl) priorityEmptyEl.classList.remove("hidden");
    return;
  }
  if (priorityEmptyEl) priorityEmptyEl.classList.add("hidden");
  priorityListEl.innerHTML = items
    .map((it) => {
      const subject = escapeHtml(it.subject || "(no subject)");
      const sender = escapeHtml(it.sender || "");
      const reason = escapeHtml(it.reason || "");
      const category = escapeHtml(it.category || "");
      const url = escapeHtml(it.gmail_url || "#");
      const score = Math.max(0, Math.min(100, Number(it.score) || 0));
      const senderAttr = escapeHtml(it.sender || "");
      const subjectAttr = escapeHtml(it.subject || "");
      const categoryAttr = escapeHtml(it.category || "");
      const options = currentCategories
        .filter((c) => c)
        .map((c) => {
          const name = escapeHtml(c);
          const selected = c === (it.category || "") ? " selected" : "";
          return `<option value="${name}"${selected}>${name}</option>`;
        })
        .join("");
      return `
        <div class="priority-row" data-sender="${senderAttr}" data-subject="${subjectAttr}" data-category="${categoryAttr}">
          <span class="priority-score" title="Priority score">${score}</span>
          <span class="priority-main">
            <a class="priority-subject" href="${url}" target="_blank" rel="noopener">${subject}</a>
            <span class="priority-sender">${sender}</span>
            <span class="priority-reason">${category} &middot; ${reason}</span>
          </span>
          <select class="priority-relabel" title="Move this sender to a different category">
            ${options}
          </select>
        </div>`;
    })
    .join("");
}

// Relabeling a priority row teaches the backend: the sender is forced into the
// chosen category (POST /api/feedback), so future mail from them sorts that way.
async function relabelPrioritySender(sender, subject, oldCategory, category) {
  if (!sender || !category) return;
  try {
    await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sender, subject, old_category: oldCategory, category }),
    });
    loadLearnedRules();
  } catch (err) {
    // Non-critical: the correction just won't be recorded this time.
  }
}

if (priorityListEl) {
  priorityListEl.addEventListener("change", (event) => {
    const select = event.target;
    if (!select.classList || !select.classList.contains("priority-relabel")) return;
    const row = select.closest(".priority-row");
    const sender = row ? row.getAttribute("data-sender") : "";
    const subject = row ? row.getAttribute("data-subject") : "";
    const oldCategory = row ? row.getAttribute("data-category") : "";
    relabelPrioritySender(sender, subject, oldCategory, select.value);
    if (row) row.setAttribute("data-category", select.value);
  });
}

// --- Daily digest ---

async function loadDigest() {
  try {
    const res = await fetch("/api/digest");
    if (!res.ok) return;
    const data = await res.json();
    renderDigest(data);
  } catch (err) {
    // Non-critical.
  }
}

function renderDigest(data) {
  if (!digestBody) return;
  const counts = data.counts || null;
  let html = "";
  if (counts) {
    const entries = Object.entries(counts).filter(([k]) => k !== DRAFTED_KEY);
    const total = entries.reduce((sum, [, n]) => sum + Number(n || 0), 0);
    html += `<div class="digest-line"><strong>${total}</strong> emails sorted in your last run.</div>`;
    if (entries.length) {
      html +=
        '<div class="digest-chips">' +
        entries
          .map(
            ([k, n]) =>
              `<span class="digest-chip">${escapeHtml(k)}: ${Number(n)}</span>`
          )
          .join("") +
        "</div>";
    }
    const drafted = counts[DRAFTED_KEY];
    if (drafted !== undefined && drafted !== null) {
      html += `<div class="digest-line muted">${Number(drafted)} FAQ ${
        Number(drafted) === 1 ? "reply" : "replies"
      } drafted (check your Gmail Drafts).</div>`;
    }
  } else {
    html +=
      '<div class="digest-line muted">No run yet. Run Triage to see your digest.</div>';
  }
  html += `<div class="digest-line muted">${Number(
    data.learned_active || 0
  )} of ${Number(data.learned_total || 0)} learned rules are auto-sorting.</div>`;
  digestBody.innerHTML = html;
}

// --- Undo last run ---

async function loadUndoStatus() {
  if (!undoRow) return;
  try {
    const res = await fetch("/api/undo");
    if (!res.ok) return;
    const data = await res.json();
    if (data.run && data.run.action_count) {
      undoRow.classList.remove("hidden");
      undoStatusEl.textContent = `${data.run.action_count} emails can be reverted`;
    } else {
      undoRow.classList.add("hidden");
      undoStatusEl.textContent = "";
    }
  } catch (err) {
    // Non-critical.
  }
}

async function undoLastRun() {
  undoBtn.disabled = true;
  undoStatusEl.textContent = "Reverting\u2026";
  try {
    const res = await fetch("/api/undo", { method: "POST" });
    if (res.status === 401) {
      showConnect();
      return;
    }
    const data = await res.json();
    if (data.status === "undone") {
      undoStatusEl.textContent = `Reverted ${data.count} emails.`;
      undoRow.classList.add("hidden");
      loadPriority();
      loadDigest();
    } else {
      undoStatusEl.textContent = "Nothing to undo.";
      undoRow.classList.add("hidden");
    }
  } catch (err) {
    undoStatusEl.textContent = "Could not undo. Try again.";
  } finally {
    undoBtn.disabled = false;
  }
}

// --- Natural-language commands ---

let lastCommandText = "";

async function previewCommand() {
  const text = commandInput.value.trim();
  if (!text) {
    commandStatusEl.textContent = "";
    commandPreviewEl.classList.add("hidden");
    commandActionsEl.classList.add("hidden");
    return;
  }
  lastCommandText = text;
  commandPreviewBtn.disabled = true;
  commandStatusEl.textContent = "";
  commandPreviewEl.classList.remove("hidden");
  commandPreviewEl.innerHTML =
    '<span class="muted">Reading your request\u2026</span>';
  commandActionsEl.classList.add("hidden");
  try {
    const res = await fetch("/api/command/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) {
      showConnect();
      return;
    }
    const data = await res.json();
    if (!res.ok || data.error) {
      commandPreviewEl.innerHTML = `<span class="muted">${escapeHtml(
        data.error || "Could not understand that."
      )}</span>`;
      return;
    }
    const samples = (data.samples || [])
      .map(
        (s) =>
          `<div class="command-sample"><span class="command-sample-subject">${escapeHtml(
            s.subject || "(no subject)"
          )}</span> <span class="muted">${escapeHtml(s.sender || "")}</span></div>`
      )
      .join("");
    commandPreviewEl.innerHTML =
      `<div class="command-summary">${escapeHtml(data.summary || "")}</div>` +
      `<div class="command-count"><strong>${Number(
        data.count || 0
      )}</strong> emails match.</div>` +
      samples;
    if (Number(data.count || 0) > 0) {
      commandActionsEl.classList.remove("hidden");
    } else {
      commandActionsEl.classList.add("hidden");
    }
  } catch (err) {
    commandPreviewEl.innerHTML =
      '<span class="muted">Something went wrong. Try again.</span>';
  } finally {
    commandPreviewBtn.disabled = false;
  }
}

async function executeCommand() {
  if (!lastCommandText) return;
  commandRunBtn.disabled = true;
  commandStatusEl.textContent = "Working\u2026";
  try {
    const res = await fetch("/api/command/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (res.status === 401) {
      showConnect();
      return;
    }
    const data = await res.json();
    if (!res.ok || data.error) {
      commandStatusEl.textContent = data.error || "Could not complete.";
      return;
    }
    commandStatusEl.textContent = `Done. ${Number(
      data.affected || 0
    )} emails updated.`;
    commandActionsEl.classList.add("hidden");
    commandPreviewEl.classList.add("hidden");
    commandInput.value = "";
    lastCommandText = "";
  } catch (err) {
    commandStatusEl.textContent = "Something went wrong. Try again.";
  } finally {
    commandRunBtn.disabled = false;
  }
}

function cancelCommand() {
  commandPreviewEl.classList.add("hidden");
  commandActionsEl.classList.add("hidden");
  commandStatusEl.textContent = "";
  lastCommandText = "";
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
  runRangeField.classList.add("hidden");
  if (runHint) runHint.classList.add("hidden");
  setScheduleInputsDisabled(true);
}

function hideScheduled() {
  isScheduled = false;
  scheduledLine.classList.add("hidden");
  runBtn.classList.remove("hidden");
  runRangeField.classList.remove("hidden");
  if (runHint) runHint.classList.remove("hidden");
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
  if (Date.now() < suppressScheduleRefreshUntil) return;
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

async function loadAutoTriage() {
  try {
    const res = await fetch("/api/triage/auto");
    if (!res.ok) return;
    const data = await res.json();
    autoIntervalEl.value = data.interval_minutes ? String(data.interval_minutes) : "";
    autoStatusEl.textContent = data.interval_minutes
      ? `On, every ${data.interval_minutes} min`
      : "";
  } catch (err) {
    // Ignore; the control just stays at its default.
  }
}

async function saveAutoTriage() {
  const value = autoIntervalEl.value;
  autoStatusEl.textContent = "Saving…";
  try {
    const res = await fetch("/api/triage/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interval_minutes: value ? parseInt(value, 10) : null }),
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
    autoStatusEl.textContent = data.interval_minutes
      ? `On, every ${data.interval_minutes} min`
      : "Off";
  } catch (err) {
    autoStatusEl.textContent = `Could not save: ${err.message}`;
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
  await runTriage(sortRange.value, runDateInput.value);
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
await scheduleRun(localValue, sortRange.value);
}

async function scheduleRun(localDateTimeValue, range) {
  scheduleBtn.disabled = true;
  statusEl.textContent = "Scheduling\u2026";

  try {
    const runAtIso = new Date(localDateTimeValue).toISOString();
    const res = await fetch("/api/triage/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_at: runAtIso, range: range || "1d" }),
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
  // Update the UI right away and suppress polls so the banner cannot flip back
  // before the server has removed the scheduled job.
  suppressScheduleRefreshUntil = Date.now() + 6000;
  hideScheduled();
  scheduleDateInput.value = todayLocalDate();
  try {
    await fetch("/api/triage/schedule/cancel", { method: "POST" });
  } catch (err) {
    // The suppression window expires and the next poll reflects the real state.
  }
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
  sortRange.disabled = isBusy;
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

function showCompletedBriefly() {
  renderProgress({ percent: 100 });
  window.setTimeout(hideProgress, 3000);
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

async function runTriage(range, date) {
  setBusy(true);
  statusEl.textContent = "";
  showProgressStarting();

  try {
    const res = await fetch("/api/triage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ range: range || "1d", date: date || null }),
    });
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
      unlockSearch();
    }

    if (progress.status === "done") {
      showCompletedBriefly();
      statusEl.textContent = "Done.";
      loadLearnedRules();
      loadPriority();
      loadDigest();
      loadUndoStatus();
      return;
    }
    if (progress.status === "cancelled") {
      hideProgress();
      statusEl.textContent = "Triage stopped.";
      loadDigest();
      loadPriority();
      loadUndoStatus();
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
  let lastStatus = null;

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
          unlockSearch();
        }
        const initialStatus = lastStatus === null;
        const changed = progress.status !== lastStatus;
        if (progress.status === "running") {
          renderProgress(progress);
        } else if (
          progress.status === "done" &&
          progress.counts &&
          changed &&
          !initialStatus
        ) {
          // Only refresh the cards when a run just finished, not every tick.
          showCompletedBriefly();
          loadDigest();
          loadPriority();
          loadUndoStatus();
        } else if (progress.status === "cancelled" && progress.counts && changed) {
          hideProgress();
          loadDigest();
        } else if (initialStatus) {
          hideProgress();
        }
        lastStatus = progress.status;
      }
    } catch (err) {
      // Ignore transient errors and keep watching.
    }

    await refreshScheduleState();
  }
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

function updateSearchLock() {
  const locked = !searchAvailable;
  searchInput.classList.toggle("locked", locked);
  searchInput.readOnly = locked;
  if (!locked) searchLockMsg.classList.add("hidden");
}

function showSearchLock() {
  searchLockMsg.classList.remove("hidden");
}

function unlockSearch() {
  searchAvailable = true;
  updateSearchLock();
}

async function runSearch() {
  if (!searchAvailable) {
    showSearchLock();
    return;
  }
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
        size: 2 + Math.random() * 2.2,
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

  // Grow straight up (angle 90) from the right side, anchored to the right edge
  // so the tree stays upright and offset to the right rather than dead center.
  generateBranch(1080, 930, 90, 148, 6, 0);

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
autoIntervalEl.addEventListener("change", saveAutoTriage);
signoutBtn.addEventListener("click", signOut);
stopTriageBtn.addEventListener("click", cancelTriage);
cancelScheduleLink.addEventListener("click", cancelSchedule);
addCategoryBtn.addEventListener("click", addCategory);
saveCategoriesBtn.addEventListener("click", saveCategories);
searchBtn.addEventListener("click", runSearch);
if (undoBtn) undoBtn.addEventListener("click", undoLastRun);
if (commandPreviewBtn) commandPreviewBtn.addEventListener("click", previewCommand);
if (commandRunBtn) commandRunBtn.addEventListener("click", executeCommand);
if (commandCancelBtn) commandCancelBtn.addEventListener("click", cancelCommand);
if (commandInput)
  commandInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") previewCommand();
  });
searchInput.addEventListener("click", () => {
  if (!searchAvailable) showSearchLock();
});
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch();
});
searchInput.addEventListener("input", () => {
  // Clearing the query should clear the results too.
  if (searchInput.value.trim() === "") {
    searchResultsEl.innerHTML = "";
  }
});
document.addEventListener("click", (event) => {
  // Dismiss the search lock message when clicking away from the search box.
  if (searchLockMsg.classList.contains("hidden")) return;
  if (event.target === searchInput || event.target === searchBtn) return;
  searchLockMsg.classList.add("hidden");
});
init();
