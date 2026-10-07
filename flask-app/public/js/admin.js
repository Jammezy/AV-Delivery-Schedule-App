// ============================================================
// admin.js - login, roster, settings, diagnostics, generation
// and the Excel export.
// ============================================================

let TOKEN = sessionStorage.getItem("adminToken") || null;
let CONFIG = null;
let EMPLOYEES = [];
let LAST_RESULT = null;
let LAST_DIAG = null;
let GENERATION_JOB = null;
let STAFFING_PLAN = null, PLAN_REQUEST = 0, PLAN_STATUS = "";
const EMPLOYEE_DRAFTS = new Map();

function renderStaffingPlan() {
  let html = `<h3>Weekly hours remaining</h3>`;
  if (!STAFFING_PLAN || PLAN_STATUS) {
    html += `<p>${escapeHtml(PLAN_STATUS || "Loading staffing totals…")}</p>`;
  } else {
    let allotted = STAFFING_PLAN.allottedHours, preview = false, invalid = false;
    for (const employee of STAFFING_PLAN.employees) {
      const draft = EMPLOYEE_DRAFTS.get(employee.name);
      if (!draft || String(employee.minHours) === draft.minHours) continue;
      preview = true;
      const min = Number(draft.minHours), max = Number(draft.maxHours);
      if (draft.minHours.trim() === "" || !Number.isSafeInteger(min) || min < 0 ||
          draft.maxHours.trim() === "" || !Number.isSafeInteger(max) || min > max) {
        invalid = true; continue;
      }
      allotted += min - employee.minHours;
    }
    const remaining = STAFFING_PLAN.requiredHours - allotted;
    const status = remaining > 0 ? `${remaining} hours remaining` : remaining === 0
      ? "0 — minimum hours match demand" : `−${Math.abs(remaining)} — minimum hours exceed demand by ${Math.abs(remaining)}`;
    html += `<p>Based on this folder’s employees’ minimum weekly hours.</p>`;
    if (invalid) {
      html += `<p class="msg err">Unsaved input is invalid. Enter nonnegative whole hours with minimum no greater than maximum. Saved remaining hours: ${STAFFING_PLAN.remainingHours}.</p>`;
    } else {
      html += `<p><strong>${preview ? "Unsaved preview" : "Saved totals"}</strong></p>
        <div class="stat-strip">
          <div class="stat"><div class="n">${STAFFING_PLAN.requiredHours}</div><div class="k">Required weekly staff-hours</div></div>
          <div class="stat"><div class="n">${allotted}</div><div class="k">Total minimum hours allotted</div></div>
        </div><p class="planning-balance"><strong>${status}</strong></p>`;
    }
    html += `<p class="hint">Matching total hours does not guarantee individual shift coverage. Check availability, leads, and scheduling rules below or in Week check.</p>`;
  }
  for (const id of ["diagnosticsStaffingPlan", "employeesStaffingPlan"]) $(id).innerHTML = html;
}

async function refreshStaffingPlan() {
  const request = ++PLAN_REQUEST, revision = VIEW_REVISION, folderId = FOLDER_ID;
  if (!folderId) { STAFFING_PLAN = null; PLAN_STATUS = 'Create or select a folder.'; renderStaffingPlan(); return null; }
  PLAN_STATUS = "Loading staffing totals…";
  renderStaffingPlan();
  const plan = await apiGet(`/api/folders/${folderId}/staffing-plan`);
  if (!TOKEN || request !== PLAN_REQUEST || revision !== VIEW_REVISION) return null;
  STAFFING_PLAN = plan;
  EMPLOYEES = plan?.employees || [];
  PLAN_STATUS = plan ? "" : "Could not load staffing totals. Re-check or reopen Employees to retry.";
  renderStaffingPlan();
  return plan;
}

function $(id) { return document.getElementById(id); }

function authHeaders() {
  return { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" };
}
async function apiGet(url) {
  const token = TOKEN, revision = VIEW_REVISION;
  try {
    const res = await fetch(url, {headers:authHeaders()});
    const data = await res.json();
    if (!TOKEN || TOKEN !== token || revision !== VIEW_REVISION) return null;
    if (res.status === 401) { clearSession(); return null; }
    if (!res.ok) {
      if (res.status === 404 && /\/generation-jobs\//.test(url)) {
        const folderId = url.split('/')[3];
        sessionStorage.removeItem(`generationJob:${folderId}`);
        return {missing:true};
      }
      if (res.status === 404 && (/^\/api\/folders\//.test(url) || /^\/api\/availability\?/.test(url))) {
        await recoverRemovedContent();
      }
      $("folderMsg").textContent = data.error || "Could not load data."; return null;
    }
    return data;
  } catch (_) { if (TOKEN === token && revision === VIEW_REVISION) $("folderMsg").textContent = "Connection failed. Please retry."; return null; }
}
async function apiSend(url, method, body) {
  const token = TOKEN, revision = VIEW_REVISION;
  try {
    const res = await fetch(url, {method, headers:authHeaders(), body:JSON.stringify(body || {})});
    const data = await res.json().catch(() => ({}));
    if (!TOKEN || TOKEN !== token || revision !== VIEW_REVISION) return null;
    if (res.status === 401) { clearSession(); return null; }
    if ((res.status === 404 && /^\/api\/folders\//.test(url)) ||
        (res.status === 409 && ["/api/generate", "/api/generate_weekend", "/api/save_weekend"].includes(url))) {
      await recoverRemovedContent();
    }
    return {ok:res.ok, status:res.status, data};
  } catch (_) { return {ok:false, data:{error:"Connection failed. Please retry."}}; }
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
function hours(cfg = CONFIG) {
  const out = [];
  for (let h = cfg.hourStart; h <= cfg.hourEnd; h++) out.push(h);
  return out;
}
function closeHourFor(day, cfg = CONFIG) {
  if (day === "Sun") return 17;
  const perDay = cfg.dayCloseHours || {};
  if (perDay[day] !== undefined && perDay[day] !== null && perDay[day] !== "") return Number(perDay[day]);
  if (day === "Fri" && cfg.fridayCloseHour != null) return Number(cfg.fridayCloseHour);
  return null;
}
function isClosed(day, h, cfg = CONFIG) {
  const ch = closeHourFor(day, cfg);
  return ch !== null && h >= ch;
}

// ---------------- auth ----------------
function clearSession() {
  GENERATION_JOB = null;
  clearCollectionView();
  PERMISSION_DRAFTS.clear();
  STAFFING_PLAN = null; PLAN_REQUEST++; PLAN_STATUS = ""; EMPLOYEE_DRAFTS.clear();
  $("diagnosticsStaffingPlan").innerHTML = $("employeesStaffingPlan").innerHTML = "";
  TOKEN = null; CONFIG = null; EMPLOYEES = []; LAST_DIAG = null;
  FOLDERS = []; FOLDER_ID = ACTIVE_FOLDER = null; SELECTED.clear(); OVERVIEW = null;
  PINNED = PREVIEWED = null; VIEW_REVISION++;
  clearResult(); clearWeekendView(); DELETION_BUSY = false; cancelFolderDeletion(); closeEditAvailabilityModal();
  editAvailState = {};
  $("editAvailGrid").innerHTML = ""; $("editAvailComment").value = "";
  for (const id of ["employeeTableWrap","overviewArea","diagArea","settingsForm","savedSchedules","generatorSelection","folderSelect","folderStatus","folderMsg","diagBadge"]) $(id).innerHTML = "";
  sessionStorage.removeItem("adminToken");
  $("passwordInput").value = "";
  $("loginCard").style.display = "block"; $("appArea").style.display = "none";
}
async function logout() {
  try {
    const res = await fetch("/api/admin/logout", {method:"POST", headers:authHeaders()});
    if (!res.ok) throw new Error("logout");
    clearSession();
  } catch (_) { $("folderMsg").textContent = "Logout could not reach the server. Please retry."; }
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
  $("passwordInput").value = "";
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
      if (FOLDER_LOADING) return;
      if (btn.dataset.tab === "employees") renderEmployees();
      if (btn.dataset.tab === "overview") { renderOverview(); renderIntake(); }
      if (btn.dataset.tab === "codes") renderCodes();
      if (btn.dataset.tab === "weekend") renderWeekendUI();
    };
  });
}

// ---------------- employees ----------------
let EMPLOYEES_SAVING = false;
function employeeRowValues(tr) {
  const minimum = tr.querySelector('[data-field="minHours"]');
  const maximum = tr.querySelector('[data-field="maxHours"]');
  if (![minimum, maximum].every(input => input.value.trim() !== '' && Number.isSafeInteger(Number(input.value)) && Number(input.value) >= 0) || Number(minimum.value) > Number(maximum.value)) return null;
  return {id:Number(tr.dataset.id), isLead:tr.querySelector('[data-field="isLead"]').checked,
    minHours:Number(minimum.value), maxHours:Number(maximum.value)};
}

$("saveAllEmployeesBtn").onclick = async () => {
  if (!FOLDER_ID || FOLDER_LOADING || DELETION_BUSY || EMPLOYEES_SAVING) return;
  const folderId = FOLDER_ID, revision = VIEW_REVISION;
  const rows = [...$('employeeTableWrap').querySelectorAll('tbody tr')];
  if (!rows.length) return;
  const employees = rows.map(employeeRowValues);
  const invalid = employees.indexOf(null);
  if (invalid >= 0) {
    $('employeesSaveMsg').textContent = `${rows[invalid].dataset.name}: enter nonnegative whole hours with minimum no greater than maximum.`;
    return;
  }
  EMPLOYEES_SAVING = true; renderFolderControls();
  $('employeesSaveMsg').textContent = 'Saving all employees…';
  const response = await apiSend(`/api/folders/${folderId}/employees`, 'PUT', {employees});
  if (revision !== VIEW_REVISION) return;
  EMPLOYEES_SAVING = false; renderFolderControls();
  if (!response?.ok) { $('employeesSaveMsg').textContent = response?.data.error || 'Could not save. Please retry.'; return; }
  EMPLOYEE_DRAFTS.clear();
  $('employeesSaveMsg').textContent = 'All employee choices saved for this folder.';
  await refreshDiagnostics();
};

async function renderEmployees() {
  const folderId = FOLDER_ID, revision = VIEW_REVISION;
  // A tab refresh replaces the rows; don't accept edits against the old rows.
  document.querySelectorAll('#employeeTableWrap input, #employeeTableWrap button').forEach(el => el.disabled = true);
  $('saveAllEmployeesBtn').disabled = true;
  const plan = await refreshStaffingPlan();
  if (revision !== VIEW_REVISION || folderId !== FOLDER_ID) return;
  if (!plan) { renderFolderControls(); return; }
  EMPLOYEES = plan.employees;
  const wrap = $("employeeTableWrap");
  if (!EMPLOYEES.length) {
    wrap.innerHTML = `<p class="hint">No employees yet. Employees appear here when they submit availability for this folder.</p>`;
    renderFolderControls();
    return;
  }
  wrap.innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th class="left">Name</th><th>Lead</th><th>Min hrs/wk</th><th>Max hrs/wk</th><th></th>
      </tr></thead>
      <tbody>${EMPLOYEES.map((e) => `
        <tr data-id="${e.id}" data-name="${escapeHtml(e.name)}">
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
    const inputs = [...tr.querySelectorAll("input")];
    const draft = EMPLOYEE_DRAFTS.get(name);
    if (draft) for (const input of inputs) {
      if (input.type === "checkbox") input.checked = draft[input.dataset.field];
      else input.value = draft[input.dataset.field];
    }
    for (const input of inputs) input.oninput = () => {
      $('employeesSaveMsg').textContent = '';
      EMPLOYEE_DRAFTS.set(name, Object.fromEntries(inputs.map(el =>
        [el.dataset.field, el.type === "checkbox" ? el.checked : el.value])));
      renderStaffingPlan();
    };
    tr.querySelector('[data-action="save"]').onclick = async () => {
      if (folderId !== FOLDER_ID || revision !== VIEW_REVISION || FOLDER_LOADING || EMPLOYEES_SAVING || DELETION_BUSY) return;
      const minimum = tr.querySelector('[data-field="minHours"]');
      const maximum = tr.querySelector('[data-field="maxHours"]');
      if (![minimum, maximum].every(input => input.value.trim() !== "" && Number.isSafeInteger(Number(input.value)) && Number(input.value) >= 0) || Number(minimum.value) > Number(maximum.value)) {
        alert("Enter nonnegative whole hours with minimum no greater than maximum."); return;
      }
      const token = TOKEN;
      EMPLOYEES_SAVING = true; renderFolderControls();
      const controls = [...tr.querySelectorAll("input, button")];
      controls.forEach(el => el.disabled = true);
      const r = await apiSend(`/api/folders/${folderId}/employees/${tr.dataset.id}`, "PUT", {
        isLead: tr.querySelector('[data-field="isLead"]').checked,
        minHours: tr.querySelector('[data-field="minHours"]').value,
        maxHours: tr.querySelector('[data-field="maxHours"]').value,
      });
      if (!TOKEN || TOKEN !== token || revision !== VIEW_REVISION || !r) return;
      EMPLOYEES_SAVING = false; renderFolderControls();
      if (!r.ok) { alert(r.data.error || "Couldn't save."); return; }
      EMPLOYEE_DRAFTS.delete(name);
      tr.style.background = "#e6f4ea";
      setTimeout(() => (tr.style.background = ""), 700);
      await refreshDiagnostics();
    };
    tr.querySelector('[data-action="delete"]').onclick = async () => {
      if (folderId !== FOLDER_ID || revision !== VIEW_REVISION || FOLDER_LOADING || EMPLOYEES_SAVING || DELETION_BUSY) return;
      if (!confirm(`Remove ${name} from this folder? Saved response history and other folders will be preserved.`)) return;
      const result = await apiSend(`/api/folders/${folderId}/employees/${tr.dataset.id}`, "DELETE");
      if (!result?.ok) { if (result) alert(result.data.error); return; }
      EMPLOYEE_DRAFTS.delete(name);
      await renderEmployees();
      await renderOverview(); await refreshDiagnostics(); await renderWeekendUI();
    };
  });
  renderFolderControls();
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
    ["solverTimeLimit", "Optimization time budget (seconds, up to 1800)"],
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
              <input type="number" id="cfg_${key}" value="${CONFIG[key]}" ${key === 'solverTimeLimit' ? 'min="1" max="1800"' : ''}>
              ${key === 'solverTimeLimit' ? '<p class="hint">More time can improve fairness while its optimum is unproven. Runs keep the best quality found for unchanged inputs and stop early when all goals are proven optimal.</p>' : ''}
            </div>`).join("")}
        </div>`).join("")}
      <div class="settings-group">
        <h3>Switches</h3>
        <label><input type="checkbox" id="cfg_allowPreferredBoundaryExtras" ${CONFIG.allowPreferredBoundaryExtras ? "checked" : ""}> Allow additional fully preferred opening/closing shifts</label>
        <p class="hint">Explicit employee consent is also required. Fairness comes first, then preferred hours, then fewer overruns, then sharing boundary shifts. The three normal limits still apply without qualifying consent.</p>
        <div style="display:flex;align-items:center;gap:6px;">
          <label style="font-weight:400;"><input type="checkbox" id="cfg_requireLeadDuringOpen"
            ${CONFIG.requireLeadDuringOpen ? "checked" : ""}> <span id="leadOpenLabel"></span></label>
          <button type="button" class="secondary help-btn" id="leadHelpBtn"
            aria-label="Help: lead switches" aria-expanded="false" aria-controls="leadHelpPopup"
            style="border-radius:50%;padding:0 8px;flex-shrink:0;">?</button>
        </div>
        <label style="font-weight:400;margin-top:8px;margin-left:22px;"><input type="checkbox" id="cfg_requireLeadDuringLate"
          ${CONFIG.requireLeadDuringLate ? "checked" : ""}> <span id="leadLateLabel"></span></label>
        <div id="leadHelpPopup" class="card" style="display:none;max-width:100%;box-sizing:border-box;margin-top:8px;">
          <p id="leadOpenHelp"></p>
          <p id="leadLateHelp"></p>
          <p>The two switches work separately, so you can turn either one on or off on its own.
            Late hours begin at the time set under "Late hours begin at" in Hours and coverage.</p>
          <button type="button" class="secondary" id="leadHelpClose">Close</button>
        </div>
        <label style="font-weight:400;margin-top:8px;"><input type="checkbox" id="cfg_blockClopening"
          ${CONFIG.blockClopening ? "checked" : ""}> Block closing then opening the next morning</label>
        <label style="font-weight:400;margin-top:8px;"><input type="checkbox" id="cfg_allowSelfRegister"
          ${CONFIG.allowSelfRegister ? "checked" : ""}> Let new names add themselves by submitting</label>
        <p class="hint" style="margin-top:10px;">The last staffed hour setting is the
          <em>start</em> of the final block. 21 means the last shift runs 9–10PM.</p>
      </div>
    </div>`;
  const updateLeadLabels = () => {
    const currentHour = key => {
      const value = $(`cfg_${key}`).value;
      return value.trim() !== "" && Number.isFinite(Number(value)) ? Number(value) : Number(CONFIG[key]);
    };
    const start = currentHour("hourStart"), end = currentHour("hourEnd"), late = currentHour("lateHourStart");
    const openLabel = `Require a lead from ${hourLabel(start)} to ${hourLabel(late)}`;
    const lateLabel = `Require a lead during late hours (${hourLabel(late)} to ${hourLabel(end + 1)})`;
    $("cfg_requireLeadDuringOpen").disabled = late <= start;
    $("cfg_requireLeadDuringLate").disabled = late > end;
    $("leadOpenLabel").textContent = late <= start ? "No day hours with the current settings." : openLabel;
    $("leadLateLabel").textContent = late > end ? "No late hours with the current settings." : lateLabel;
    $("leadOpenHelp").textContent = late <= start ? "No day hours with the current settings." :
      `${openLabel}: every staffed hour in this window must include at least one person marked as a lead on the Employees tab. If this is off, the day hours don't need a lead.`;
    $("leadLateHelp").textContent = late > end ? "No late hours with the current settings." :
      `${lateLabel}: every staffed late hour must include at least one lead. If this is off, anyone can work ${hourLabel(late)} to ${hourLabel(end + 1)}.`;
  };
  for (const key of ["hourStart", "hourEnd", "lateHourStart"]) {
    $(`cfg_${key}`).addEventListener("input", updateLeadLabels);
    $(`cfg_${key}`).addEventListener("change", updateLeadLabels);
  }
  updateLeadLabels();
  const helpButton = $("leadHelpBtn"), popup = $("leadHelpPopup");
  const setHelpOpen = open => {
    popup.style.display = open ? "block" : "none";
    helpButton.setAttribute("aria-expanded", String(open));
  };
  helpButton.addEventListener("click", () => setHelpOpen(helpButton.getAttribute("aria-expanded") !== "true"));
  $("leadHelpClose").addEventListener("click", () => { setHelpOpen(false); helpButton.focus(); });
  const escapeHelp = event => {
    if (event.key === "Escape" && helpButton.getAttribute("aria-expanded") === "true") {
      event.stopPropagation();
      setHelpOpen(false);
      helpButton.focus();
    }
  };
  helpButton.addEventListener("keydown", escapeHelp);
  popup.addEventListener("keydown", escapeHelp);
}

async function saveSettings() {
  if (!FOLDER_ID || !CONFIG || FOLDER_LOADING) return;
  const folderId = FOLDER_ID, revision = VIEW_REVISION;
  const updated = { ...CONFIG };
  for (const [, fields] of SETTINGS_GROUPS) {
    for (const [key] of fields) updated[key] = Number($(`cfg_${key}`).value);
  }
  updated.requireLeadDuringOpen = $("cfg_requireLeadDuringOpen").checked;
  updated.requireLeadDuringLate = $("cfg_requireLeadDuringLate").checked;
  updated.blockClopening = $("cfg_blockClopening").checked;
  updated.allowSelfRegister = $("cfg_allowSelfRegister").checked;
  updated.allowPreferredBoundaryExtras = $("cfg_allowPreferredBoundaryExtras").checked;

  const r = await apiSend(`/api/folders/${folderId}/config`, "PUT", updated);
  if (!r) return;
  if (!r.ok) {
    $("settingsMsg").innerHTML = `<div class="msg err">${(r.data.errors || ["Couldn't save."])
      .map(escapeHtml).join("<br>")}</div>`;
    return;
  }
  if (revision !== VIEW_REVISION || folderId !== FOLDER_ID) return;
  CONFIG = r.data;
  $("settingsMsg").innerHTML = `<div class="msg ok">Settings saved.</div>`;
  refreshDiagnostics();
  if (FOLDER_ID) await renderOverview();
}

// ---------------- submissions ----------------
async function renderOverview(reset = false) { await renderFolderOverview(reset); await renderIntake(); }

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

  if (!d.coverage.length) {
    $("diagArea").innerHTML = d.blockers.map(b =>
      `<div class="finding block"><span>${escapeHtml(b.message)}</span></div>`).join("") ||
      "No submitted availability. Refresh after employees submit.";
    return;
  }

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
  const revision = VIEW_REVISION;
  await refreshStaffingPlan();
  if (revision !== VIEW_REVISION) return;
  if (!TOKEN || !FOLDER_ID) return;
  const payload = await apiGet(`/api/diagnostics?${selectionQuery()}`);
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

function boundarySummary(result) {
  const rows = (result.boundarySummary || []).filter(r => r.overrun > 0);
  if (!rows.length) return "";
  return `<h3>Additional preferred boundary shifts</h3><p>These extras used complete preferred blocks and explicit employee consent captured with this schedule. Extras are optional.</p><div class="scroll-x"><table class="data-table"><thead><tr><th>Employee</th><th>Openings / limit</th><th>Closings / limit</th><th>Combined / limit</th><th>Qualifying assigned credits (opening / closing)</th></tr></thead><tbody>${rows.map(r => `<tr><td>${escapeHtml(r.name)}</td><td>${r.openings} / ${r.caps.openings}</td><td>${r.closings} / ${r.caps.closings}</td><td>${r.openings + r.closings} / ${r.caps.combined}</td><td>${r.qualifyingOpenings} / ${r.qualifyingClosings}</td></tr>`).join("")}</tbody></table></div>`;
}

async function generateSchedule() {
  if (!FOLDER_ID || GENERATION_JOB) return;
  const revision = VIEW_REVISION, folderId = FOLDER_ID, employeeIds = [...SELECTED];
  clearResult();
  GENERATION_JOB = {folderId, status:'submitting'};
  $("generateMsg").textContent = 'Starting schedule generation…';
  $("fairnessOutput").innerHTML = "";
  $("generateBtn").disabled = true;
  $("regenerateBtn").disabled = true;
  await new Promise((r) => setTimeout(r, 30));

  if (revision !== VIEW_REVISION) return;
  const requestId = crypto.randomUUID().replaceAll('-', '');
  // A lost POST response may still mean a completed job. Persist its key before
  // sending so a reload can retrieve that exact result without resubmitting.
  sessionStorage.setItem(`generationJob:${folderId}`, requestId);
  const r = await apiSend("/api/generate", "POST", { seed: Math.floor(Math.random() * 1e9), folderId, employeeIds, requestId });
  if (revision !== VIEW_REVISION) return;
  GENERATION_JOB = null;
  renderFolderControls();
  if (!r) return;
  if (!r.ok) {
    $("generateMsg").textContent = r.data.error || 'Could not start generation. Checking for an accepted job…';
    // A disconnected response can still mean the job was accepted.
    await resumeGeneration();
    return;
  }
  sessionStorage.setItem(`generationJob:${folderId}`, r.data.jobId);
  await watchGeneration(r.data, revision);
}

async function resumeGeneration() {
  if (!FOLDER_ID || GENERATION_JOB) return;
  const revision = VIEW_REVISION, folderId = FOLDER_ID;
  const id = sessionStorage.getItem(`generationJob:${folderId}`);
  let job = await apiGet(`/api/folders/${folderId}/generation-jobs/${id || 'latest'}`);
  if (!job || revision !== VIEW_REVISION) return;
  if (job.missing && id) job = await apiGet(`/api/folders/${folderId}/generation-jobs/latest`);
  if (!job || job.missing || revision !== VIEW_REVISION) return;
  if (['queued','running'].includes(job.status) || id) await watchGeneration(job, revision);
}

async function watchGeneration(job, revision) {
  const folderId = job.folderId;
  let delay = 2000;
  while (revision === VIEW_REVISION && TOKEN && folderId === FOLDER_ID) {
    GENERATION_JOB = job;
    renderFolderControls();
    if (!['queued','running'].includes(job.status)) {
      GENERATION_JOB = null;
      sessionStorage.removeItem(`generationJob:${folderId}`);
      renderFolderControls();
      if (job.status === 'completed') await showGenerationResult({ok:true, data:job.result});
      else $("generateMsg").textContent = job.error || 'Schedule generation cancelled.';
      return;
    }
    sessionStorage.setItem(`generationJob:${folderId}`, job.jobId);
    const seconds = job.startedAt ? Math.max(0, Math.floor((Date.now()-Date.parse(job.startedAt))/1000)) : 0;
    $("generateMsg").innerHTML = `<div class="msg info" role="status">${job.status === 'queued' ? 'Waiting for the current schedule to finish.' :
      `Optimizing… ${seconds}s elapsed. Search budget: ${job.timeLimit}s.`}
      ${job.bestFairness == null ? '' : `Best fairness found: ${job.bestFairness}.`}
      ${job.fairnessOptimal ? 'The best fairness is proven; refining the remaining goals.' : ''}
      ${job.attempts > 1 ? 'Resumed after an interruption, keeping saved progress.' : ''}
      You can use other pages while this runs. Reloading will reconnect.
      <button id="cancelGenerationBtn" class="secondary">Cancel generation</button></div>`;
    $("cancelGenerationBtn").onclick = async () => {
      $("cancelGenerationBtn").disabled = true;
      const response = await apiSend(`/api/folders/${folderId}/generation-jobs/${job.jobId}/cancel`, 'POST', {});
      if (response?.ok && revision === VIEW_REVISION) job = response.data;
    };
    await new Promise(r => setTimeout(r, delay));
    if (revision !== VIEW_REVISION || !TOKEN) return;
    const update = await apiGet(`/api/folders/${folderId}/generation-jobs/${job.jobId}`);
    if (update?.missing) {
      GENERATION_JOB = null; renderFolderControls();
      $("generateMsg").textContent = 'This generation job or its folder was removed.';
      return;
    }
    if (update) { job = update; delay = 2000; }
    else {
      delay = Math.min(delay * 2, 10000);
      if (revision === VIEW_REVISION && TOKEN) $("generateMsg").textContent = 'Reconnecting to generation… Your job and saved progress remain in the database.';
    }
  }
}

async function showGenerationResult(r) {
  const msg = $("generateMsg");
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

  if (!["OPTIMAL","FEASIBLE"].includes(result.status)) { msg.textContent = result.message || "No valid schedule found. Try again."; return; }
  LAST_RESULT = result;
  const kind = result.status === "OPTIMAL" ? "Optimal" : "Valid";
  msg.innerHTML =
    `<div class="msg ok">${kind} schedule found in ${result.solveSeconds}s — every hour
      staffed, every rule satisfied. Fairness floor: ${result.fairnessFloor}.</div>` +
    (result.note ? `<div class="msg warn">${escapeHtml(result.note)}</div>` : "");
  if (result.qualityChange === 'improved') {
    msg.innerHTML += `<div class="msg ok">${result.fairnessImprovement > 0 ?
      `Fairness improved by ${result.fairnessImprovement} points compared with the best previous run on these inputs.` :
      'Fairness was preserved and another scheduling goal improved.'}</div>`;
  } else if (result.qualityChange === 'unchanged') {
    msg.innerHTML += '<div class="msg info">No quality improvement found during this run. The best previous schedule was kept. Extra time may help only when optimality remains unproven.</div>';
  }

  $("fairnessOutput").innerHTML = fairnessTable(result.fairness) + boundarySummary(result);
  $("scheduleOutput").innerHTML = renderPrintableTable(result.schedule, result.config);
  $("regenerateBtn").style.display = "inline-block";
  $("downloadBtn").style.display = "inline-block";
  await loadSavedSchedules();
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
  for (const h of hours(cfg)) {
    let row = "<tr>";
    for (const day of cfg.days) {
      row += `<td>${blockLabel(h)}</td>`;
      const closed = isClosed(day, h, cfg);
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
  const revision = VIEW_REVISION, result = LAST_RESULT;
  const { work, schedule, employees, fairness } = LAST_RESULT;
  const cfg = LAST_RESULT.config;
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
    for (const h of hours(cfg)) {
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
  for (const h of hours(cfg)) {
    cfg.days.forEach((day, dIdx) => {
      const c0 = dIdx * (slots.length + 1) + 1;
      const closed = isClosed(day, h, cfg);
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

  if (LAST_RESULT.boundarySummary) {
    const bs = wb.addWorksheet("Boundary consent");
    bs.addRow(["Extras require explicit consent and complete preferred assigned blocks. Snapshot values below."]);
    bs.addRow(["Name", "Openings", "Closings", "Opening cap", "Closing cap", "Combined cap", "Opening credits", "Closing credits", "Recorded opening", "Recorded closing", "Effective opening", "Effective closing", "Context", "Feature enabled"]);
    LAST_RESULT.boundarySummary.forEach(r => bs.addRow([r.name, r.openings, r.closings, r.caps.openings, r.caps.closings, r.caps.combined, r.qualifyingOpenings, r.qualifyingClosings, r.consent.allowExtraOpenings, r.consent.allowExtraClosings, r.consent.effectiveOpenings, r.consent.effectiveClosings, r.consent.consentContext, r.consent.enabled]));
    bs.columns.forEach(c => c.width = 20);
  }

  const buf = await wb.xlsx.writeBuffer();
  if (revision !== VIEW_REVISION || LAST_RESULT !== result || DELETION_BUSY) return;
  const url = URL.createObjectURL(new Blob([buf], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url; a.download = "Final_Schedule.xlsx"; a.click();
  URL.revokeObjectURL(url);
}

// ---------------- boot ----------------
async function bootApp() {
  if (!TOKEN) return;
  await loadFolders();
}

setupTabs();
setupFolders();
$("loginBtn").onclick = login;
$("passwordInput").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("saveSettingsBtn").onclick = saveSettings;
$("generateBtn").onclick = generateSchedule;
$("regenerateBtn").onclick = generateSchedule;
$("downloadBtn").onclick = downloadExcel;
$("refreshDiagBtn").onclick = refreshDiagnostics;

if (TOKEN) {
  $("loginCard").style.display = "none";
  $("appArea").style.display = "block";
  bootApp();
}


// --- Weekend Generator ---
let WKND_EXCLUDED = [];
let WKND_ROTATING_ORDER = [];
let WKND_FIXED = {};
let LAST_WKND_PREVIEW = null;
let WEEKEND_CHOICES_SAVING = false;

function loadWeekendChoices() {
  const choices = CONFIG?.weekendChoices;
  if (!choices) return;
  $('wkndStart').value = choices.start_date || '';
  $('wkndEnd').value = choices.end_date || '';
  WKND_EXCLUDED = (choices.excluded_dates || []).map(item => ({...item}));
  WKND_FIXED = {...choices.fixed_assignments};
  WKND_ROTATING_ORDER = [...(choices.rotating_employees || [])];
  $('wkndShiftPerson').checked = choices.shift_starting_person !== false;
}

function weekendFormChoices() {
  return {start_date:$('wkndStart').value, end_date:$('wkndEnd').value,
    excluded_dates:WKND_EXCLUDED.map(item => ({...item})), fixed_assignments:{...WKND_FIXED},
    rotating_employees:[...WKND_ROTATING_ORDER], shift_starting_person:$('wkndShiftPerson').checked};
}

$('wkndSaveChoicesBtn').onclick = async () => {
  if (!FOLDER_ID || FOLDER_LOADING || DELETION_BUSY || WEEKEND_CHOICES_SAVING) return;
  const revision = VIEW_REVISION, folderId = FOLDER_ID;
  WEEKEND_CHOICES_SAVING = true; renderFolderControls();
  $('wkndChoicesMsg').textContent = 'Saving weekend choices…';
  const response = await apiSend(`/api/folders/${folderId}/weekend-choices`, 'PUT', weekendFormChoices());
  if (revision !== VIEW_REVISION) return;
  WEEKEND_CHOICES_SAVING = false; renderFolderControls();
  if (!response?.ok) { $('wkndChoicesMsg').textContent = response?.data.error || 'Could not save. Please retry.'; return; }
  CONFIG.weekendChoices = response.data;
  $('wkndChoicesMsg').textContent = 'Weekend choices saved for this folder.';
};

function clearWeekendView() {
  WEEKEND_CHOICES_SAVING = false;
  $('wkndShiftPerson').checked = true;
  LAST_WKND_PREVIEW = null; WKND_EXCLUDED = []; WKND_ROTATING_ORDER = []; WKND_FIXED = {};
  for (const id of ["wkndMsg", "wkndChoicesMsg", "wkndPreviewArea", "wkndSavedSchedules", "wkndExcludedList"]) $(id).innerHTML = "";
  $("wkndFixedTable").querySelector("tbody").innerHTML = "";
  $("wkndRotatingTable").querySelector("tbody").innerHTML = "";
  for (const id of ["wkndStart", "wkndEnd", "newExclDate", "newExclLabel"]) $(id).value = "";
  $("wkndHelpPopup").style.display = "none";
}

const WKND_SHIFTS = [
  {key: "friday_evening", day: "Friday", time: "19:00-22:00"},
  {key: "saturday_morning", day: "Saturday", time: "07:00-12:00"},
  {key: "saturday_afternoon", day: "Saturday", time: "12:00-17:00"},
  {key: "saturday_evening", day: "Saturday", time: "17:00-22:00"},
  {key: "sunday_morning", day: "Sunday", time: "07:00-12:00"},
  {key: "sunday_afternoon", day: "Sunday", time: "12:00-17:00"},
];

function renderWeekendUI() {
  if (!FOLDER_ID || !CONFIG) return;
  const ids = new Set(EMPLOYEES.map(e => e.id));
  WKND_FIXED = Object.fromEntries(Object.entries(WKND_FIXED).filter(([,id]) => ids.has(id)));
  if (WKND_ROTATING_ORDER.length === 0 && EMPLOYEES.length > 0) {
    WKND_ROTATING_ORDER = EMPLOYEES.map(e => e.id);
  }

  // Render excluded dates
  const exclHtml = WKND_EXCLUDED.map((excl, i) =>
    `<div class="row" style="margin-bottom:4px;">
       <span>${escapeHtml(excl.date)} ${excl.label ? '('+escapeHtml(excl.label)+')' : ''}</span>
       <button class="secondary" onclick="removeWkndExcluded(${i})">Remove</button>
     </div>`
  ).join('');
  $("wkndExcludedList").innerHTML = exclHtml || "<p>None</p>";

  // Render fixed assignments
  const fixedTbody = $("wkndFixedTable").querySelector("tbody");
  fixedTbody.innerHTML = WKND_SHIFTS.map(s => {
    let options = `<option value="">-- Rotation --</option>`;
    EMPLOYEES.forEach(e => {
      const sel = (WKND_FIXED[s.key] == e.id) ? "selected" : "";
      options += `<option value="${e.id}" ${sel}>${escapeHtml(e.name)}</option>`;
    });
    return `<tr>
      <td>${s.key}</td>
      <td>${s.day}</td>
      <td>${s.time}</td>
      <td><select onchange="updateWkndFixed('${s.key}', this.value)">${options}</select></td>
    </tr>`;
  }).join('');

  // Render rotating pool
  const rotTbody = $("wkndRotatingTable").querySelector("tbody");
  // Filter out any IDs not in EMPLOYEES
  WKND_ROTATING_ORDER = WKND_ROTATING_ORDER.filter(id => EMPLOYEES.find(e => e.id == id));
  // Add any new employees
  EMPLOYEES.forEach(e => {
    if (!WKND_ROTATING_ORDER.includes(e.id)) WKND_ROTATING_ORDER.push(e.id);
  });

  rotTbody.innerHTML = WKND_ROTATING_ORDER.map((id, idx) => {
    const e = EMPLOYEES.find(emp => emp.id == id);
    if (!e) return '';
    const isFixed = Object.values(WKND_FIXED).includes(String(id)) || Object.values(WKND_FIXED).includes(id);
    const style = isFixed ? 'text-decoration: line-through; opacity: 0.6;' : '';
    const fixedNote = isFixed ? ' (Excluded: Fixed)' : '';

    return `<tr style="${style}">
      <td>${idx + 1}</td>
      <td>${escapeHtml(e.name)}${fixedNote}</td>
      <td>
        <button class="secondary" ${idx === 0 ? 'disabled' : ''} onclick="moveWkndRotating(${idx}, -1)">Up</button>
        <button class="secondary" ${idx === WKND_ROTATING_ORDER.length - 1 ? 'disabled' : ''} onclick="moveWkndRotating(${idx}, 1)">Down</button>
      </td>
    </tr>`;
  }).join('');

  return renderSavedWeekendSchedules();
}

window.removeWkndExcluded = (i) => {
  if (WEEKEND_CHOICES_SAVING || FOLDER_LOADING || DELETION_BUSY) return;
  $('wkndChoicesMsg').textContent = '';
  WKND_EXCLUDED.splice(i, 1);
  renderWeekendUI();
};

$("addExclBtn").onclick = () => {
  if (WEEKEND_CHOICES_SAVING || FOLDER_LOADING || DELETION_BUSY) return;
  $('wkndChoicesMsg').textContent = '';
  const date = $("newExclDate").value;
  const label = $("newExclLabel").value.trim();
  if (!date) return;
  WKND_EXCLUDED.push({date, label});
  $("newExclDate").value = "";
  $("newExclLabel").value = "";
  renderWeekendUI();
};

window.updateWkndFixed = (shiftKey, empId) => {
  if (WEEKEND_CHOICES_SAVING || FOLDER_LOADING || DELETION_BUSY) return;
  $('wkndChoicesMsg').textContent = '';
  if (empId) {
    WKND_FIXED[shiftKey] = parseInt(empId, 10);
  } else {
    delete WKND_FIXED[shiftKey];
  }
  renderWeekendUI();
};

window.moveWkndRotating = (idx, dir) => {
  if (WEEKEND_CHOICES_SAVING || FOLDER_LOADING || DELETION_BUSY) return;
  $('wkndChoicesMsg').textContent = '';
  if (idx + dir < 0 || idx + dir >= WKND_ROTATING_ORDER.length) return;
  const tmp = WKND_ROTATING_ORDER[idx];
  WKND_ROTATING_ORDER[idx] = WKND_ROTATING_ORDER[idx + dir];
  WKND_ROTATING_ORDER[idx + dir] = tmp;
  renderWeekendUI();
};

$("wkndHelpBtn").onclick = () => {
  const popup = $("wkndHelpPopup");
  popup.style.display = popup.style.display === "none" ? "block" : "none";
};
$("wkndHelpClose").onclick = () => $("wkndHelpPopup").style.display = "none";
for (const id of ['wkndStart','wkndEnd','wkndShiftPerson']) $(id).oninput = () => $('wkndChoicesMsg').textContent = '';

$("wkndGenerateBtn").onclick = async () => {
  if (!FOLDER_ID || FOLDER_LOADING || DELETION_BUSY || WEEKEND_CHOICES_SAVING) return;
  const revision = VIEW_REVISION;
  $("wkndGenerateBtn").disabled = true;
  $("wkndMsg").textContent = "Generating preview...";
  const config = weekendFormChoices();

  const res = await apiSend('/api/generate_weekend', 'POST', { config, folderId: FOLDER_ID });
  if (revision !== VIEW_REVISION) return;
  renderFolderControls();
  if (!res) return;
  if (!res.ok) { $("wkndMsg").textContent = res.data.error || "Preview failed."; return; }
  LAST_WKND_PREVIEW = res.data;
  $("wkndMsg").textContent = "Preview generated successfully.";
  renderWeekendPreview(res.data);
};

// Old saved previews did not contain signup_shifts. Rebuild only that section
// from their saved exclusions, never from the current form or roster.
function weekendSignupShifts(data) {
  if (Array.isArray(data.signup_shifts)) return data.signup_shifts;
  const cfg = data.config || {}, seen = new Set(), shifts = [];
  for (const exclusion of cfg.excluded_dates || []) {
    const date = typeof exclusion === 'string' ? exclusion : exclusion.date;
    if (!date || seen.has(date) || date < cfg.start_date || date > cfg.end_date) continue;
    seen.add(date);
    const day = new Date(`${date}T12:00:00Z`).getUTCDay();
    const dayName = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'][day];
    for (const s of WKND_SHIFTS.filter(s => s.day === dayName)) {
      const [start, end] = s.time.split('-').map(t => Number(t.split(':')[0]));
      shifts.push({date, shift:{key:s.key, start, end}, assigned:null, origin:'signup',
        label: typeof exclusion === 'string' ? '' : exclusion.label || ''});
    }
  }
  return shifts.sort((a,b) => a.date.localeCompare(b.date) || a.shift.start - b.shift.start);
}

function weekendShiftLabel(ds) {
  const date = new Date(`${ds.date}T12:00:00Z`);
  return `${date.toLocaleDateString('en-US', {weekday:'long', month:'short', day:'numeric', year:'numeric', timeZone:'UTC'})}: ${hourLabel(ds.shift.start)}–${hourLabel(ds.shift.end)}`;
}

function weekendScheduleModel(data) {
  const employees = data.employees || [], byId = new Map(employees.map(e => [e.id, e]));
  const assignments = [...(data.assignments || [])].sort((a,b) => a.date.localeCompare(b.date) || a.shift.start - b.shift.start);
  return {
    assignments: assignments.map(ds => ({...ds, name: byId.get(ds.assigned)?.name || 'Unfilled'})),
    people: employees.map(e => ({name:e.name, shifts:assignments.filter(ds => ds.assigned === e.id)})),
    signups: weekendSignupShifts(data)
  };
}

function renderFormattedWeekend(model, data) {
  const range = data.config ? `${data.config.start_date} – ${data.config.end_date}` : '';
  return `<div class="weekend-formatted">
    <h3 class="weekend-title">Weekend Shifts</h3><p class="weekend-range">${escapeHtml(range)}</p>
    <div class="weekend-columns">
      <table class="weekend-table weekend-assignments"><caption>Rotating &amp; fixed assignments</caption>
        <thead><tr><th scope="col">Weekend shift</th><th scope="col">Assignment</th></tr></thead>
        <tbody>${model.assignments.map(ds => `<tr class="${ds.assigned ? '' : 'weekend-unfilled'}">
          <td class="weekend-day-${new Date(`${ds.date}T12:00:00Z`).getUTCDay()}">${escapeHtml(weekendShiftLabel(ds))}</td>
          <td>${escapeHtml(ds.name)}<small>${escapeHtml(ds.origin || 'Unfilled')}${ds.unfilled_reason ? ' · '+escapeHtml(ds.unfilled_reason) : ''}</small></td>
        </tr>`).join('') || '<tr><td colspan="2">No rotating or fixed shifts in this range.</td></tr>'}</tbody></table>
      <table class="weekend-table weekend-people"><caption>Weekend shifts by employee</caption>
        <thead><tr><th scope="col">Employee</th><th scope="col">Assigned shifts</th></tr></thead>
        <tbody>${model.people.map(e => `<tr><th scope="row">${escapeHtml(e.name)}</th>
          <td>${e.shifts.map(ds => escapeHtml(weekendShiftLabel(ds))).join('<br>') || 'No assigned shifts'}</td></tr>`).join('') || '<tr><td colspan="2">No employees.</td></tr>'}</tbody></table>
    </div>
    <h3>Voluntary signup shifts — additional hours</h3>
    <p>Excluded from fixed assignments and rotation. Supervisors can ask employees who wants to work these additional hours.</p>
    <table class="weekend-table weekend-signups"><thead><tr><th scope="col">Weekend shift</th><th scope="col">Occasion / note</th><th scope="col">Employee signup</th></tr></thead>
      <tbody>${model.signups.map(ds => `<tr><td>${escapeHtml(weekendShiftLabel(ds))}</td><td>${escapeHtml(ds.label || 'Excluded date')}</td><td>Open for signup</td></tr>`).join('') || '<tr><td colspan="3">No excluded weekend shifts in this range.</td></tr>'}</tbody></table>
  </div>`;
}

function buildWeekendWorkbook(data) {
  const model = weekendScheduleModel(data), wb = new ExcelJS.Workbook();
  const border = {top:{style:'thin'},bottom:{style:'thin'},left:{style:'thin'},right:{style:'thin'}};
  const colors = {title:'FFE99B9B', header:'FFD9D9D9', employee:'FFF4CCCC', alternate:'FFFCE5DC',
    friday:'FFCFE2F3', saturday:'FFE2DDF2', sunday:'FFFFF2CC', assigned:'FFD9EAD3', unfilled:'FFFFDDDD'};
  const fill = color => ({type:'pattern',pattern:'solid',fgColor:{argb:color}});
  function cell(ws, row, col, value, color, bold = false) {
    const c = ws.getCell(row,col); c.value = value;
    c.font = {name:'Calibri',size:11,bold}; c.border = border;
    c.alignment = {vertical:'middle',wrapText:true};
    if (color) c.fill = fill(color);
    return c;
  }
  function title(ws, text, columns) {
    ws.mergeCells(1,1,1,columns);
    const c = cell(ws,1,1,text,colors.title,true);
    c.font = {name:'Calibri',size:22,bold:true}; c.alignment = {horizontal:'center',vertical:'middle'};
    ws.getRow(1).height = 38;
    ws.mergeCells(2,1,2,columns);
    cell(ws,2,1,data.config ? `${data.config.start_date} – ${data.config.end_date}` : '',null);
    ws.views = [{state:'frozen',ySplit:4}];
    ws.pageSetup = {paperSize:9,orientation:'landscape',fitToPage:true,fitToWidth:1,fitToHeight:0,printTitlesRow:'1:4'};
  }
  const ws = wb.addWorksheet('Weekend Shifts');
  [43,30,4,26,58].forEach((width,i) => ws.getColumn(i+1).width = width);
  title(ws,'Weekend Shifts',5);
  ['Weekend shift','Assignment',null,'Employee','Assigned shifts'].forEach((v,i) => {
    if (v) cell(ws,4,i+1,v,colors.header,true);
  });
  // Excel caps row height at 409 points. Split long semester lists into
  // continuation rows so every fixed occurrence remains readable in print.
  const peopleRows = model.people.flatMap((e,index) => {
    const rows = [];
    for (let offset=0; offset<Math.max(e.shifts.length,1); offset+=10) {
      rows.push({...e, name:e.name+(offset ? ' (continued)' : ''), shifts:e.shifts.slice(offset,offset+10), index});
    }
    return rows;
  });
  model.assignments.forEach((ds,i) => {
    const row = i+5;
      const day = new Date(`${ds.date}T12:00:00Z`).getUTCDay();
      cell(ws,row,1,weekendShiftLabel(ds),day === 5 ? colors.friday : day === 6 ? colors.saturday : colors.sunday);
      cell(ws,row,2,`${ds.name}\n${ds.origin || 'Unfilled'}${ds.unfilled_reason ? ' · '+ds.unfilled_reason : ''}`,ds.assigned ? colors.assigned : colors.unfilled);
  });
  let personRow = 5;
  peopleRows.forEach(e => {
    const span = Math.max(1,Math.ceil(e.shifts.length*24/60));
    if (span > 1) {
      ws.mergeCells(personRow,4,personRow+span-1,4);
      ws.mergeCells(personRow,5,personRow+span-1,5);
    }
    cell(ws,personRow,4,e.name,colors.employee);
    cell(ws,personRow,5,e.shifts.map(weekendShiftLabel).join('\n') || 'No assigned shifts',e.index%2 === 0 ? colors.alternate : 'FFFFFFFF');
    personRow += span;
  });
  for (let row=5; row<=Math.max(ws.rowCount,personRow-1); row++) ws.getRow(row).height = 60;
  const signup = wb.addWorksheet('Signup Shifts');
  [46,34,30].forEach((width,i) => signup.getColumn(i+1).width = width);
  title(signup,'Voluntary Signup Shifts',3);
  signup.mergeCells(3,1,3,3);
  cell(signup,3,1,'Additional hours excluded from fixed assignments and rotation. Supervisors can collect employee signups below.',null);
  signup.getRow(3).height = 36;
  ['Weekend shift','Occasion / note','Employee signup'].forEach((v,i) => cell(signup,4,i+1,v,colors.header,true));
  model.signups.forEach((ds,i) => {
    cell(signup,i+5,1,weekendShiftLabel(ds),colors.sunday);
    cell(signup,i+5,2,ds.label || 'Excluded date',colors.sunday);
    cell(signup,i+5,3,null,'FFFFFFFF'); signup.getRow(i+5).height = 44;
  });
  if (!model.signups.length) cell(signup,5,1,'No excluded weekend shifts in this range.',null);
  ws.pageSetup.printArea = `A1:E${Math.max(ws.rowCount,5)}`;
  signup.pageSetup.printArea = `A1:C${Math.max(signup.rowCount,5)}`;
  return wb;
}

async function downloadWeekendExcel() {
  if (!LAST_WKND_PREVIEW || !FOLDER_ID || DELETION_BUSY) return;
  const revision = VIEW_REVISION, data = LAST_WKND_PREVIEW, button = $('wkndDownloadBtn');
  button.disabled = true;
  try {
    const buf = await buildWeekendWorkbook(data).xlsx.writeBuffer();
    if (revision !== VIEW_REVISION || LAST_WKND_PREVIEW !== data || DELETION_BUSY) return;
    const url = URL.createObjectURL(new Blob([buf], {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
    const a = document.createElement('a'); a.href = url;
    a.download = `Weekend_Schedule${data.config ? '_'+data.config.start_date+'_'+data.config.end_date : ''}.xlsx`;
    a.click(); URL.revokeObjectURL(url);
  } catch (_) {
    if (revision === VIEW_REVISION) $('wkndMsg').textContent = 'Excel download failed. Please retry; if the Excel library did not load, refresh the page.';
  } finally {
    if (revision === VIEW_REVISION && LAST_WKND_PREVIEW === data) button.disabled = false;
  }
}

function renderWeekendPreview(data) {
  const revision = VIEW_REVISION, folderId = FOLDER_ID;
  LAST_WKND_PREVIEW = data;
  const empById = {};
  data.employees.forEach(e => { empById[e.id] = e; });

  let html = `<div class="row">${data.folderVersion ? '<button id="wkndSaveBtn">Save schedule</button>' : ''}
    <button id="wkndDownloadBtn" class="secondary">Download .xlsx</button></div>`;
  html += renderFormattedWeekend(weekendScheduleModel(data), data);
  html += '<details class="weekend-details"><summary>Assignment details and totals</summary>';

  // Table of assignments
  html += `<h3>Assignments</h3><table class="data-table">
    <thead><tr><th>Friday (Weekend ID)</th><th>Date</th><th>Shift</th><th>Assigned</th><th>Type</th><th>Note</th></tr></thead><tbody>`;

  data.assignments.forEach(ds => {
    const empName = ds.assigned ? escapeHtml(empById[ds.assigned]?.name || 'Unknown employee') : "<strong>Unfilled</strong>";
    const origin = ds.origin || "";
    const note = ds.unfilled_reason || "";
    const isUnfilled = !ds.assigned ? "style='background-color:#ffeeee;'" : "";
    html += `<tr ${isUnfilled}>
      <td>${escapeHtml(ds.friday)}</td>
      <td>${escapeHtml(ds.date)}</td>
      <td>${escapeHtml(ds.shift.key)}</td>
      <td>${empName}</td>
      <td>${escapeHtml(origin)}</td>
      <td>${escapeHtml(note)}</td>
    </tr>`;
  });
  html += `</tbody></table>`;

  // Summary
  html += `<h3>Summary</h3><table class="data-table">
    <thead><tr><th>Employee</th><th>Fixed Shifts</th><th>Rotating Shifts</th><th>Total Shifts</th><th>Notes</th></tr></thead><tbody>`;

  data.employees.forEach(e => {
    let fixed = 0;
    let rotating = data.rotating_counts[e.id] || 0;
    data.assignments.forEach(ds => {
      if (ds.assigned === e.id && ds.origin === "fixed") fixed++;
    });

    let total = fixed + rotating;
    let notes = [];
    if (total === 0) {
      if (Object.values(data.config?.fixed_assignments || {}).includes(e.id)) notes.push("Fixed occurrences were excluded/outside range.");
      else if (!data.effective_pool.includes(e.id)) notes.push("Cannot cover any remaining rotating shift in full or not in pool.");
      else notes.push("No assignment before semester ended; limited openings.");
    }

    html += `<tr>
      <td>${escapeHtml(e.name)}</td>
      <td>${fixed}</td>
      <td>${rotating}</td>
      <td>${total}</td>
      <td>${notes.join(" ")}</td>
    </tr>`;
  });
  html += `</tbody></table></details>`;

  $("wkndPreviewArea").innerHTML = `<div class="scroll-x">${html}</div>`;
  $('wkndDownloadBtn').onclick = downloadWeekendExcel;
  if (!$("wkndSaveBtn")) return;
  $("wkndSaveBtn").onclick = async () => {
    if (revision !== VIEW_REVISION || folderId !== FOLDER_ID) return;
    const button = $("wkndSaveBtn"); button.disabled = true;
    const res = await apiSend('/api/save_weekend', 'POST', { folderId, snapshot: data });
    if (revision !== VIEW_REVISION) return;
    button.disabled = false;
    if (!res) return;
    if (res.ok && res.data.savedScheduleId) {
      $("wkndMsg").textContent = "Saved successfully!";
      await renderSavedWeekendSchedules();
    } else { $("wkndMsg").textContent = res.data.error || "Saving failed. Please retry."; }
  };
}

async function renderSavedWeekendSchedules() {
  if (!FOLDER_ID) return;
  const schedules = await apiGet(`/api/folders/${FOLDER_ID}/weekend_schedules`);
  if (!schedules) return;

  $("wkndSavedSchedules").innerHTML = schedules.map(s => {
    const d = new Date(s.createdAt);
    return `<div class="row card" style="margin-bottom:8px; display:flex; justify-content:space-between;">
      <span>Saved on ${d.toLocaleString()}</span>
      <div>
        <button class="secondary" onclick="loadSavedWeekend(${s.id})">View</button>
        <button class="secondary" onclick="deleteSavedWeekend(${s.id})">Delete</button>
      </div>
    </div>`;
  }).join('') || 'No saved weekend schedules yet.';
}

window.loadSavedWeekend = async (id) => {
  const data = await apiGet(`/api/folders/${FOLDER_ID}/weekend_schedules/${id}`);
  if (data) renderWeekendPreview(data);
};

window.deleteSavedWeekend = async (id) => {
  if (confirm("Delete this saved weekend schedule?")) {
    await apiSend(`/api/folders/${FOLDER_ID}/weekend_schedules/${id}`, 'DELETE', {confirm: true});
    renderSavedWeekendSchedules();
  }
};
