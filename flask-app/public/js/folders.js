let FOLDERS = [], FOLDER_ID = null, ACTIVE_FOLDER = null;
let SELECTED = new Set(), OVERVIEW = null, PINNED = null, PREVIEWED = null;
let VIEW_REVISION = 0;

function folderQuery() { return `folderId=${FOLDER_ID}`; }
function selectionQuery() {
  return `${folderQuery()}&selection=1` + [...SELECTED].map(id => `&employeeId=${id}`).join("");
}
function clearResult() {
  LAST_RESULT = null;
  for (const id of ["generateMsg", "scheduleOutput", "fairnessOutput"]) $(id).innerHTML = "";
  $("downloadBtn").style.display = $("regenerateBtn").style.display = "none";
}
async function loadFolders(preferred = FOLDER_ID) {
  const data = await apiGet("/api/folders");
  if (!data) return;
  FOLDERS = data.folders; ACTIVE_FOLDER = data.activeFolderId;
  FOLDER_ID = FOLDERS.some(f => f.id === preferred) ? preferred : (ACTIVE_FOLDER || FOLDERS[0]?.id);
  $("folderSelect").innerHTML = FOLDERS.map(f => `<option value="${f.id}" ${f.id === FOLDER_ID ? "selected" : ""}>${escapeHtml(f.name)}${f.archived ? " (archived)" : ""}</option>`).join("");
  const active = FOLDERS.find(f => f.id === ACTIVE_FOLDER);
  $("folderStatus").textContent = active ? `Accepting submissions: ${active.name}` : "No folder is accepting submissions.";
  await changeFolder();
}
async function changeFolder() {
  FOLDER_ID = Number($("folderSelect").value);
  VIEW_REVISION++;
  PINNED = PREVIEWED = null;
  SELECTED = new Set(); OVERVIEW = null;
  clearResult();
  $("diagArea").innerHTML = $("overviewArea").innerHTML = $("generatorSelection").innerHTML = $("savedSchedules").innerHTML = "";
  await renderOverview(true);
  await refreshDiagnostics();
  await loadSavedSchedules();
}
function validAvailability(av) {
  const valid = new Set(CONFIG.availabilityDays.flatMap(d => hours().map(h => `${d}_${String(h).padStart(2, "0")}`)));
  return Object.fromEntries(Object.entries(av || {}).filter(([k,v]) => valid.has(k) && Number(v) > 0));
}
async function renderFolderOverview(reset = false) {
  const revision = VIEW_REVISION;
  const data = await apiGet(`/api/availability?${folderQuery()}`);
  if (!data || revision !== VIEW_REVISION) return;
  OVERVIEW = data;
  const submitted = data.employees.filter(e => Object.hasOwn(data.availability, e.name));
  submitted.sort((a,b) => Object.keys(validAvailability(data.availability[b.name])).length - Object.keys(validAvailability(data.availability[a.name])).length || a.name.localeCompare(b.name));
  const ids = new Set(submitted.map(e => e.id));
  SELECTED = reset ? ids : new Set([...SELECTED].filter(id => ids.has(id)));
  $("generatorSelection").innerHTML = `<p id="selectedCount"></p>` + submitted.map(e => `<label class="selection-person"><input type="checkbox" data-select="${e.id}" ${SELECTED.has(e.id) ? "checked" : ""}> ${escapeHtml(e.name)}</label>`).join("");
  $("generatorSelection").querySelectorAll("[data-select]").forEach(box => box.onchange = () => {
    box.checked ? SELECTED.add(Number(box.dataset.select)) : SELECTED.delete(Number(box.dataset.select));
    updateSelectionCount(); refreshDiagnostics();
  });
  updateSelectionCount();
  $("overviewArea").innerHTML = `<p>Hover or focus to preview. Click to pin; click again, outside, or press Escape to clear.</p>
    <div class="availability-viewer"><div class="viewer-list">${submitted.map(e => `<button class="secondary viewer-person" data-person="${e.id}" aria-pressed="false">${escapeHtml(e.name)} — ${Object.keys(validAvailability(data.availability[e.name])).length} hrs</button>`).join("")}</div>
    <div><p id="viewerCaption" role="status"></p><p id="viewerComment"></p><div class="scroll-x" id="viewerGrid"></div></div></div>
    <p>Missing submissions: ${data.missing.length ? data.missing.map(escapeHtml).join(", ") : "None"}.</p>`;
  $("overviewArea").querySelectorAll("[data-person]").forEach(btn => {
    const id = Number(btn.dataset.person);
    btn.onmouseenter = btn.onfocus = () => { PREVIEWED = id; drawViewer(); };
    btn.onmouseleave = btn.onblur = () => { PREVIEWED = null; drawViewer(); };
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
  $("viewerCaption").textContent = employee ? `${PINNED ? "Pinned" : "Preview"}: ${employee.name} · Submitted ${new Date(OVERVIEW.submittedAt[employee.name]).toLocaleString()}` : "All submissions: available employee count per hour";
  $("viewerComment").textContent = employee ? OVERVIEW.comments[employee.name] || "No comment" : "";
  $("overviewArea").querySelectorAll("[data-person]").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.person) === PINNED)));
  $("viewerGrid").innerHTML = `<table class="data-table"><thead><tr><th>Time</th>${CONFIG.availabilityDays.map(d => `<th>${d}</th>`).join("")}</tr></thead><tbody>${hours().map(h => `<tr><th>${blockLabel(h)}</th>${CONFIG.availabilityDays.map(d => {
    const key = `${d}_${String(h).padStart(2,"0")}`;
    const count = data.filter(av => Number(av[key]) > 0).length;
    const level = employee ? Number(data[0][key] || 0) : count > 0 ? 1 : 0;
    return `<td class="viewer-level-${level}">${employee ? level === 2 ? "Preferred" : level ? "Available" : "—" : count}</td>`;
  }).join("")}</tr>`).join("")}</tbody></table>`;
}
async function loadSavedSchedules() {
  const revision = VIEW_REVISION;
  const rows = await apiGet(`/api/folders/${FOLDER_ID}/schedules`);
  if (!rows || revision !== VIEW_REVISION) return;
  $("savedSchedules").innerHTML = rows.length ? rows.map(s => `<div class="row"><span>${escapeHtml(new Date(s.createdAt).toLocaleString())}</span><button data-open="${s.id}" class="secondary">Open</button><button data-delete="${s.id}" class="danger">Delete</button></div>`).join("") : "No saved schedules yet.";
  $("savedSchedules").querySelectorAll("[data-open]").forEach(btn => btn.onclick = async () => {
    const revision = VIEW_REVISION;
    const saved = await apiGet(`/api/folders/${FOLDER_ID}/schedules/${btn.dataset.open}`);
    if (!saved || revision !== VIEW_REVISION) return;
    LAST_RESULT = saved.result;
    $("scheduleOutput").innerHTML = renderPrintableTable(LAST_RESULT.schedule, LAST_RESULT.config);
    $("fairnessOutput").innerHTML = fairnessTable(LAST_RESULT.fairness || []);
    $("generateMsg").textContent = "Showing a saved schedule with its original settings and availability.";
    $("downloadBtn").style.display = "inline-block";
  });
  $("savedSchedules").querySelectorAll("[data-delete]").forEach(btn => btn.onclick = async () => {
    if (!confirm("Delete this saved schedule? Employee availability will be kept.")) return;
    const r = await apiSend(`/api/folders/${FOLDER_ID}/schedules/${btn.dataset.delete}`, "DELETE", {confirm:true});
    if (r?.ok) { clearResult(); await loadSavedSchedules(); }
  });
}
function setupFolders() {
  $("folderSelect").onchange = changeFolder;
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
  document.addEventListener("click", e => { if (!e.target.closest("#overviewArea")) { PINNED = PREVIEWED = null; drawViewer(); } });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { PINNED = PREVIEWED = null; drawViewer(); } });
}
