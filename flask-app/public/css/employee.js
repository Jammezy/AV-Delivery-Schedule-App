// ============================================================
// employee.js - WhenIsGood-style availability painting.
//
// state[key] holds 0 (can't), 1 (can work) or 2 (would prefer).
// Level 2 implies level 1 everywhere downstream, so the grid never
// needs a separate "preferred" layer.
// ============================================================

let CONFIG = null;
let ROSTER = { names: [], allowSelfRegister: true };
let EMPLOYEE = null;
let state = {};
let mode = 1;
let painting = false;
let paintedThisDrag = new Set();

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
  const perDay = CONFIG.dayCloseHours || {};
  if (perDay[day] !== undefined && perDay[day] !== null && perDay[day] !== "") return Number(perDay[day]);
  if (day === "Fri" && CONFIG.fridayCloseHour != null) return Number(CONFIG.fridayCloseHour);
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
  grid.style.setProperty("--cols", CONFIG.days.length);
  const parts = ['<div class="wg-corner"></div>'];

  for (const day of CONFIG.days) {
    parts.push(`<button type="button" class="wg-daylabel" data-fillday="${day}"
      title="Fill or clear ${day}">${escapeHtml(day)}</button>`);
  }

  for (const h of hours()) {
    parts.push(`<div class="wg-timelabel">${blockLabel(h)}</div>`);
    for (const day of CONFIG.days) {
      const key = cellKey(day, h);
      const closed = isClosed(day, h);
      parts.push(
        `<button type="button" class="wg-cell" data-key="${key}" data-day="${day}"
           data-hour="${h}" data-level="${closed ? 0 : (state[key] || 0)}"
           ${closed ? 'data-closed="1" disabled' : ""}
           aria-label="${day} ${blockLabel(h)}"></button>`
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
  const values = Object.values(state);
  const avail = values.filter((v) => v >= 1).length;
  const pref = values.filter((v) => v === 2).length;
  $("tallyAvail").textContent = avail;
  $("tallyPref").textContent = pref;

  let target = "";
  if (EMPLOYEE && EMPLOYEE.minHours > 0) {
    target = avail >= EMPLOYEE.minHours
      ? `Your weekly minimum is ${EMPLOYEE.minHours} hrs — you're covered.`
      : `Your weekly minimum is ${EMPLOYEE.minHours} hrs. Mark at least ${EMPLOYEE.minHours - avail} more.`;
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
  $("msgArea").innerHTML = `<div class="msg ${type}">${text}</div>`;
  $("msgArea").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------------- data ----------------
async function loadRoster() {
  try {
    ROSTER = await fetch("/api/roster").then((r) => r.json());
    $("rosterList").innerHTML = ROSTER.names
      .map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
  } catch (_) { /* autocomplete is a nicety, not a requirement */ }
}

async function loadPrevious(quiet) {
  const name = $("nameInput").value.trim();
  if (!name) { if (!quiet) showMsg("Type your name first.", "err"); return; }
  const data = await fetch(`/api/availability/${encodeURIComponent(name)}`).then((r) => r.json());
  EMPLOYEE = data.employee || null;
  if (!data.found) {
    if (!quiet) showMsg("No previous submission under that name — start fresh below.", "info");
    updateTally();
    return;
  }
  state = {};
  for (const [k, v] of Object.entries(data.availability || {})) state[k] = Number(v);
  renderGrid();
  const when = data.submittedAt ? new Date(data.submittedAt).toLocaleString() : "earlier";
  showMsg(`Loaded what you submitted ${when}. Change anything that's different this week.`, "ok");
}

async function submitAvailability() {
  const name = $("nameInput").value.trim();
  if (!name) return showMsg("Enter your name before submitting.", "err");
  const avail = Object.values(state).filter((v) => v >= 1).length;
  if (!avail) return showMsg("Mark at least a few hours before submitting.", "err");

  $("submitBtn").disabled = true;
  try {
    const res = await fetch("/api/availability", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, availability: state }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return showMsg(escapeHtml(data.error || "That didn't save. Try again."), "err");

    let extra = "";
    if (data.shortOfMinimum > 0) {
      extra = ` Heads up: you're ${data.shortOfMinimum} hours below your ${data.minHours} hour ` +
        `weekly minimum, so the schedule may not be able to include you. Add more hours if you can.`;
    } else if (!data.preferredHours) {
      extra = " You didn't mark any preferred hours, so you'll be treated as flexible.";
    }
    showMsg(`Saved — ${data.availableHours} available, ${data.preferredHours} preferred.${escapeHtml(extra)}`,
      data.shortOfMinimum > 0 ? "warn" : "ok");
  } finally {
    $("submitBtn").disabled = false;
  }
}

// ---------------- boot ----------------
(async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  await loadRoster();
  renderGrid();
  bindGrid();

  document.querySelectorAll(".paint-mode").forEach((b) => {
    b.onclick = () => setMode(b.dataset.mode);
  });
  $("loadBtn").onclick = () => loadPrevious(false);
  $("submitBtn").onclick = submitAvailability;
  $("clearAllBtn").onclick = () => {
    if (!Object.keys(state).length || confirm("Clear every hour you've marked?")) {
      state = {}; renderGrid();
    }
  };
  $("copyMonBtn").onclick = () => {
    const [first, ...rest] = CONFIG.days;
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
  // Look up their record as soon as they've picked a name.
  $("nameInput").addEventListener("change", () => loadPrevious(true));
})();
