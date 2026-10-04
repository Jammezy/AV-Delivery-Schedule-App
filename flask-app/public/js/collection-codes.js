// Supervisor-only collection controls. Server authorization is authoritative.
let CODE_ROWS = [], INTAKE_ROWS = [], INTAKE_EMPLOYEES = [], CODES_REQUEST = 0, INTAKE_REQUEST = 0;
function clearCollectionView() {
  CODE_ROWS = []; INTAKE_ROWS = []; INTAKE_EMPLOYEES = []; CODES_REQUEST++; INTAKE_REQUEST++;
  for (const id of ['codesTable','codesMsg','codesCollectionStatus','folderLimitName','intakeArea','intakeMsg']) document.getElementById(id).textContent = '';
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
  const duplicates = new Map(); for (const row of INTAKE_ROWS) duplicates.set(row.name.toLocaleLowerCase(), (duplicates.get(row.name.toLocaleLowerCase()) || 0)+1);
  $('intakeArea').innerHTML = `<p>${INTAKE_ROWS.length} responses saved in this folder’s history. ${[...duplicates.values()].filter(n=>n>1).length} names have multiple responses. Availability is saved automatically; no approval is needed.</p>` + INTAKE_ROWS.map(row=>`<details class="card" data-intakerow="${row.id}">
    <summary>${escapeHtml(row.name)} — ${escapeHtml({Accepted:'Saved',Superseded:'Earlier response',Rejected:'Excluded legacy response'}[row.status] || row.status)}${duplicates.get(row.name.toLocaleLowerCase())>1 ? ' · Multiple responses' : ''} · ${escapeHtml(new Date(row.submittedAt).toLocaleString())}</summary>
    <p>Code #${row.codeId}${row.codeLabel ? ' · '+escapeHtml(row.codeLabel) : ''}${row.parentId ? ' · Correction of sheet #'+row.parentId : ''}.</p>
    <p>${escapeHtml(row.comment || 'No comment')}</p>
    <p>Additional preferred openings: ${row.consent.allowExtraOpenings ? 'Opted in' : 'Not opted in'}. Closings: ${row.consent.allowExtraClosings ? 'Opted in' : 'Not opted in'}.${row.consent.reconfirmationNeeded ? ' Reconfirmation needed under current settings.' : ''}</p>
    <div class="scroll-x"><table class="data-table"><thead><tr><th>Time</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d=>`<th>${d}</th>`).join('')}</tr></thead><tbody>${supervisorAvailabilityHours().map(h=>`<tr><th>${blockLabel(h)}</th>${SUPERVISOR_AVAILABILITY_DAYS.map(d=>`<td>${row.availability[cellKey(d,h)]===2 ? 'Preferred' : row.availability[cellKey(d,h)] ? 'Available' : '—'}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    <p>Linked employee: ${escapeHtml(INTAKE_EMPLOYEES.find(e=>e.id===row.employeeId)?.name || row.name)}. ${row.status === 'Accepted' ? 'Saved separately for scheduling. Existing submissions are preserved.' : 'Kept for history; current availability appears below.'}</p>
    <button class="secondary" data-editintake="${row.id}">Create edit link</button>
    <button class="secondary" data-revokeedits="${row.id}">Revoke edit links</button>
    </details>`).join('');
  for (const button of $('intakeArea').querySelectorAll('[data-editintake]')) button.onclick = async () => {
    button.disabled = true;
    const r = await apiSend(`/api/admin/intake/${button.dataset.editintake}/edit-links`, 'POST', {});
    if (!r) return;
    if (r.ok) { await copyCollectionText(`${formLink()}#edit=${encodeURIComponent(r.data.token)}`); $('intakeMsg').textContent = 'Edit link ready. It expires in 24 hours and uses this code’s remaining responses.'; }
    else $('intakeMsg').textContent = r.data.error;
    button.disabled = false;
  };
  for (const button of $('intakeArea').querySelectorAll('[data-revokeedits]')) button.onclick = async () => {
    const r = await apiSend(`/api/admin/intake/${button.dataset.revokeedits}/edit-links`, 'DELETE', {});
    if (r) $('intakeMsg').textContent = r.ok ? 'Edit links revoked.' : r.data.error;
  };
}
function bindCollectionControls() {
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
