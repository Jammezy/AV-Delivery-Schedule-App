// Supervisor-only collection controls. Server authorization is authoritative.
let CODE_ROWS = [], INTAKE_ROWS = [], INTAKE_EMPLOYEES = [], CODES_REQUEST = 0, INTAKE_REQUEST = 0;
function clearCollectionView() {
  CODE_ROWS = []; INTAKE_ROWS = []; INTAKE_EMPLOYEES = []; CODES_REQUEST++; INTAKE_REQUEST++;
  for (const id of ['codesTable','codesMsg','codesCollectionStatus','folderLimitName','intakeArea','intakeMsg']) document.getElementById(id).textContent = '';
  closeResponseHistory(); HISTORY_SELECTED = null; HISTORY_PAGE = 0; $('historySearch').value = '';
  $('historyDetails').textContent = $('historyMsg').textContent = '';
  $('viewHistoryBtn').textContent = 'View response history (0)'; $('viewHistoryBtn').disabled = true;
  $('collectionResponseCap').value = '';
  $('collectionResponseCap').disabled = $('saveCollectionCapBtn').disabled = true;
}
async function copyCollectionText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch (_) { window.prompt('Copy this invitation:', text); return false; }
}
function formLink() { return new URL('/', location.href).href; }
async function renderCodes() {
  if (!TOKEN || !FOLDER_ID) return;
  const requestId = ++CODES_REQUEST, revision = VIEW_REVISION;
  const data = await apiGet(`/api/admin/codes?${folderQuery()}&showDeleted=${$('showDeletedCodes').checked ? 1 : 0}`);
  if (!data || !Array.isArray(data.codes) || !data.collection || requestId !== CODES_REQUEST || revision !== VIEW_REVISION) return;
  CODE_ROWS = data.codes;
  $('collectionResponseCap').disabled = $('saveCollectionCapBtn').disabled = false;
  $('collectionResponseCap').value = data.collection.responseCap;
  const folder = FOLDERS.find(f=>f.id===FOLDER_ID), folderName = folder?.name || 'Selected folder';
  const full = data.collection.received >= data.collection.responseCap;
  $('codesCollectionStatus').textContent = `${folderName}: ${folder?.archived ? 'Archived — restore before creating codes' : 'Codes accept submissions into this folder'} · ${data.collection.received} total submissions received · Folder limit ${data.collection.responseCap}${full ? '. This folder has reached its total submission limit. Increase the folder limit to accept more submissions.' : ''}`;
  $('folderLimitName').textContent = `${folderName}: ${data.collection.received} of ${data.collection.responseCap} total submissions received.`;
  $('codesTable').innerHTML = CODE_ROWS.length ? `<table class="data-table"><thead><tr><th>Code / label</th><th>Folder</th><th>Created / expires</th><th>Received</th><th>Remaining</th><th>Status</th><th>Actions</th></tr></thead><tbody>${CODE_ROWS.map(c=>`<tr>
    <td><strong>${escapeHtml(c.code)}</strong><br>${escapeHtml(c.label)}</td><td>${escapeHtml(c.folderName)}</td>
    <td>${escapeHtml(new Date(c.createdAt).toLocaleString())}<br>${c.expiresAt ? escapeHtml(new Date(c.expiresAt).toLocaleString()) : 'No expiry'}</td>
    <td>${c.received} / ${c.responseLimit}</td><td>${c.remaining}</td><td>${escapeHtml({'Collection closed':'Folder closed','Collection paused':'Folder limit reached'}[c.status] || c.status)}${c.status !== 'Active' ? '<br>Not accepting responses' : ''}</td>
    <td><button class="secondary" data-copycode="${c.id}">Copy code</button> <button class="secondary" data-invitecode="${c.id}">Copy invitation</button>
    ${!['Revoked','Deleted'].includes(c.status) && c.responseLimit < 10000 ? `<div><label for="extraResponses-${c.id}">Additional responses</label> <input id="extraResponses-${c.id}" type="number" min="1" max="${10000-c.responseLimit}" step="1" value="1" style="width:6rem"> <button class="secondary" data-addresponses="${c.id}">Add responses</button></div>` : ''}
    ${!['Revoked','Deleted'].includes(c.status) ? `<button class="secondary" data-revokecode="${c.id}">Revoke</button>` : ''}
    ${c.status !== 'Deleted' ? `<button class="danger" data-deletecode="${c.id}">Delete</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p>No codes yet. Click Create code to invite employees.</p>';
  for (const button of $('codesTable').querySelectorAll('[data-copycode],[data-invitecode]')) button.onclick = async () => {
    const c = CODE_ROWS.find(c=>c.id===Number(button.dataset.copycode || button.dataset.invitecode));
    if (!c) return;
    const text = button.dataset.copycode ? c.code : `Please submit availability for ${c.folderName}.\nLink: ${formLink()}\nCode: ${c.code}\n${c.expiresAt ? 'Submit before '+new Date(c.expiresAt).toLocaleString()+'.\n' : ''}No account needed. This code allows ${c.responseLimit} responses total.`;
    const copied = await copyCollectionText(text); $('codesMsg').textContent = copied ? 'Copied.' : 'Invitation ready to copy.';
  };
  for (const button of $('codesTable').querySelectorAll('[data-addresponses]')) button.onclick = async () => {
    const id = Number(button.dataset.addresponses), input = $(`extraResponses-${id}`);
    if (!input.reportValidity()) return;
    button.disabled = true;
    const r = await apiSend(`/api/admin/codes/${id}/responses`, 'POST', {additionalResponses:Number(input.value)});
    if (!r) return;
    if (!r.ok) { $('codesMsg').textContent = r.data.error; button.disabled = false; return; }
    const pause = r.data.status === 'Collection paused' ? ' This folder has reached its total submission limit. Increase the folder limit to accept more submissions.' : '';
    $('codesMsg').textContent = `Responses added. This code now allows ${r.data.responseLimit} total, with ${r.data.remaining} remaining.${pause}`;
    await renderCodes();
  };
  for (const button of $('codesTable').querySelectorAll('[data-revokecode],[data-deletecode]')) button.onclick = async () => {
    const id = Number(button.dataset.revokecode || button.dataset.deletecode), deleting = !!button.dataset.deletecode;
    if (!confirm(`${deleting ? 'Delete' : 'Revoke'} this code and stop new responses? Existing responses will be kept.`)) return;
    button.disabled = true;
    const r = await apiSend(`/api/admin/codes/${id}`, deleting ? 'DELETE' : 'PATCH', {});
    if (!r) return;
    $('codesMsg').textContent = r.ok ? `Code ${deleting ? 'deleted' : 'revoked'}. Responses kept.` : r.data.error;
    await renderCodes();
  };
}
async function renderIntake() {
  if (!TOKEN || !FOLDER_ID) return;
  const id = ++INTAKE_REQUEST, revision = VIEW_REVISION;
  const data = await apiGet(`/api/admin/intake?${folderQuery()}`);
  if (!data || !Array.isArray(data.submissions) || !Array.isArray(data.employees) || !data.acceptedVersions || id !== INTAKE_REQUEST || revision !== VIEW_REVISION) return;
  INTAKE_ROWS = data.submissions; INTAKE_EMPLOYEES = data.employees;
  $('viewHistoryBtn').textContent = `View response history (${INTAKE_ROWS.length})`;
  $('viewHistoryBtn').disabled = false;
  if (HISTORY_SELECTED !== null && !INTAKE_ROWS.some(r=>r.id===HISTORY_SELECTED)) HISTORY_SELECTED = null;
  if ($('responseHistoryDialog').open) renderResponseHistory();
  drawViewer();
}
let HISTORY_PAGE = 0, HISTORY_SELECTED = null;
function closeResponseHistory() {
  if ($('responseHistoryDialog').open) $('responseHistoryDialog').close();
}
function duplicateResponseName(name) {
  return INTAKE_ROWS.filter(r=>r.name.toLocaleLowerCase()===name.toLocaleLowerCase()).length > 1;
}
function editLinkButtons(row) {
  if (!row) return '';
  return `${row.canCreateEditLink ? `<button class="secondary" data-editintake="${row.id}">Create edit link</button>` : ''}
    ${row.canRevokeEditLinks ? `<button class="secondary" data-revokeedits="${row.id}">Revoke edit links</button>` : ''}`;
}
function bindEditLinkButtons(host, message) {
  for (const button of host.querySelectorAll('[data-editintake],[data-revokeedits]')) button.onclick = async () => {
    const revision = VIEW_REVISION, creating = !!button.dataset.editintake;
    const entryId = button.dataset.editintake || button.dataset.revokeedits;
    button.disabled = true;
    const r = await apiSend(`/api/admin/intake/${entryId}/edit-links`, creating ? 'POST' : 'DELETE', {});
    if (revision !== VIEW_REVISION) return;
    if (r?.ok && creating) {
      await copyCollectionText(`${formLink()}#edit=${encodeURIComponent(r.data.token)}`);
      if (revision !== VIEW_REVISION) return;
    }
    $(message).textContent = !r ? 'Could not complete the request. Please retry.' : !r.ok ? r.data.error :
      creating ? 'Edit link ready. It expires in 24 hours and uses this code’s remaining responses.' : 'Edit links revoked.';
    button.disabled = false;
  };
}
function renderPinnedEditLinks(employee) {
  const host = $('viewerPermissionControls');
  if (!host || !employee || PINNED !== employee.id) return;
  const rows = INTAKE_ROWS.filter(r=>r.employeeId===employee.id);
  const row = rows.find(r=>r.canCreateEditLink) || rows.find(r=>r.canRevokeEditLinks);
  const controls = document.createElement('div');
  controls.innerHTML = `${rows.some(r=>duplicateResponseName(r.name)) ? '<p>Multiple responses: matching names may belong to different people. Each employee entry stays separate.</p>' : ''}${editLinkButtons(row)}`;
  host.append(controls); bindEditLinkButtons(controls, 'intakeMsg');
}
function renderResponseHistory() {
  const query = $('historySearch').value.trim().toLocaleLowerCase();
  const rows = INTAKE_ROWS.filter(r=>`${r.name} ${r.codeLabel}`.toLocaleLowerCase().includes(query))
    .sort((a,b)=>new Date(b.submittedAt)-new Date(a.submittedAt) || b.id-a.id);
  const pages = Math.max(1, Math.ceil(rows.length/20));
  HISTORY_PAGE = Math.min(HISTORY_PAGE, pages-1);
  if (!rows.some(r=>r.id===HISTORY_SELECTED)) HISTORY_SELECTED = null;
  $('intakeArea').innerHTML = rows.length ? `<table class="data-table"><thead><tr><th>Name</th><th>Submitted</th><th>Code</th><th>Status</th></tr></thead><tbody>${rows.slice(HISTORY_PAGE*20,(HISTORY_PAGE+1)*20).map(row=>`<tr>
    <td><button class="secondary" data-intakerow="${row.id}" aria-pressed="${HISTORY_SELECTED===row.id}">${escapeHtml(row.name)}</button>${duplicateResponseName(row.name) ? '<br>Multiple responses' : ''}</td>
    <td>${escapeHtml(new Date(row.submittedAt).toLocaleString())}</td><td>#${row.codeId} ${escapeHtml(row.codeLabel)}</td>
    <td>${escapeHtml({Accepted:'Saved',Superseded:'Earlier response',Rejected:'Excluded legacy response'}[row.status] || row.status)}</td></tr>`).join('')}</tbody></table>` : '<p>No matching responses.</p>';
  $('historyPageStatus').textContent = `${rows.length} matching responses · Page ${HISTORY_PAGE+1} of ${pages}`;
  $('historyPrevBtn').disabled = HISTORY_PAGE===0; $('historyNextBtn').disabled = HISTORY_PAGE===pages-1;
  for (const button of $('intakeArea').querySelectorAll('[data-intakerow]')) button.onclick = () => {
    HISTORY_SELECTED = Number(button.dataset.intakerow);
    $('intakeArea').querySelectorAll('[data-intakerow]').forEach(b=>b.setAttribute('aria-pressed', String(Number(b.dataset.intakerow)===HISTORY_SELECTED)));
    renderHistoryDetails(); $('historyDetailsTitle').focus();
  };
  renderHistoryDetails();
}
function renderHistoryDetails() {
  const row = INTAKE_ROWS.find(r=>r.id===HISTORY_SELECTED);
  $('historyDetails').innerHTML = !row ? '<p>Select a response to see its details.</p>' : `
    <h3 id="historyDetailsTitle" tabindex="-1">${escapeHtml(row.name)} · Response #${row.id}</h3>
    <p>${escapeHtml(row.comment || 'No comment')}</p>
    <p>Recorded additional openings: ${row.consent.allowExtraOpenings ? 'Opted in' : 'Not opted in'}. Closings: ${row.consent.allowExtraClosings ? 'Opted in' : 'Not opted in'}.${row.consent.reconfirmationNeeded ? ' Reconfirmation needed under current settings.' : ''}</p>
    <p>${row.parentId ? `Correction of response #${row.parentId}.` : 'Original response.'} ${INTAKE_ROWS.filter(r=>r.parentId===row.id).map(r=>`Corrected by response #${r.id}.`).join(' ')}</p>
    <p>Linked employee: ${escapeHtml(INTAKE_EMPLOYEES.find(e=>e.id===row.employeeId)?.name || 'No linked employee')}. This preserved response may differ from current availability.</p>
    <div class="scroll-x"><table class="data-table"><thead><tr><th>Time</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d=>`<th>${d}</th>`).join('')}</tr></thead><tbody>${supervisorAvailabilityHours().map(h=>`<tr><th>${blockLabel(h)}</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d=>`<td>${row.availability[cellKey(d,h)]===2 ? 'Preferred' : row.availability[cellKey(d,h)] ? 'Available' : '—'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    ${editLinkButtons(row)}`;
  bindEditLinkButtons($('historyDetails'), 'historyMsg');
}
function bindCollectionControls() {
  $('viewHistoryBtn').onclick = () => { HISTORY_PAGE = 0; HISTORY_SELECTED = null; $('historySearch').value = ''; $('historyMsg').textContent = ''; renderResponseHistory(); $('responseHistoryDialog').showModal(); $('historySearch').focus(); };
  $('closeHistoryBtn').onclick = closeResponseHistory;
  $('responseHistoryDialog').addEventListener('close', () => $('viewHistoryBtn').focus());
  $('historySearch').oninput = () => { HISTORY_PAGE = 0; HISTORY_SELECTED = null; renderResponseHistory(); };
  $('historyPrevBtn').onclick = () => { HISTORY_PAGE--; HISTORY_SELECTED = null; renderResponseHistory(); };
  $('historyNextBtn').onclick = () => { HISTORY_PAGE++; HISTORY_SELECTED = null; renderResponseHistory(); };
  $('codesFolderSelect').onchange = async () => {
    $('folderSelect').value = $('codesFolderSelect').value;
    await changeFolder();
  };
  $('createCodeBtn').onclick = async () => {
    if (!FOLDER_ID) { $('codesMsg').textContent='Create or select a folder first.'; return; }
    $('createCodeBtn').disabled=true;
    const expiry = $('codeExpiry').value;
    const r = await apiSend('/api/admin/codes','POST',{folderId:FOLDER_ID,label:$('codeLabel').value,expiresAt:expiry ? new Date(expiry).toISOString() : null});
    $('createCodeBtn').disabled=!FOLDER_ID || FOLDERS.find(f=>f.id===FOLDER_ID)?.archived;
    if (!r) return;
    $('codesMsg').textContent = r.ok ? `Code created for ${r.data.folderName}: 30 responses available.${r.data.status !== 'Active' ? ' This folder is not accepting submissions through this code yet; check its status below.' : ''}` : r.data.error;
    if (r.ok) { $('codeLabel').value=''; await renderCodes(); }
  };
  $('refreshCodesBtn').onclick = renderCodes; $('showDeletedCodes').onchange = renderCodes;
  $('refreshIntakeBtn').onclick = () => renderOverview();
  $('saveCollectionCapBtn').onclick = async () => {
    const r = await apiSend(`/api/admin/collections/${FOLDER_ID}`,'PUT',{responseCap:Number($('collectionResponseCap').value)});
    if (!r) return;
    $('codesMsg').textContent=r.ok ? 'Folder limit saved. Individual code limits are unchanged.' : r.data.error;
    if (r.ok) await renderCodes();
  };
}
bindCollectionControls();
