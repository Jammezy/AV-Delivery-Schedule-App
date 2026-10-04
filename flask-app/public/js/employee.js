// ============================================================
// employee.js - WhenIsGood-style availability painting.
//
// state[key] holds 0 (can't), 1 (can work) or 2 (would prefer).
// Level 2 implies level 1 everywhere downstream, so the grid never
// needs a separate "preferred" layer.
// ============================================================

let CONFIG = null;
let CONTEXT = null;
let LOAD_REVISION = 0;
let EMPLOYEE = null;
let state = {};
let mode = 1;
let painting = false;
let paintedThisDrag = new Set();
let CONSENT_RECONFIRM = false;
let SUBMITTING = false;

function resetConsent(consent = {}) {
  $("allowExtraOpenings").checked = consent.allowExtraOpenings === true;
  $("allowExtraClosings").checked = consent.allowExtraClosings === true;
  CONSENT_RECONFIRM = consent.reconfirmationNeeded === true;
}

function updateConsent() {
  const context = CONTEXT?.boundaryContext;
  if (!context) return;
  const caps = context.caps;
  $("boundaryLimits").textContent = `Normal weekly limits: ${caps.openings} openings, ${caps.closings} closings, ${caps.combined} combined.`;
  $("boundaryExplanation").textContent = `Extra shifts are optional and are not guaranteed. Fairness comes first. Permission stays recorded when shift lengths change; qualifying blocks use the current minimum. Only fully preferred blocks can exceed normal limits. Each choice also permits that type of preferred shift to exceed the combined limit of ${caps.combined} opening and closing shifts.`;
  let total = 0;
  let showSection = false;
  for (const [kind, stem, field, required] of [["openings", "opening", "allowExtraOpenings", "opening"], ["closings", "closing", "allowExtraClosings", "closingRequired"]]) {
    const count = Object.entries(context.blocks).filter(([day, b]) => b[stem].length && b[required].length &&
      b[stem].every(h => state[cellKey(day, h)] === 2) && b[required].every(h => [1,2].includes(state[cellKey(day,h)]))).length;
    total += count;
    showSection ||= count > caps[kind];
    const box = $(field);
    box.disabled = !count && !box.checked; // A recorded choice can always be cleared.
    $(stem + "ConsentLabel").textContent = `I am okay with more than ${caps[kind]} ${stem} shifts per week, on days when I mark the full ${stem} block preferred.`;
    $(stem + "ConsentNote").textContent = count ? `You marked ${count} fully preferred ${kind}; the normal limit is ${caps[kind]}. These are possible assignments, not a guarantee.` :
      `${box.checked ? "Opted in; no" : "No"} qualifying preferred block currently. Mark every hour of a complete ${stem} block preferred, with enough available hours for a minimum shift.`;
  }
  // Visibility follows individual caps only. Keep choices independent of the grid.
  $("boundaryChoices").hidden = !showSection;
  $("boundaryStatus").textContent = [
    total > caps.combined ? `Your ${total} preferred boundary candidates exceed the combined limit of ${caps.combined}, even if neither individual limit is exceeded.` : "",
    !context.enabled ? "Your supervisor has not enabled additional preferred opening/closing shifts. Normal limits currently apply." : "",
    CONSENT_RECONFIRM ? "Reconfirmation needed after settings changed." : ""
  ].filter(Boolean).join(" ");
  const openings = $("allowExtraOpenings").checked;
  const closings = $("allowExtraClosings").checked;
  const hasConsent = openings || closings;
  $("boundaryConsentManagement").hidden = !(CONSENT_RECONFIRM || (!showSection && hasConsent));
  $("boundaryConsentSummary").textContent = [
    `Current choices: additional openings ${openings ? "opted in" : "not opted in"}; additional closings ${closings ? "opted in" : "not opted in"}.`,
    $("boundaryLimits").textContent,
    !context.enabled ? "Your supervisor has not enabled additional preferred opening/closing shifts. Normal limits currently apply." : "",
    CONSENT_RECONFIRM ? "Settings changed. Reconfirm these choices under the current limits, or clear consent. Submit availability to save." : "Submit availability to save any changes to these choices."
  ].filter(Boolean).join(" ");
  $("clearBoundaryConsent").hidden = !hasConsent;
  $("reconfirmConsent").hidden = !CONSENT_RECONFIRM;
  $("submitBtn").disabled = SUBMITTING || SAVED || CONSENT_RECONFIRM || !CSRF || !CONTEXT?.folder;
}

const $ = (id) => document.getElementById(id);

function cellKey(day, hour) {
  return `${day}_${String(hour).padStart(2, "0")}`;
}

function hourLabel(h) {
  const hh = h % 12 === 0 ? 12 : h % 12;
  return `${hh}${h < 12 ? "AM" : "PM"}`;
}

// hourEnd is the *start* of the last block, so the day runs to hourEnd + 1.
function blockLabel(h) {
  const a = h % 12 === 0 ? 12 : h % 12;
  const n = (h + 1) % 12 === 0 ? 12 : (h + 1) % 12;
  const am = h < 12, an = h + 1 < 12;
  return am === an ? `${a}–${n}${an ? "AM" : "PM"}` : `${a}${am ? "AM" : "PM"}–${n}${an ? "AM" : "PM"}`;
}

function closeHourFor(day) {
  if (day === "Sun") return 17;
  if (day === "Sat") return null;
  const perDay = CONFIG.dayCloseHours || {};
  if (perDay[day] !== undefined && perDay[day] !== null && perDay[day] !== "") return Number(perDay[day]);
  return null;
}

function isClosed(day, hour) {
  const ch = closeHourFor(day);
  return ch !== null && hour >= ch;
}

function hours() {
  const out = [];
  for (let h = CONFIG.hourStart; h <= CONFIG.hourEnd; h++) out.push(h);
  return out;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------- grid ----------------
function renderGrid() {
  const grid = $("grid");
  grid.style.setProperty("--cols", CONFIG.availabilityDays.length);
  const parts = ['<div class="wg-corner"></div>'];

  for (const day of CONFIG.availabilityDays) {
    parts.push(`<button type="button" class="wg-daylabel" data-fillday="${day}"
      title="Fill or clear ${day}">${escapeHtml(day)}</button>`);
  }

  for (const h of hours()) {
    parts.push(`<div class="wg-timelabel">${blockLabel(h)}</div>`);
    for (const day of CONFIG.availabilityDays) {
      const key = cellKey(day, h);
      const closed = isClosed(day, h);
      parts.push(
        `<button type="button" class="wg-cell" data-key="${key}" data-day="${day}"
           data-hour="${h}" data-level="${closed ? 0 : (state[key] || 0)}"
           ${closed ? 'data-closed="1" disabled' : ""}
           aria-pressed="${Boolean(state[key])}" aria-label="${day} ${blockLabel(h)}: ${state[key] === 2 ? "Preferred" : state[key] ? "Available" : "Unavailable"}"></button>`
      );
    }
  }
  grid.innerHTML = parts.join("");
  updateTally();
}

function paint(cell) {
  if (!cell || cell.dataset.closed === "1") return;
  const key = cell.dataset.key;
  if (paintedThisDrag.has(key)) return;
  paintedThisDrag.add(key);
  if (mode === 0) delete state[key];
  else state[key] = mode;
  cell.dataset.level = state[key] || 0;
  cell.setAttribute("aria-pressed", String(Boolean(state[key])));
  cell.setAttribute("aria-label", `${cell.dataset.day} ${blockLabel(Number(cell.dataset.hour))}: ${state[key] === 2 ? "Preferred" : state[key] ? "Available" : "Unavailable"}`);
  updateTally();
}

function bindGrid() {
  const grid = $("grid");

  grid.addEventListener("pointerdown", (e) => {
    const fill = e.target.closest("[data-fillday]");
    if (fill) { toggleDay(fill.dataset.fillday); return; }
    const cell = e.target.closest(".wg-cell");
    if (!cell) return;
    e.preventDefault();
    painting = true;
    paintedThisDrag = new Set();
    try { grid.setPointerCapture(e.pointerId); } catch (_) {}
    paint(cell);
  });

  grid.addEventListener("pointermove", (e) => {
    if (!painting) return;
    // Pointer capture routes every move to the grid, so hit-test manually.
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (el && el.classList && el.classList.contains("wg-cell")) paint(el);
  });

  grid.addEventListener("click", e => {
    if (e.detail !== 0) return; // Keyboard button activation.
    const fill = e.target.closest("[data-fillday]");
    if (fill) { toggleDay(fill.dataset.fillday); return; }
    paintedThisDrag = new Set(); paint(e.target.closest(".wg-cell"));
  });
  const stop = () => { painting = false; paintedThisDrag = new Set(); };
  grid.addEventListener("pointerup", stop);
  grid.addEventListener("pointercancel", stop);
  window.addEventListener("pointerup", stop);
}

function toggleDay(day) {
  const open = hours().filter((h) => !isClosed(day, h));
  const allSet = open.every((h) => (state[cellKey(day, h)] || 0) >= 1);
  for (const h of open) {
    const key = cellKey(day, h);
    if (allSet) delete state[key];
    else state[key] = Math.max(state[key] || 0, 1);
  }
  renderGrid();
}

function setMode(next) {
  mode = Number(next);
  document.querySelectorAll(".paint-mode").forEach((b) =>
    b.setAttribute("aria-pressed", String(Number(b.dataset.mode) === mode)));
}

// ---------------- feedback ----------------
function runsFor(day) {
  const out = [];
  let cur = 0;
  for (const h of hours()) {
    const ok = !isClosed(day, h) && (state[cellKey(day, h)] || 0) >= 1;
    if (ok) cur++;
    else { if (cur) out.push(cur); cur = 0; }
  }
  if (cur) out.push(cur);
  return out;
}

function updateTally() {
  updateConsent();
  const values = Object.values(state);
  const weekdayHours = Object.entries(state).filter(([k,v]) => CONFIG.days.includes(k.split("_")[0]) && v > 0).length;
  const avail = values.filter((v) => v >= 1).length;
  const pref = values.filter((v) => v === 2).length;
  $("tallyAvail").textContent = avail;
  $("tallyPref").textContent = pref;

  let target = "";
  if (EMPLOYEE && EMPLOYEE.minHours > 0) {
    target = weekdayHours >= EMPLOYEE.minHours
      ? `Your weekly minimum is ${EMPLOYEE.minHours} hrs — you're covered.`
      : `Your weekly minimum is ${EMPLOYEE.minHours} hrs. Mark at least ${EMPLOYEE.minHours - weekdayHours} more.`;
  }
  $("tallyTarget").textContent = target;

  const min = CONFIG.minShiftLength;
  const notes = [];
  for (const day of CONFIG.days) {
    const runs = runsFor(day);
    if (!runs.length) continue;
    if (Math.max(...runs) < min) {
      notes.push(`${day}: your longest open stretch is ${Math.max(...runs)} hr` +
        `${Math.max(...runs) === 1 ? "" : "s"}, but shifts are at least ${min} hours, ` +
        `so nothing can be scheduled that day.`);
    }
  }
  $("dayNotes").innerHTML = notes.length
    ? `<div class="msg warn">${notes.map(escapeHtml).join("<br>")}</div>` : "";
}

function showMsg(text, type) {
  $("msgArea").innerHTML = `<div class="msg ${type}">${escapeHtml(text)}</div>`;
  $("msgArea").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------------- data ----------------
let CSRF = null, REQUEST_ID = null, LAST_PAYLOAD = null, SAVED = false;

function updateCollectionDetails() {
  $("collectionDestination").textContent = CONTEXT?.unlocked ? `Submitting to scheduling folder: ${CONTEXT.folder?.name || "Folder closed"}` : "";
  $("codeResponseAllowance").textContent = CONTEXT?.unlocked && CSRF && Number.isInteger(CONTEXT.responseLimit)
    ? `No account needed. This code allows up to ${CONTEXT.responseLimit} responses total, shared by everyone using it.` : "";
}

function useContext(next) {
  CONTEXT = next; CONFIG = next.config; CSRF = next.csrf || null;
  $("availabilityForm").hidden = !next.unlocked;
  $("codeGate").hidden = !!next.unlocked;
  updateCollectionDetails();
  if (next.edit) {
    $("nameInput").value = next.edit.name; $("nameInput").readOnly = true;
    state = next.edit.availability || {}; $("commentInput").value = next.edit.comment || "";
    resetConsent(next.edit.consent); updateCommentCount();
  }
  renderGrid();
}
async function unlock(editToken = null) {
  $("unlockBtn").disabled = true;
  try {
    const res = await fetch("/api/collection/unlock", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(editToken ? {editToken} : {code:$("collectionCode").value})});
    const data = await res.json();
    if (!res.ok) { $("codeGateMsg").textContent = data.error || "Could not open the form."; return; }
    $("collectionCode").value = ""; SAVED = false; useContext(data); $("nameInput").focus();
  } catch (_) { $("codeGateMsg").textContent = "Connection failed. Please retry."; }
  finally { $("unlockBtn").disabled = false; }
}

async function submitAvailability() {
  if (SUBMITTING || CONSENT_RECONFIRM || SAVED || !CSRF) return;
  const name = $("nameInput").value.trim();
  if (!name) return showMsg("Enter your name before submitting.", "err");
  const avail = Object.values(state).filter((v) => v >= 1).length;
  if (!CONTEXT?.folder) return showMsg("No folder is accepting submissions.", "err");
  if ([...$("commentInput").value].length > 99) return showMsg("Comments must be shorter than 100 characters.", "err");

  SUBMITTING = true;
  $("submitBtn").disabled = true;
  try {
    if (!await refreshContext()) return showMsg($("codeGateMsg").textContent, "err");
    if (CONSENT_RECONFIRM) return showMsg("Settings changed. Reconfirm your consent choices before submitting.", "err");
    const payload = {name, availability:state, comment:$("commentInput").value, folderId:CONTEXT.folder.id, revision:CONTEXT.revision,
      allowExtraOpenings:$("allowExtraOpenings").checked, allowExtraClosings:$("allowExtraClosings").checked,
      consentContext:CONTEXT.boundaryContext?.token};
    const serialized = JSON.stringify(payload);
    if (serialized !== LAST_PAYLOAD) { REQUEST_ID = crypto.randomUUID(); LAST_PAYLOAD = serialized; }
    const res = await fetch("/api/availability", {
      method: "POST",
      headers: {"Content-Type":"application/json", "X-Submission-CSRF":CSRF},
      body:JSON.stringify({...payload, requestId:REQUEST_ID}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 409) {
        CONSENT_RECONFIRM = true;
        await refreshContext(); // Preserve the unsaved grid, comment and choices.
        updateConsent();
      }
      return showMsg(data.error || "That didn't save. Try again.", "err");
    }

    SAVED = true; $("newSheetBtn").hidden = !!CONTEXT.edit;
    showMsg(`Saved — ${data.availableHours} available, ${data.preferredHours} preferred. Your supervisor will review this response.`, "ok");
  } catch (_) { showMsg("Could not save. Your entries are still here; please retry.", "err");
  } finally {
    SUBMITTING = false;
    $("submitBtn").disabled = SAVED || CONSENT_RECONFIRM || !CSRF || !CONTEXT?.folder;
  }
}

function updateCommentCount() { $("commentCount").textContent = `${[...$("commentInput").value].length} / 99 characters`; }
async function refreshContext() {
  const res = await fetch(CSRF ? "/api/collection/context" : "/api/submission-context");
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    $("codeGate").hidden = false; CSRF = null; updateCollectionDetails();
    $("codeGateMsg").textContent = `${error.error || 'Enter a current code to continue.'} Your unsaved answers are still here.`;
    return false;
  }
  const next = await res.json();
  if (CONTEXT?.unlocked && (next.folder?.id !== CONTEXT.folder?.id || next.revision !== CONTEXT.revision)) {
    CSRF = null; $("codeGate").hidden = false; updateCollectionDetails();
    $("codeGateMsg").textContent = "The scheduling folder changed. Enter a current code to confirm the destination. Your unsaved answers are still here.";
    return false;
  }
  if (CONTEXT && next.boundaryContext?.token !== CONTEXT.boundaryContext?.token) CONSENT_RECONFIRM = true;
  CONTEXT = next; CONFIG = next.config; CSRF = next.csrf || null;
  updateCollectionDetails();
  renderGrid();
  return true;
}

// ---------------- boot ----------------
(async function init() {
  $("submitBtn").disabled = true;
  $("commentInput").oninput = updateCommentCount;
  for (const id of ["allowExtraOpenings", "allowExtraClosings"]) $(id).onchange = updateConsent;
  $("reconfirmConsent").onclick = () => { CONSENT_RECONFIRM = false; updateConsent(); };
  $("clearBoundaryConsent").onclick = () => {
    $("allowExtraOpenings").checked = false;
    $("allowExtraClosings").checked = false;
    CONSENT_RECONFIRM = false;
    updateConsent();
    $("submitBtn").focus();
  };
  $("unlockBtn").onclick = () => unlock();
  $("collectionCode").onkeydown = event => { if (event.key === "Enter") unlock(); };
  $("newSheetBtn").onclick = () => {
    SAVED = false; REQUEST_ID = LAST_PAYLOAD = null; $("newSheetBtn").hidden = true;
    state = {}; $("nameInput").value = ""; $("commentInput").value = ""; resetConsent(); updateCommentCount(); renderGrid();
    $("nameInput").focus();
  };
  window.addEventListener("hashchange", async () => {
    const token = new URLSearchParams(location.hash.slice(1)).get("edit");
    if (token) { history.replaceState(null, "", location.pathname); await unlock(token); }
  });
  const editToken = new URLSearchParams(location.hash.slice(1)).get("edit");
  if (editToken) { history.replaceState(null, "", location.pathname); await unlock(editToken); }
  else {
    const res = await fetch("/api/collection/context");
    if (res.ok) useContext(await res.json());
    else await refreshContext();
  }
  renderGrid();
  bindGrid();

  document.querySelectorAll(".paint-mode").forEach((b) => {
    b.onclick = () => setMode(b.dataset.mode);
  });
  $("submitBtn").onclick = submitAvailability;
  $("clearAllBtn").onclick = () => {
    if (!Object.keys(state).length || confirm("Clear every hour you've marked?")) {
      state = {}; renderGrid();
    }
  };
  $("copyMonBtn").onclick = () => {
    const [first, ...rest] = CONFIG.availabilityDays;
    for (const day of rest) {
      for (const h of hours()) {
        const key = cellKey(day, h);
        delete state[key];
        if (isClosed(day, h)) continue;
        const v = state[cellKey(first, h)] || 0;
        if (v) state[key] = v;
      }
    }
    renderGrid();
  };
  // A new name starts a new set of consent choices.
  $("nameInput").addEventListener("input", () => { LOAD_REVISION++; resetConsent(); updateConsent(); });
})();
