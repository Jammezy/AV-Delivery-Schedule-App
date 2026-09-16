// ============================================================
// admin.js - login, roster, settings, diagnostics, generation
// and the Excel export.
// ============================================================

let TOKEN = sessionStorage.getItem("adminToken") || null;
let CONFIG = null;
let EMPLOYEES = [];
let LAST_RESULT = null;
let LAST_DIAG = null;

const $ = (id) => document.getElementById(id);

function authHeaders() {
  return { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" };
}
async function apiGet(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) { logout(); return null; }
  return res.json();
}
async function apiSend(url, method, body) {
  const res = await fetch(url, { method, headers: authHeaders(), body: JSON.stringify(body || {}) });
  if (res.status === 401) { logout(); return null; }
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function hourLabel(h) {
  const hh = h % 12 === 0 ? 12 : h % 12;
  return `${hh}${h < 12 ? "AM" : "PM"}`;
}
function blockLabel(h) { return `${hourLabel(h)}–${hourLabel(h + 1)}`; }
function hours() {
  const out = [];
  for (let h = CONFIG.hourStart; h <= CONFIG.hourEnd; h++) out.push(h);
  return out;
}
function closeHourFor(day) {
  const perDay = CONFIG.dayCloseHours || {};
  if (perDay[day] !== undefined && perDay[day] !== null && perDay[day] !== "") return Number(perDay[day]);
  if (day === "Fri" && CONFIG.fridayCloseHour != null) return Number(CONFIG.fridayCloseHour);
  return null;
}
function isClosed(day, h) {
  const ch = closeHourFor(day);
  return ch !== null && h >= ch;
}

// ---------------- auth ----------------
function logout() {
  TOKEN = null;
  sessionStorage.removeItem("adminToken");
  $("loginCard").style.display = "block";
  $("appArea").style.display = "none";
}

async function login() {
  const password = $("passwordInput").value;
  const res = await fetch("/api/admin/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    $("loginMsg").innerHTML = `<div class="msg err">${escapeHtml(data.error || "Login failed.")}</div>`;
    return;
  }
  TOKEN = data.token;
  sessionStorage.setItem("adminToken", TOKEN);
  $("loginCard").style.display = "none";
  $("appArea").style.display = "block";
  await bootApp();
}

function setupTabs() {
  document.querySelectorAll(".tabs button").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll("#appArea > section").forEach((s) => (s.style.display = "none"));
      $(`tab-${btn.dataset.tab}`).style.display = "block";
      if (btn.dataset.tab === "employees") renderEmployees();
      if (btn.dataset.tab === "overview") renderOverview();
    };
  });
}

// ---------------- employees ----------------
async function renderEmployees() {
  EMPLOYEES = (await apiGet("/api/employees")) || [];
  const wrap = $("employeeTableWrap");
  if (!EMPLOYEES.length) {
    wrap.innerHTML = `<p class="hint">Nobody yet. Add someone above, or wait for the first submission.</p>`;
    return;
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th class="left">Name</th><th>Lead</th><th>Min hrs/wk</th><th>Max hrs/wk</th><th></th>
      </tr></thead>
      <tbody>${EMPLOYEES.map((e) => `
        <tr data-name="${escapeHtml(e.name)}">
          <td class="left">${escapeHtml(e.name)}</td>
          <td><input type="checkbox" ${e.isLead ? "checked" : ""} data-field="isLead"></td>
          <td><input type="number" min="0" value="${e.minHours}" data-field="minHours"></td>
          <td><input type="number" min="0" value="${e.maxHours}" data-field="maxHours"></td>
          <td><button class="secondary" data-action="save">Save</button>
              <button class="danger" data-action="delete">Remove</button></td>
        </tr>`).join("")}</tbody>
    </table>`;

  wrap.querySelectorAll("tbody tr").forEach((tr) => {
    const name = tr.dataset.name;
    tr.querySelector('[data-action="save"]').onclick = async () => {
      const r = await apiSend(`/api/employees/${encodeURIComponent(name)}`, "PUT", {
        isLead: tr.querySelector('[data-field="isLead"]').checked,
        minHours: tr.querySelector('[data-field="minHours"]').value,
        maxHours: tr.querySelector('[data-field="maxHours"]').value,
      });
      if (r && !r.ok) { alert(r.data.error || "Couldn't save."); return; }
      tr.style.background = "#e6f4ea";
      setTimeout(() => (tr.style.background = ""), 700);
      refreshDiagnostics();
    };
    tr.querySelector('[data-action="delete"]').onclick = async () => {
      if (!confirm(`Remove ${name} and their submitted availability?`)) return;
      await apiSend(`/api/employees/${encodeURIComponent(name)}`, "DELETE");
      renderEmployees();
      refreshDiagnostics();
    };
  });
}

// ---------------- settings ----------------
const SETTINGS_GROUPS = [
  ["Hours and coverage", [
    ["hourStart", "Opens at (24hr)"],
    ["hourEnd", "Last staffed hour starts at (24hr)"],
    ["fridayCloseHour", "Friday closes at (24hr)"],
    ["reqStaffOpen", "Staff needed per hour"],
    ["reqStaffLate", "Staff needed per late hour"],
    ["lateHourStart", "Late hours begin at (24hr)"],
  ]],
  ["Shift rules", [
    ["minShiftLength", "Shortest shift (hours)"],
    ["maxShiftLength", "Longest shift (hours)"],
    ["maxMorningShifts", "Max opening shifts per person"],
    ["maxEveningShifts", "Max closing shifts per person"],
    ["maxMorningPlusEvening", "Max opening + closing combined"],
  ]],
  ["Fairness and solver", [
    ["burdenWeight", "Weight of an unwanted hour"],
    ["wFairness", "Priority: lift the worst-off person"],
    ["wPreference", "Priority: total preferred hours granted"],
    ["wSpread", "Priority: share opening/closing duty"],
    ["solverTimeLimit", "Solver time limit (seconds)"],
  ]],
];

function renderSettings() {
  $("settingsForm").innerHTML = `
    <div class="settings-grid">
      ${SETTINGS_GROUPS.map(([title, fields]) => `
        <div class="settings-group">
          <h3>${escapeHtml(title)}</h3>
          ${fields.map(([key, label]) => `
            <div style="margin-bottom:10px;">
              <label for="cfg_${key}">${escapeHtml(label)}</label>
              <input type="number" id="cfg_${key}" value="${CONFIG[key]}">
            </div>`).join("")}
        </div>`).join("")}
      <div class="settings-group">
        <h3>Switches</h3>
        <label style="font-weight:400;"><input type="checkbox" id="cfg_requireLeadDuringOpen"
          ${CONFIG.requireLeadDuringOpen ? "checked" : ""}> Require a lead during every open hour</label>
        <label style="font-weight:400;margin-top:8px;"><input type="checkbox" id="cfg_blockClopening"
          ${CONFIG.blockClopening ? "checked" : ""}> Block closing then opening the next morning</label>
        <label style="font-weight:400;margin-top:8px;"><input type="checkbox" id="cfg_allowSelfRegister"
          ${CONFIG.allowSelfRegister ? "checked" : ""}> Let new names add themselves by submitting</label>
        <p class="hint" style="margin-top:10px;">The last staffed hour setting is the
          <em>start</em> of the final block. 21 means the last shift runs 9–10PM.</p>
      </div>
    </div>`;
}

async function saveSettings() {
  const updated = { ...CONFIG };
  for (const [, fields] of SETTINGS_GROUPS) {
    for (const [key] of fields) updated[key] = Number($(`cfg_${key}`).value);
  }
  updated.requireLeadDuringOpen = $("cfg_requireLeadDuringOpen").checked;
  updated.blockClopening = $("cfg_blockClopening").checked;
  updated.allowSelfRegister = $("cfg_allowSelfRegister").checked;

  const r = await apiSend("/api/config", "PUT", updated);
  if (!r) return;
  if (!r.ok) {
    $("settingsMsg").innerHTML = `<div class="msg err">${(r.data.errors || ["Couldn't save."])
      .map(escapeHtml).join("<br>")}</div>`;
    return;
  }
  CONFIG = r.data;
  $("settingsMsg").innerHTML = `<div class="msg ok">Settings saved.</div>`;
  refreshDiagnostics();
}

// ---------------- submissions ----------------
async function renderOverview() {
  const data = await apiGet("/api/availability");
  if (!data) return;
  const submitted = Object.keys(data.availability);
  const rows = data.employees.map((e) => {
    const av = data.availability[e.name];
    if (!av) return `<tr><td class="left">${escapeHtml(e.name)}</td>
      <td colspan="4" style="color:var(--red);">Nothing submitted</td></tr>`;
    const vals = Object.values(av).map(Number);
    const when = data.submittedAt[e.name]
      ? new Date(data.submittedAt[e.name]).toLocaleDateString() : "—";
    return `<tr>
      <td class="left">${escapeHtml(e.name)}${e.isLead ? ' <span class="pill lead">lead</span>' : ""}</td>
      <td>${vals.filter((v) => v >= 1).length}</td>
      <td>${vals.filter((v) => v === 2).length}</td>
      <td>${e.minHours}–${e.maxHours}</td>
      <td>${when}</td></tr>`;
  }).join("");

  $("overviewArea").innerHTML = `
    <p><strong>${submitted.length}</strong> of <strong>${data.employees.length}</strong>
      submitted. ${data.missing.length
        ? `Still waiting on: ${data.missing.map(escapeHtml).join(", ")}.`
        : "Everyone's in."}</p>
    <div class="scroll-x"><table class="data-table">
      <thead><tr><th class="left">Name</th><th>Available hrs</th><th>Preferred hrs</th>
        <th>Target</th><th>Submitted</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// ---------------- diagnostics ----------------
function heatTable(title, valueFor, classFor, legend) {
  const head = `<tr><th></th>${CONFIG.days.map((d) => `<th class="day">${escapeHtml(d)}</th>`).join("")}</tr>`;
  const body = hours().map((h) => `
    <tr><td class="time">${blockLabel(h)}</td>${CONFIG.days.map((d) => {
      if (isClosed(d, h)) return `<td class="closed">—</td>`;
      return `<td class="${classFor(d, h)}">${valueFor(d, h)}</td>`;
    }).join("")}</tr>`).join("");
  return `
    <h3>${escapeHtml(title)}</h3>
    <div class="scroll-x"><table class="heat">${head}${body}</table></div>
    <div class="heat-legend">${legend}</div>`;
}

function renderDiagnostics(payload) {
  LAST_DIAG = payload;
  const d = payload.diagnostics;
  const t = d.totals;
  const burden = payload.burdenMap || {};
  const cov = {};
  for (const row of d.coverage) {
    cov[row.day] = cov[row.day] || {};
    cov[row.day][row.hour] = row;
  }

  const blockers = d.blockers.length;
  const badge = $("diagBadge");
  badge.className = "badge" + (blockers ? "" : " warn");
  badge.textContent = blockers ? blockers : (d.warnings.length || "");
  badge.style.display = (blockers || d.warnings.length) ? "" : "none";

  const stats = `
    <div class="stat-strip">
      <div class="stat"><div class="n">${t.people}</div><div class="k">on the roster</div></div>
      <div class="stat ${t.leads ? "" : "bad"}"><div class="n">${t.leads}</div><div class="k">leads</div></div>
      <div class="stat ${t.capacityHours < t.requiredHours ? "bad" : ""}">
        <div class="n">${t.requiredHours}</div><div class="k">staff-hours needed</div></div>
      <div class="stat ${t.capacityHours < t.requiredHours ? "bad" : ""}">
        <div class="n">${t.capacityHours}</div><div class="k">staff-hours available</div></div>
      <div class="stat ${blockers ? "bad" : d.warnings.length ? "warn" : ""}">
        <div class="n">${blockers}</div><div class="k">blockers</div></div>
    </div>`;

  const findings = `
    ${blockers ? `<h3>Blocking this week</h3>${d.blockers.map((b) =>
      `<div class="finding block"><span class="tag">STOP</span><span>${escapeHtml(b.message)}</span></div>`).join("")}`
      : `<div class="msg ok">Nothing makes this week impossible. The solver has a chance.</div>`}
    ${d.warnings.length ? `<h3>Squeezes worth knowing about</h3>${d.warnings.map((w) =>
      `<div class="finding warn"><span class="tag">TIGHT</span><span>${escapeHtml(w.message)}</span></div>`).join("")}` : ""}`;

  const coverage = heatTable(
    "Coverage: people free vs. people needed",
    (day, h) => { const c = cov[day][h]; return `${c.available}/${c.required}`; },
    (day, h) => {
      const c = cov[day][h];
      if (c.available < c.required) return "lv-short";
      if (c.available === c.required) return "lv-exact";
      if (c.available - c.required <= 2) return "lv-thin";
      if (c.available - c.required <= 5) return "lv-ok";
      return "lv-rich";
    },
    `<span><i style="background:#e8544a"></i> short — impossible</span>
     <span><i style="background:#f0a500"></i> exact — no slack</span>
     <span><i style="background:#ffe9b3"></i> 1–2 spare</span>
     <span><i style="background:#dbe7f1"></i> 3–5 spare</span>
     <span><i style="background:#b8d0e4"></i> plenty</span>`
  );

  const maxDemand = Math.max(1, ...Object.values(d.demand)
    .flatMap((byHour) => Object.values(byHour)));
  const demand = heatTable(
    "Demand: how many people asked for each hour",
    (day, h) => d.demand[day][h],
    (day, h) => {
      const v = d.demand[day][h];
      if (!v) return "lv-0";
      return "lv-" + Math.min(4, Math.max(1, Math.ceil(4 * v / maxDemand)));
    },
    `<span><i style="background:#f7f7f7;border:1px solid #ddd"></i> nobody wants it</span>
     <span><i style="background:#bcd5e8"></i> some demand</span>
     <span><i style="background:#1f4e78"></i> most wanted</span>`
  );

  const burdenHeat = heatTable(
    "Burden: what covering each hour costs you in fairness terms",
    (day, h) => (burden[day] || {})[h] ?? 0,
    (day, h) => {
      const v = (burden[day] || {})[h] ?? 0;
      if (!v) return "lv-0";
      return "lv-" + Math.min(4, Math.ceil(v / 2.5));
    },
    `<span>0 = plenty of people want this hour.
      10 = nobody does, so whoever covers it is owed preferred hours elsewhere.</span>`
  );

  const notSubmitted = d.people.filter((p) => !p.submitted);
  const roster = `
    <h3>Per person</h3>
    <div class="scroll-x"><table class="data-table">
      <thead><tr><th class="left">Name</th><th>Marked</th><th>Preferred</th>
        <th>Schedulable ceiling</th><th>Target</th></tr></thead>
      <tbody>${d.people.map((p) => `
        <tr>
          <td class="left">${escapeHtml(p.name)}${p.isLead ? ' <span class="pill lead">lead</span>' : ""}</td>
          <td>${p.submitted ? p.markedHours : "—"}</td>
          <td>${p.submitted ? p.preferredHours : "—"}</td>
          <td${p.submitted && p.workableCeiling < p.minHours ? ' style="color:var(--red);font-weight:700;"' : ""}>
            ${p.submitted ? p.workableCeiling : "—"}</td>
          <td>${p.minHours}–${p.maxHours}</td>
        </tr>`).join("")}</tbody></table></div>
    ${notSubmitted.length ? `<p class="hint">"Schedulable ceiling" is the most hours this
      person could legally work given one shift a day and a ${CONFIG.minShiftLength}-hour
      minimum — not just the number of boxes they ticked.</p>` : ""}`;

  $("diagArea").innerHTML = stats + findings + coverage + demand + burdenHeat + roster;
}

async function refreshDiagnostics() {
  const payload = await apiGet("/api/diagnostics");
  if (!payload) return;
  CONFIG = payload.config;
  renderDiagnostics(payload);
}

// ---------------- generate ----------------
function fairnessTable(rows) {
  return `
    <h3>How the week landed for each person</h3>
    <div class="scroll-x"><table class="data-table">
      <thead><tr>
        <th class="left">Name</th><th>Hours</th><th>Preferred asked</th>
        <th>Preferred got</th><th class="left">Share of their ask</th>
        <th>Unwanted hours</th><th>Deal score</th>
      </tr></thead>
      <tbody>${rows.map((r) => {
        const pct = r.preferredPct;
        const cls = pct == null ? "" : pct < 40 ? "low" : pct < 70 ? "mid" : "";
        return `<tr>
          <td class="left">${escapeHtml(r.name)}</td>
          <td>${r.hours}</td>
          <td>${r.preferredMarked}</td>
          <td>${r.preferredSatisfied}</td>
          <td class="left">${pct == null
            ? '<span class="hint">no preferences marked</span>'
            : `<span class="bar"><i class="${cls}" style="width:${pct}%"></i><span>${pct}%</span></span>`}</td>
          <td>${r.unwantedHours}</td>
          <td>${r.dealScore}</td>
        </tr>`;
      }).join("")}</tbody></table></div>
    <p class="hint">Share of their ask is measured against what each person could
      realistically have received, so someone who marked 5 preferred hours is scored on
      the same footing as someone who marked 40. The deal score subtracts the cost of
      unwanted hours, and the solver works to lift whoever sits lowest.</p>`;
}

async function generateSchedule() {
  const msg = $("generateMsg");
  msg.innerHTML = `<div class="msg info">Solving… this can take up to
    ${CONFIG.solverTimeLimit} seconds.</div>`;
  $("fairnessOutput").innerHTML = "";
  $("generateBtn").disabled = true;
  $("regenerateBtn").disabled = true;
  await new Promise((r) => setTimeout(r, 30));

  const r = await apiSend("/api/generate", "POST", { seed: Math.floor(Math.random() * 1e9) });
  $("generateBtn").disabled = false;
  $("regenerateBtn").disabled = false;
  if (!r) return;
  const result = r.data;

  if (!r.ok || result.error) {
    msg.innerHTML = `<div class="msg err">${escapeHtml(result.error || "Generation failed.")}</div>`;
    return;
  }

  if (result.diagnostics) renderDiagnostics({ diagnostics: result.diagnostics, config: CONFIG,
    burdenMap: result.burdenMap || {} });

  if (result.status === "IMPOSSIBLE") {
    msg.innerHTML = `<div class="msg err"><strong>No schedule can exist this week.</strong>
      Fix these first, then generate again:<br>${result.diagnostics.blockers
        .map((b) => escapeHtml(b.message)).join("<br>")}</div>`;
    $("scheduleOutput").innerHTML = "";
    return;
  }

  if (result.status === "INFEASIBLE") {
    const rel = result.relaxation || {};
    const gaps = (rel.gaps || []).map((g) => escapeHtml(g.message)).join("<br>");
    const mins = (rel.missedMinimums || []).map((m) => escapeHtml(m.message)).join("<br>");
    msg.innerHTML = `<div class="msg err">
      <strong>The rules conflict with what people submitted.</strong><br>
      ${escapeHtml(rel.message || "")}<br>${gaps}${gaps && mins ? "<br>" : ""}${mins}
      </div>`;
    $("scheduleOutput").innerHTML = "";
    return;
  }

  LAST_RESULT = result;
  const kind = result.status === "OPTIMAL" ? "Optimal" : "Valid";
  msg.innerHTML =
    `<div class="msg ok">${kind} schedule found in ${result.solveSeconds}s — every hour
      staffed, every rule satisfied. Fairness floor: ${result.fairnessFloor}.</div>` +
    (result.note ? `<div class="msg warn">${escapeHtml(result.note)}</div>` : "");

  $("fairnessOutput").innerHTML = fairnessTable(result.fairness);
  $("scheduleOutput").innerHTML = renderPrintableTable(result.schedule, CONFIG);
  $("regenerateBtn").style.display = "inline-block";
  $("downloadBtn").style.display = "inline-block";
}

function slotNamesFor(cfg) {
  const needed = Math.max(cfg.reqStaffOpen, cfg.reqStaffLate, 1);
  const names = (cfg.slotNames || []).filter((s) => String(s).trim()).slice(0, needed);
  while (names.length < needed) names.push(`Slot ${names.length + 1}`);
  return names;
}

function renderPrintableTable(schedule, cfg) {
  const slots = slotNamesFor(cfg);
  let head1 = "<tr>", head2 = "<tr>";
  for (const day of cfg.days) {
    head1 += `<th class="day-header" colspan="${slots.length + 1}">${escapeHtml(day)}</th>`;
    head2 += `<th class="sub-header">Time</th>` +
      slots.map((s) => `<th class="sub-header">${escapeHtml(s)}</th>`).join("");
  }
  let body = "";
  for (const h of hours()) {
    let row = "<tr>";
    for (const day of cfg.days) {
      row += `<td>${blockLabel(h)}</td>`;
      const closed = isClosed(day, h);
      for (const slot of slots) {
        row += `<td>${closed ? "" : escapeHtml((schedule[day][h] || {})[slot] || "")}</td>`;
      }
    }
    body += row + "</tr>";
  }
  return `<div class="printable-schedule"><table>
    <thead>${head1}</tr>${head2}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// ---------------- Excel export ----------------
async function downloadExcel() {
  if (!LAST_RESULT) return;
  const { work, schedule, employees, fairness } = LAST_RESULT;
  const cfg = CONFIG;
  const slots = slotNamesFor(cfg);
  const wb = new ExcelJS.Workbook();

  const navyFill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF1F4E78" } };
  const greyFill = { type: "pattern", pattern: "solid", fgColor: { argb: "FFD9D9D9" } };
  const whiteBold = { color: { argb: "FFFFFFFF" }, bold: true };
  const bold = { bold: true };
  const thin = { top: { style: "thin" }, bottom: { style: "thin" },
                 left: { style: "thin" }, right: { style: "thin" } };
  const center = { horizontal: "center", vertical: "middle", wrapText: true };

  // Sheet 1 - raw 1/0 matrix
  const raw = wb.addWorksheet("Raw_Logic");
  raw.addRow(["Time", ...employees.map((e) => e.name)]);
  raw.getRow(1).font = bold;
  for (const day of cfg.days) {
    for (const h of hours()) {
      raw.addRow([`${day} ${h}:00`,
        ...employees.map((e) => ((work[day][h] || []).includes(e.name) ? 1 : 0))]);
    }
  }
  raw.columns.forEach((c) => (c.width = 14));

  // Sheet 2 - printable
  const ws = wb.addWorksheet("Printable_Schedule");
  cfg.days.forEach((day, dIdx) => {
    const c0 = dIdx * (slots.length + 1) + 1;
    ws.mergeCells(1, c0, 1, c0 + slots.length);
    const head = ws.getCell(1, c0);
    head.value = `Day: ${day}`;
    head.fill = navyFill; head.font = whiteBold; head.alignment = center;
    ["Time", ...slots].forEach((label, i) => {
      const c = ws.getCell(2, c0 + i);
      c.value = label; c.fill = greyFill; c.font = bold; c.alignment = center; c.border = thin;
    });
  });
  let r = 3;
  for (const h of hours()) {
    cfg.days.forEach((day, dIdx) => {
      const c0 = dIdx * (slots.length + 1) + 1;
      const closed = isClosed(day, h);
      const t = ws.getCell(r, c0);
      t.value = blockLabel(h); t.border = thin; t.alignment = center;
      slots.forEach((slot, i) => {
        const c = ws.getCell(r, c0 + i + 1);
        c.value = closed ? "" : (schedule[day][h] || {})[slot] || "";
        c.border = thin; c.alignment = center;
      });
    });
    r++;
  }
  const totalCols = cfg.days.length * (slots.length + 1);
  for (let col = 1; col <= totalCols; col++) {
    let max = 0;
    for (let row = 2; row <= ws.rowCount; row++) {
      const v = ws.getCell(row, col).value;
      if (v && String(v).length > max) max = String(v).length;
    }
    ws.getColumn(col).width = Math.max(max + 4, 12);
  }

  // Sheet 3 - fairness, so you can defend the schedule when someone asks
  const fs = wb.addWorksheet("Fairness");
  fs.addRow(["Name", "Hours", "Preferred asked", "Preferred got",
             "Share of their ask (%)", "Unwanted hours", "Deal score"]);
  fs.getRow(1).font = bold;
  (fairness || []).forEach((row) => fs.addRow([
    row.name, row.hours, row.preferredMarked, row.preferredSatisfied,
    row.preferredPct == null ? "n/a" : row.preferredPct,
    row.unwantedHours, row.dealScore,
  ]));
  fs.columns.forEach((c) => (c.width = 18));

  const buf = await wb.xlsx.writeBuffer();
  const url = URL.createObjectURL(new Blob([buf], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url; a.download = "Final_Schedule.xlsx"; a.click();
  URL.revokeObjectURL(url);
}

// ---------------- boot ----------------
async function bootApp() {
  CONFIG = await fetch("/api/config").then((res) => res.json());
  renderSettings();
  await refreshDiagnostics();
}

setupTabs();
$("loginBtn").onclick = login;
$("passwordInput").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("saveSettingsBtn").onclick = saveSettings;
$("generateBtn").onclick = generateSchedule;
$("regenerateBtn").onclick = generateSchedule;
$("downloadBtn").onclick = downloadExcel;
$("refreshDiagBtn").onclick = refreshDiagnostics;
$("addEmpBtn").onclick = async () => {
  const name = $("newEmpName").value.trim();
  if (!name) return;
  await apiSend(`/api/employees/${encodeURIComponent(name)}`, "PUT",
    { isLead: false, minHours: 0, maxHours: 40 });
  $("newEmpName").value = "";
  renderEmployees();
  refreshDiagnostics();
};

if (TOKEN) {
  $("loginCard").style.display = "none";
  $("appArea").style.display = "block";
  bootApp();
}
