let FOLDERS = [], FOLDER_ID = null, ACTIVE_FOLDER = null;
let SELECTED = new Set(), OVERVIEW = null, PINNED = null, PREVIEWED = null;
let VIEW_REVISION = 0;
let OVERVIEW_REQUEST = 0;
const PERMISSION_DRAFTS = new Map();
let RECOVERING_CONTENT = false;

async function recoverRemovedContent() {
  if (RECOVERING_CONTENT) return;
  RECOVERING_CONTENT = true;
  try {
    clearFolderView();
    await loadFolders();
  } finally { RECOVERING_CONTENT = false; }
}

function $(id) { return document.getElementById(id); }

function folderQuery() { return `folderId=${FOLDER_ID}`; }
function selectionQuery() {
  return `${folderQuery()}&selection=1` + [...SELECTED].map(id => `&employeeId=${id}`).join("");
}
function clearResult() {
  LAST_RESULT = null;
  for (const id of ["generateMsg", "scheduleOutput", "fairnessOutput"]) $(id).innerHTML = "";
  $("downloadBtn").style.display = $("regenerateBtn").style.display = "none";
}
function renderFolderControls() {
  $("folderSelect").innerHTML = FOLDERS.length ? FOLDERS.map(f => `<option value="${f.id}" ${f.id === FOLDER_ID ? "selected" : ""}>${escapeHtml(f.name)}${f.archived ? " (archived)" : ""}</option>`).join("") : '<option value="">No folders yet</option>';
  $("folderSelect").disabled = !FOLDERS.length;
  for (const id of ["renameFolderBtn", "activateFolderBtn", "stopFolderBtn", "archiveFolderBtn", "deleteFolderBtn", "generateBtn", "regenerateBtn", "wkndGenerateBtn"]) {
    $(id).disabled = !FOLDER_ID || DELETION_BUSY;
  }
  $("downloadBtn").disabled = DELETION_BUSY || !FOLDER_ID;
  const active = FOLDERS.find(f => f.id === ACTIVE_FOLDER);
  $("folderStatus").textContent = active ? `Accepting submissions: ${active.name}` : "No folder is accepting submissions.";
}
function clearFolderView() {
  if (typeof clearCollectionView === "function") clearCollectionView();
  PERMISSION_DRAFTS.clear();
  VIEW_REVISION++;
  PINNED = PREVIEWED = null;
  SELECTED = new Set(); OVERVIEW = null; LAST_DIAG = null;
  clearResult(); clearWeekendView(); closeEditAvailabilityModal();
  editAvailState = {};
  for (const id of ["diagArea", "overviewArea", "generatorSelection", "savedSchedules", "diagBadge", "editAvailGrid", "editAvailMsg", "editAvailName"]) $(id).innerHTML = "";
  $("editAvailComment").value = "";
}
async function loadFolders(preferred = FOLDER_ID) {
  const data = await apiGet("/api/folders");
  if (!data) return;
  FOLDERS = data.folders; ACTIVE_FOLDER = data.activeFolderId;
  FOLDER_ID = FOLDERS.some(f => f.id === preferred) ? preferred : (FOLDERS[0]?.id ?? null);
  renderFolderControls();
  await changeFolder();
}
async function changeFolder() {
  cancelFolderDeletion();
  FOLDER_ID = $("folderSelect").value ? Number($("folderSelect").value) : null;
  clearFolderView(); renderFolderControls();
  const revision = VIEW_REVISION;
  if (!FOLDER_ID) {
    $("overviewArea").textContent = "No folders yet. Create a folder to collect availability.";
    $("diagArea").textContent = "Create or select a folder to check availability.";
    await refreshStaffingPlan();
    return;
  }
  await renderOverview(true);
  if (revision !== VIEW_REVISION) return;
  await refreshDiagnostics();
  if (revision !== VIEW_REVISION) return;
  await loadSavedSchedules();
  if (revision !== VIEW_REVISION) return;
  await renderSavedWeekendSchedules();
  if (revision !== VIEW_REVISION) return;
  await renderCodes();
}
// Supervisor collection windows are independent of weekday staffing settings.
const SUPERVISOR_AVAILABILITY_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function supervisorAvailabilityHours() {
  return Array.from({length: 15}, (_, i) => i + 7);
}
function supervisorAvailabilityClosed(day, hour) {
  return hour < 7 || hour >= (day === "Sun" ? 17 : 22);
}
function validAvailability(av) {
  const valid = new Set(SUPERVISOR_AVAILABILITY_DAYS.flatMap(d => supervisorAvailabilityHours()
    .filter(h => !supervisorAvailabilityClosed(d, h)).map(h => cellKey(d, h))));
  return Object.fromEntries(Object.entries(av || {}).filter(([k,v]) => valid.has(k) && Number(v) > 0));
}
async function renderFolderOverview(reset = false) {
  if (!FOLDER_ID) return;
  const revision = VIEW_REVISION;
  const requestId = ++OVERVIEW_REQUEST;
  const data = await apiGet(`/api/availability?${folderQuery()}`);
  if (!data || revision !== VIEW_REVISION || requestId !== OVERVIEW_REQUEST || !Array.isArray(data.employees)) return;
  OVERVIEW = data;
  const submitted = data.employees.filter(e => Object.hasOwn(data.availability, e.name));
  submitted.sort((a,b) => Object.keys(validAvailability(data.availability[b.name])).length - Object.keys(validAvailability(data.availability[a.name])).length || a.name.localeCompare(b.name));
  const ids = new Set(submitted.map(e => e.id));
  if (PINNED !== null && !ids.has(PINNED)) PINNED = null;
  if (PREVIEWED !== null && !ids.has(PREVIEWED)) PREVIEWED = null;
  for (const key of PERMISSION_DRAFTS.keys()) {
    if (!ids.has(Number(key.split(":")[1]))) PERMISSION_DRAFTS.delete(key);
  }
  if (editAvailEmployee && !ids.has(editAvailEmployee.id)) closeEditAvailabilityModal();
  SELECTED = reset ? ids : new Set([...SELECTED].filter(id => ids.has(id)));
  $("generatorSelection").innerHTML = `<p id="selectedCount"></p>` + submitted.map(e => `<label class="selection-person"><input type="checkbox" data-select="${e.id}" ${SELECTED.has(e.id) ? "checked" : ""}> ${escapeHtml(e.name)}</label>`).join("");
  $("generatorSelection").querySelectorAll("[data-select]").forEach(box => box.onchange = () => {
    box.checked ? SELECTED.add(Number(box.dataset.select)) : SELECTED.delete(Number(box.dataset.select));
    updateSelectionCount(); refreshDiagnostics();
  });
  updateSelectionCount();
  $("overviewArea").innerHTML = `<p>Hover or focus to preview. Click to pin; click again, outside, or press Escape to clear.</p>
    <div class="availability-viewer"><div class="viewer-list">${submitted.map(e => `<button class="secondary viewer-person" data-person="${e.id}" aria-pressed="false">${escapeHtml(e.name)} — ${Object.keys(validAvailability(data.availability[e.name])).length} hrs</button>`).join("")}</div>
    <div><p id="viewerCaption" role="status"></p><p id="viewerComment"></p><div id="viewerConsent" role="status"></div><div id="viewerPermissionControls"></div><div class="scroll-x" id="viewerGrid"></div></div></div>
    <p>Missing submissions: ${data.missing.length ? data.missing.map(escapeHtml).join(", ") : "None"}.</p>`;
  $("overviewArea").querySelectorAll("[data-person]").forEach(btn => {
    const id = Number(btn.dataset.person);
    // A pinned viewer does not change on hover/focus. Keep its Edit button stable
    // when focus leaves the employee list, so a real pointer click can reach it.
    btn.onmouseenter = btn.onfocus = () => { PREVIEWED = id; if (PINNED === null) drawViewer(); };
    btn.onmouseleave = btn.onblur = () => { PREVIEWED = null; if (PINNED === null) drawViewer(); };
    btn.onclick = () => { PINNED = PINNED === id ? null : id; PREVIEWED = null; drawViewer(); };
  });
  drawViewer();
}
function updateSelectionCount() {
  $("selectedCount").textContent = `${SELECTED.size} employees selected. Excluding someone keeps their submission saved.`;
}
function drawViewer() {
  if (!OVERVIEW || !$("viewerGrid")) return;
  const id = PINNED ?? PREVIEWED;
  const employee = OVERVIEW.employees.find(e => e.id === id);
  const data = employee ? [validAvailability(OVERVIEW.availability[employee.name])] : Object.values(OVERVIEW.availability).map(validAvailability);
  $("viewerCaption").innerHTML = employee
      ? `${PINNED ? "Pinned" : "Preview"}: ${escapeHtml(employee.name)} · Submitted ${escapeHtml(new Date(OVERVIEW.submittedAt[employee.name]).toLocaleString())} ` +
        (PINNED ? `<button class="secondary" id="openEditAvailBtn" style="margin-left: 8px; padding: 2px 8px; font-size: 0.85rem;">Edit</button>` : "")
      : "All submissions: available employee count per hour";
  $("viewerComment").textContent = employee ? OVERVIEW.comments[employee.name] || "No comment" : "";
  $("viewerConsent").textContent = "";
  if (employee) {
    const consent = OVERVIEW.submissions?.find(s => s.employeeId === employee.id)?.consent;
    const lines = [`Weekly hours: ${employee.minHours}–${employee.maxHours}. Lead: ${employee.isLead ? "yes" : "no"}.`];
    for (const [label, recorded, effective, kind] of [["openings", "allowExtraOpenings", "effectiveOpenings", "openings"], ["closings", "allowExtraClosings", "effectiveClosings", "closings"]]) {
      let status = "not opted in";
      if (consent?.[recorded]) {
        status = "opted in. " + (consent.reconfirmationNeeded ? "Reconfirmation required after limits or shift blocks changed." :
          !consent.enabled ? "Recorded opt-in; extra preferred shifts are currently disabled." :
          !consent.candidates[kind].length ? "No qualifying preferred block currently." : consent[effective] ? "Eligible; assignments are optional and not guaranteed." : "Not currently effective.");
      }
      lines.push(`Additional ${label}: ${status}`);
    }
    if (consent) lines.push(`Limits: ${consent.caps.openings} openings, ${consent.caps.closings} closings, ${consent.caps.combined} combined. Fully preferred candidates: ${consent.candidates.openings.length} openings, ${consent.candidates.closings.length} closings.`);
    $("viewerConsent").textContent = lines.join(" ");
  }
  $("overviewArea").querySelectorAll("[data-person]").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.person) === PINNED)));
  renderPermissionControls(employee);
  $("viewerGrid").innerHTML = `<table class="data-table"><thead><tr><th>Time</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d => `<th>${d}</th>`).join("")}</tr></thead><tbody>${supervisorAvailabilityHours().map(h => `<tr><th>${blockLabel(h)}</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d => {
    if (supervisorAvailabilityClosed(d, h)) return '<td class="closed" aria-label="Outside collection hours">—</td>';
    const key = `${d}_${String(h).padStart(2,"0")}`;
    const count = data.filter(av => Number(av[key]) > 0).length;
    const level = employee ? Number(data[0][key] || 0) : count > 0 ? 1 : 0;
    return `<td class="viewer-level-${level}">${employee ? level === 2 ? "Preferred" : level ? "Available" : "—" : count}</td>`;
  }).join("")}</tr>`).join("")}</tbody></table>`;

  const editBtn = $("openEditAvailBtn");
  if (editBtn) {
    editBtn.onclick = () => openEditAvailabilityModal(employee);
  }
}

function renderPermissionControls(employee) {
  const host = $("viewerPermissionControls");
  if (!host) return;
  host.innerHTML = "";
  const submission = OVERVIEW.submissions?.find(s => s.employeeId === employee?.id);
  if (!employee || PINNED !== employee.id || !submission || !OVERVIEW.boundaryContext) return;
  const key = `${FOLDER_ID}:${employee.id}`;
  let draft = PERMISSION_DRAFTS.get(key);
  if (!draft || (!draft.dirty && !draft.busy)) {
    draft = {openings: submission.consent.allowExtraOpenings, closings: submission.consent.allowExtraClosings,
      version: submission.permissionVersion, context: OVERVIEW.boundaryContext.token,
      dirty: false, busy: false, message: draft?.message || "", conflict: false};
    PERMISSION_DRAFTS.set(key, draft);
  }
  host.innerHTML = `<fieldset><legend>Additional-shift permissions</legend>
    <p id="permissionHelp">Permission allows optional assignments when preferred availability and all scheduling constraints qualify. Shift-length changes keep permission recorded and recalculate eligible days. Saving explicitly confirms these choices against the current caps and boundaries. Employees can still change their own choices.</p>
    <label style="display:block"><input id="supervisorExtraOpenings" type="checkbox" aria-describedby="permissionHelp" ${draft.openings ? "checked" : ""} ${draft.busy ? "disabled" : ""}> Allow additional fully preferred opening shifts</label>
    <label style="display:block"><input id="supervisorExtraClosings" type="checkbox" aria-describedby="permissionHelp" ${draft.closings ? "checked" : ""} ${draft.busy ? "disabled" : ""}> Allow additional fully preferred closing shifts</label>
    <p>Exception feature: ${submission.consent.enabled ? "enabled" : "disabled"}. ${submission.consent.reconfirmationNeeded ? "Reconfirmation required; an explicit save confirms the selected choices." : "No reconfirmation required."}</p>
    <button id="saveBoundaryPermissions" ${draft.busy ? "disabled" : ""}>Save additional-shift permissions</button>
    <button id="reloadBoundaryPermissions" class="secondary" ${draft.conflict ? "" : "hidden"}>Reload saved permissions (discard draft)</button>
    <p id="permissionSaveStatus" role="status">${escapeHtml(draft.message)}</p></fieldset>`;
  for (const [id, field] of [["supervisorExtraOpenings", "openings"], ["supervisorExtraClosings", "closings"]]) {
    $(id).onchange = event => { draft[field] = event.target.checked; draft.dirty = true; };
  }
  $("reloadBoundaryPermissions").onclick = async () => {
    PERMISSION_DRAFTS.delete(key);
    await renderOverview();
  };
  $("saveBoundaryPermissions").onclick = async () => {
    const revision = VIEW_REVISION, folderId = FOLDER_ID;
    draft.busy = true; draft.message = "Saving…";
    renderPermissionControls(employee);
    const response = await apiSend("/api/admin/boundary-permissions", "PUT", {
      employeeId: employee.id, folderId, permissionVersion: draft.version, consentContext: draft.context,
      allowExtraOpenings: draft.openings, allowExtraClosings: draft.closings
    });
    if (revision !== VIEW_REVISION || folderId !== FOLDER_ID || !response) return;
    draft.busy = false;
    if (response.ok) {
      draft.dirty = false; draft.message = "Additional-shift permissions saved."; draft.conflict = false;
      OVERVIEW.submissions = OVERVIEW.submissions.map(s => s.employeeId === employee.id ? response.data.submission : s);
      drawViewer();
      await renderOverview();
    } else {
      draft.dirty = true; draft.conflict = response.status === 409;
      draft.message = response.data.error || "Could not save permissions. Please retry.";
      renderPermissionControls(OVERVIEW.employees.find(e => e.id === (PINNED ?? PREVIEWED)));
    }
  };
}

// --- Edit Availability logic ---
let editAvailState = {};
let editAvailEmployee = null;
let editAvailPainting = false;
let editAvailMode = 1;
let editAvailPaintedThisDrag = new Set();

function openEditAvailabilityModal(employee) {
  editAvailEmployee = employee;
  editAvailState = Object.assign({}, validAvailability(OVERVIEW.availability[employee.name]));
  $("editAvailName").textContent = employee.name;
  $("editAvailComment").value = OVERVIEW.comments[employee.name] || "";
  $("editAvailMsg").textContent = "";

  $("editAvailabilityModal").style.display = "block";
  $("editAvailabilityOverlay").style.display = "block";

  renderEditAvailGrid();
  bindEditAvailGrid();
}

function closeEditAvailabilityModal() {
  $("editAvailabilityModal").style.display = "none";
  $("editAvailabilityOverlay").style.display = "none";
  editAvailEmployee = null;
}

function cellKey(day, hour) {
  return `${day}_${String(hour).padStart(2, "0")}`;
}

function editAvailCellLabel(day, hour, level, closed = false) {
  return `${day} ${blockLabel(hour)}: ${closed ? "Outside collection hours" : level === 2 ? "Preferred" : level ? "Available" : "Unavailable"}`;
}

function renderEditAvailGrid() {
  const grid = $("editAvailGrid");
  grid.style.setProperty("--cols", SUPERVISOR_AVAILABILITY_DAYS.length);
  const parts = ['<div class="wg-corner"></div>'];

  for (const day of SUPERVISOR_AVAILABILITY_DAYS) {
    parts.push(`<button type="button" class="wg-daylabel" data-fillday="${day}" title="Fill or clear ${day}">${escapeHtml(day)}</button>`);
  }

  for (const h of supervisorAvailabilityHours()) {
    parts.push(`<div class="wg-timelabel">${blockLabel(h)}</div>`);
    for (const day of SUPERVISOR_AVAILABILITY_DAYS) {
      const key = cellKey(day, h);
      const closed = supervisorAvailabilityClosed(day, h);
      const level = closed ? 0 : (editAvailState[key] || 0);
      parts.push(
        `<button type="button" class="wg-cell" data-key="${key}" data-day="${day}" data-hour="${h}" data-level="${level}"
           ${closed ? 'data-closed="1" disabled' : ""}
           aria-pressed="${Boolean(level)}" aria-label="${editAvailCellLabel(day, h, level, closed)}"></button>`
      );
    }
  }
  grid.innerHTML = parts.join("");
}

function editAvailPaint(cell) {
  if (!cell || cell.hasAttribute("data-closed")) return;
  const key = cell.dataset.key;
  if (editAvailPaintedThisDrag.has(key)) return;
  editAvailPaintedThisDrag.add(key);
  if (editAvailMode === 0) {
    delete editAvailState[key];
  } else {
    editAvailState[key] = editAvailMode;
  }
  cell.dataset.level = editAvailState[key] || 0;
  cell.setAttribute("aria-pressed", String(Boolean(editAvailState[key])));
  cell.setAttribute("aria-label", editAvailCellLabel(cell.dataset.day, Number(cell.dataset.hour), editAvailState[key] || 0));
}

function editAvailToggleDay(day) {
  const open = supervisorAvailabilityHours().filter(h => !supervisorAvailabilityClosed(day, h));
  const allSet = open.every(h => (editAvailState[cellKey(day, h)] || 0) >= 1);
  for (const h of open) {
    const key = cellKey(day, h);
    if (allSet) delete editAvailState[key];
    else editAvailState[key] = Math.max(editAvailState[key] || 0, 1);
  }
  renderEditAvailGrid();
}

function bindEditAvailGrid() {
  const grid = $("editAvailGrid");

  const downHandler = (e) => {
    const fill = e.target.closest("[data-fillday]");
    if (fill) { editAvailToggleDay(fill.dataset.fillday); return; }
    const cell = e.target.closest(".wg-cell");
    if (!cell) return;
    e.preventDefault();
    editAvailPainting = true;
    editAvailPaintedThisDrag = new Set();
    try { grid.setPointerCapture(e.pointerId); } catch (_) {}
    editAvailPaint(cell);
  };

  const moveHandler = (e) => {
    if (!editAvailPainting) return;
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (el && el.classList && el.classList.contains("wg-cell")) editAvailPaint(el);
  };

  const clickHandler = e => {
    if (e.detail !== 0) return;
    const fill = e.target.closest("[data-fillday]");
    if (fill) { editAvailToggleDay(fill.dataset.fillday); return; }
    editAvailPaintedThisDrag = new Set(); editAvailPaint(e.target.closest(".wg-cell"));
  };

  const stopHandler = () => { editAvailPainting = false; editAvailPaintedThisDrag = new Set(); };

  grid.onpointerdown = downHandler;
  grid.onpointermove = moveHandler;
  grid.onclick = clickHandler;
  grid.onpointerup = stopHandler;
  grid.onpointercancel = stopHandler;
}

document.querySelectorAll("#editAvailabilityModal .paint-mode").forEach(b => {
  b.onclick = () => {
    editAvailMode = Number(b.dataset.mode);
    document.querySelectorAll("#editAvailabilityModal .paint-mode").forEach(btn =>
      btn.setAttribute("aria-pressed", String(Number(btn.dataset.mode) === editAvailMode))
    );
  };
});

$("editAvailClearBtn").onclick = () => {
  if (confirm("Clear every hour for this employee?")) {
    editAvailState = {};
    renderEditAvailGrid();
  }
};

$("editAvailCancelBtn").onclick = closeEditAvailabilityModal;
$("editAvailabilityOverlay").onclick = closeEditAvailabilityModal;

$("editAvailSaveBtn").onclick = async () => {
  if (!editAvailEmployee) return;
  $("editAvailSaveBtn").disabled = true;
  $("editAvailMsg").innerHTML = "Saving...";

  const payload = {
    employeeId: editAvailEmployee.id,
    folderId: FOLDER_ID,
    availability: editAvailState,
    comment: $("editAvailComment").value
  };

  const revision = VIEW_REVISION;
  const r = await apiSend("/api/admin/availability", "PUT", payload);
  $("editAvailSaveBtn").disabled = false;
  if (revision !== VIEW_REVISION) return;
  if (!r) {
    $("editAvailMsg").innerHTML = `<span style="color:var(--danger)">Network error saving availability.</span>`;
    return;
  }
  if (!r.ok) {
    $("editAvailMsg").innerHTML = `<span style="color:var(--danger)">${escapeHtml(r.data.error || "Failed to save.")}</span>`;
    return;
  }

  closeEditAvailabilityModal();
  await renderFolderOverview(false);
  await refreshDiagnostics();
};
// ---------------------------------

async function loadSavedSchedules() {
  if (!FOLDER_ID) return;
  const revision = VIEW_REVISION, folderId = FOLDER_ID;
  const rows = await apiGet(`/api/folders/${folderId}/schedules`);
  if (!rows || revision !== VIEW_REVISION) return;
  $("savedSchedules").innerHTML = rows.length ? rows.map(s => `<div class="row"><span>${escapeHtml(new Date(s.createdAt).toLocaleString())}</span><button data-open="${s.id}" class="secondary">Open</button><button data-delete="${s.id}" class="danger">Delete</button></div>`).join("") : "No saved schedules yet.";
  $("savedSchedules").querySelectorAll("[data-open]").forEach(btn => btn.onclick = async () => {
    if (revision !== VIEW_REVISION || folderId !== FOLDER_ID) return;
    const saved = await apiGet(`/api/folders/${folderId}/schedules/${btn.dataset.open}`);
    if (!saved || revision !== VIEW_REVISION) return;
    LAST_RESULT = saved.result;
    $("scheduleOutput").innerHTML = renderPrintableTable(LAST_RESULT.schedule, LAST_RESULT.config);
    $("fairnessOutput").innerHTML = fairnessTable(LAST_RESULT.fairness || []) + boundarySummary(LAST_RESULT);
    $("generateMsg").textContent = "Showing a saved schedule with its original settings and availability.";
    $("downloadBtn").style.display = "inline-block";
  });
  $("savedSchedules").querySelectorAll("[data-delete]").forEach(btn => btn.onclick = async () => {
    if (revision !== VIEW_REVISION || folderId !== FOLDER_ID) return;
    if (!confirm("Delete this saved schedule? Employee availability will be kept.")) return;
    const r = await apiSend(`/api/folders/${folderId}/schedules/${btn.dataset.delete}`, "DELETE", {confirm:true});
    if (r?.ok) { clearResult(); await loadSavedSchedules(); }
  });
}
function setupFolders() {
  $("folderSelect").onchange = changeFolder;
  $("deleteFolderBtn").onclick = openFolderDeletion;
  $("deleteFolderCancelBtn").onclick = cancelFolderDeletion;
  $("deleteFolderConfirmBtn").onclick = confirmFolderDeletion;
  $("deleteFolderConfirmation").oninput = updateDeletionConfirmation;
  $("deleteFolderDialog").addEventListener("cancel", event => { event.preventDefault(); cancelFolderDeletion(); });
  $("logoutBtn").onclick = logout;
  const update = async data => {
    const r = await apiSend(`/api/folders/${FOLDER_ID}`, "PATCH", data);
    if (!r) return;
    if (!r.ok) { $("folderMsg").textContent = r.data.error; return; }
    await loadFolders();
  };
  $("createFolderBtn").onclick = async () => {
    const r = await apiSend("/api/folders", "POST", {name:$("newFolderName").value, activate:$("activateNewFolder").checked});
    if (!r) return;
    if (!r.ok) { $("folderMsg").textContent = r.data.error; return; }
    $("newFolderName").value = ""; await loadFolders(r.data.id);
  };
  $("renameFolderBtn").onclick = () => { const name = prompt("Folder name", FOLDERS.find(f => f.id === FOLDER_ID)?.name); if (name !== null) update({name}); };
  $("activateFolderBtn").onclick = () => update({activate:true});
  $("stopFolderBtn").onclick = () => update({activate:false});
  $("archiveFolderBtn").onclick = () => update({archived:!FOLDERS.find(f => f.id === FOLDER_ID)?.archived});
  document.addEventListener("click", e => {
    // A save may redraw its button before the click bubbles to document. Use
    // the captured event path so that an internal click still preserves the pin.
    if (!e.composedPath().some(node => ["overviewArea", "editAvailabilityModal", "editAvailabilityOverlay"].includes(node.id))) {
      PINNED = PREVIEWED = null; drawViewer();
    }
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { PINNED = PREVIEWED = null; drawViewer(); } });
}


// A deletion request keeps its captured target even if the view changes.
let DELETION_PREVIEW = null, DELETION_REQUEST = 0, DELETION_BUSY = false;
function hideDeletionDialog() {
  const dialog = $("deleteFolderDialog");
  if (dialog.close) dialog.close(); else dialog.removeAttribute("open");
}
function cancelFolderDeletion() {
  if (DELETION_BUSY) return;
  DELETION_REQUEST++; DELETION_PREVIEW = null;
  hideDeletionDialog();
  $("deleteFolderConfirmation").value = "";
}
function updateDeletionConfirmation() {
  $("deleteFolderConfirmBtn").disabled = DELETION_BUSY || !DELETION_PREVIEW ||
    $("deleteFolderConfirmation").value !== DELETION_PREVIEW.folder.name;
}
async function openFolderDeletion() {
  if (!FOLDER_ID || DELETION_BUSY) return;
  const request = ++DELETION_REQUEST, folderId = FOLDER_ID;
  DELETION_PREVIEW = null;
  $("deleteFolderConfirmation").value = "";
  $("deleteFolderError").textContent = "";
  $("deleteFolderDetails").textContent = "Loading deletion counts…";
  updateDeletionConfirmation();
  const dialog = $("deleteFolderDialog");
  if (!dialog.open) { if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", ""); }
  const preview = await apiGet(`/api/folders/${folderId}/deletion-preview`);
  if (request !== DELETION_REQUEST || folderId !== FOLDER_ID) return;
  if (!preview) { $("deleteFolderError").textContent = $("folderMsg").textContent || "Could not load the preview. Cancel and retry."; return; }
  DELETION_PREVIEW = preview;
  const c = preview.counts;
  $("deleteFolderDetails").innerHTML = `<p>Folder: <strong>${escapeHtml(preview.folder.name)}</strong></p>
    <ul><li>${c.unverifiedResponses || 0} intake responses and ${c.collectionCodes || 0} collection codes</li>
    <li>${c.availabilitySubmissions} accepted availability submissions, including comments and submission timestamps</li>
    <li>${c.weekdaySchedules} saved weekday schedules (Monday–Friday)</li>
    <li>${c.weekendSchedules} saved weekend schedules (Friday evening–Sunday)</li></ul>
    <p>Deletion is permanent and cannot be undone through the app.</p>
    <p>Employee roster entries, minimum/maximum hours, lead designations, global settings, and other folders will be preserved.</p>
    ${preview.acceptsSubmissions ? '<p>This folder currently accepts submissions. Deleting it also stops submissions here. No other folder will be activated.</p>' : ''}`;
  updateDeletionConfirmation(); $("deleteFolderConfirmation").focus();
}
async function confirmFolderDeletion() {
  if (DELETION_BUSY || !DELETION_PREVIEW || $("deleteFolderConfirmation").value !== DELETION_PREVIEW.folder.name) return;
  const preview = DELETION_PREVIEW, token = TOKEN;
  DELETION_BUSY = true; updateDeletionConfirmation(); renderFolderControls();
  $("deleteFolderCancelBtn").disabled = $("deleteFolderConfirmation").disabled = true;
  $("deleteFolderError").textContent = "Deleting…";
  let response, data;
  try {
    response = await fetch(`/api/folders/${preview.folder.id}`, {method:"DELETE", headers:authHeaders(),
      body:JSON.stringify({confirmationName:$("deleteFolderConfirmation").value, previewVersion:preview.previewVersion})});
    data = await response.json();
  } catch (_) { data = {error:"Connection failed. Deletion could not be confirmed. Please retry."}; }
  DELETION_BUSY = false;
  $("deleteFolderCancelBtn").disabled = $("deleteFolderConfirmation").disabled = false;
  if (!TOKEN || TOKEN !== token) return;
  renderFolderControls();
  if (response?.status === 401) { clearSession(); return; }
  if (!response?.ok) {
    const error = data.error || "Deletion failed. Please retry.";
    if (FOLDER_ID !== preview.folder.id) {
      cancelFolderDeletion();
      $("folderMsg").textContent = `Deletion of “${preview.folder.name}” failed: ${error}`;
      return;
    }
    if (response?.status === 409) {
      await openFolderDeletion(); // Fresh scope requires freshly typed confirmation.
    }
    $("deleteFolderError").textContent = error;
    updateDeletionConfirmation(); return;
  }
  cancelFolderDeletion();
  FOLDERS = FOLDERS.filter(f => f.id !== preview.folder.id);
  if (ACTIVE_FOLDER === preview.folder.id) ACTIVE_FOLDER = null;
  if (FOLDER_ID === preview.folder.id) FOLDER_ID = FOLDERS[0]?.id ?? null;
  renderFolderControls();
  await changeFolder(); // Invalidate pending reads immediately, even if refresh fails.
  await loadFolders(FOLDER_ID);
  $("folderMsg").textContent = `Folder “${preview.folder.name}” was permanently deleted.`;
}
