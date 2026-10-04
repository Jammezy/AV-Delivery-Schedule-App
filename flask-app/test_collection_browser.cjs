// Real browser end-to-end check, synthetic database and random test-only keys.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process');const {chromium}=require('playwright');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'code-browser-'));
const python=process.env.PYTHON||'python';
const generate=spawnSync(python,['-c',`from cryptography.fernet import Fernet;import secrets,json;print(json.dumps(dict(COLLECTION_ENCRYPTION_KEY=Fernet.generate_key().decode(),COLLECTION_VERIFIER_KEY=secrets.token_hex(32))))`],{encoding:'utf8'});
assert.equal(generate.status,0,generate.stderr);
const env={...process.env,...JSON.parse(generate.stdout),DATABASE_URL:'',DATABASE_PATH:path.join(dir,'synthetic.db'),ADMIN_PASSWORD:'browser-test',PORT:'5102',REQUEST_RETENTION_ENABLED:'false'};
const server=spawn(python,['app.py'],{cwd:__dirname,env,stdio:'ignore'});
(async()=>{let browser;try{
 for(let i=0;i<100;i++){try{if((await fetch('http://127.0.0.1:5102/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
 browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
 const supervisor=await browser.newContext({viewport:{width:1280,height:900},permissions:['clipboard-read','clipboard-write']});
 const admin=await supervisor.newPage();let errors=[];admin.on('pageerror',e=>errors.push(e.message));
 await admin.route('**/cdnjs.cloudflare.com/**',r=>r.abort());
 await admin.goto('http://127.0.0.1:5102/admin.html');await admin.locator('#passwordInput').fill('browser-test');await admin.locator('#loginBtn').click();
 await admin.locator('#appArea').waitFor({state:'visible'});
 await admin.locator('[data-tab="codes"]').click();await admin.locator('#codeLabel').fill('Team <first>');await admin.locator('#createCodeBtn').click();
 await admin.waitForFunction(()=>CODE_ROWS.length===1);let code=await admin.evaluate(()=>CODE_ROWS[0]);assert.equal(code.remaining,30);
 await admin.locator(`[data-invitecode="${code.id}"]`).click();const invitation=await admin.evaluate(()=>navigator.clipboard.readText());assert.ok(invitation.includes(code.code));
 await admin.locator('#createCodeBtn').click();await admin.waitForFunction(()=>CODE_ROWS.length===2);let second=await admin.evaluate(()=>CODE_ROWS[0]);
 // The Codes dropdown selects the same folder as the rest of the supervisor UI.
 await admin.locator('#newFolderName').fill('Next scheduling folder');await admin.locator('#createFolderBtn').click();
 await admin.waitForFunction(()=>FOLDERS.some(f=>f.name==='Next scheduling folder') && FOLDER_ID!==ACTIVE_FOLDER);
 await admin.evaluate(()=>renderCodes());const nextFolder=await admin.evaluate(()=>FOLDER_ID);
 assert.equal(await admin.locator('#codesFolderSelect').inputValue(),String(nextFolder));assert.equal(await admin.evaluate(()=>CODE_ROWS.length),0);
 assert.equal(await admin.locator('#collectionResponseCap').inputValue(),'100');assert.match(await admin.locator('#codesCollectionStatus').innerText(),/Codes accept submissions/);
 await admin.locator('#createCodeBtn').click();await admin.waitForFunction(()=>CODE_ROWS.length===1);const nextCode=await admin.evaluate(()=>CODE_ROWS[0]);assert.equal(nextCode.folderId,nextFolder);
 assert.equal(await admin.evaluate(()=>ACTIVE_FOLDER),code.folderId);
 await admin.locator('#tab-codes details').evaluate(el=>el.open=true);await admin.locator('#collectionResponseCap').fill('150');await admin.locator('#saveCollectionCapBtn').click();
 await admin.waitForFunction(()=>document.querySelector('#codesCollectionStatus').textContent.includes('Folder limit 150'));
 // Delay an old folder's codes response, then switch to another folder before it returns.
 let delayedRoute,delayedReply;let releaseSeen;const seen=new Promise(r=>releaseSeen=r);
 await admin.route(`**/api/admin/codes?folderId=${code.folderId}&*`,async route=>{delayedReply=await route.fetch();delayedRoute=route;releaseSeen();});
 await admin.locator('#codesFolderSelect').selectOption(String(code.folderId));await seen;
 await admin.locator('#codesFolderSelect').selectOption(String(nextFolder));await admin.waitForFunction(id=>CODE_ROWS.length===1 && CODE_ROWS[0].folderId===id,nextFolder);
 await delayedRoute.fulfill({response:delayedReply});await admin.unroute(`**/api/admin/codes?folderId=${code.folderId}&*`);
 await admin.evaluate(()=>renderCodes());assert.equal(await admin.evaluate(()=>CODE_ROWS[0].folderId),nextFolder);
 await admin.locator('#codesFolderSelect').selectOption(String(code.folderId));await admin.waitForFunction(()=>CODE_ROWS.length===2);assert.equal(await admin.locator('#collectionResponseCap').inputValue(),'100');
 assert.equal(await admin.locator('#folderSelect').inputValue(),String(code.folderId));
 assert.equal(await admin.locator('#codesTable img').count(),0);
 // 35 different people can use the same generic link with two codes; no accounts.
 const employeeContext=await browser.newContext({viewport:{width:390,height:844}}),employee=await employeeContext.newPage();employee.on('pageerror',e=>errors.push(e.message));
 await employee.goto('http://127.0.0.1:5102/');assert.equal(await employee.locator('#availabilityForm').isVisible(),false);
 assert.match(await employee.locator('#codeGate').innerText(),/Enter the code your supervisor provided/);
 assert.ok(!(await employee.locator('main').innerText()).includes('generator still schedules weekdays only'));
 await employee.locator('#collectionCode').fill(code.code);await employee.locator('#unlockBtn').click();await employee.locator('#availabilityForm').waitFor({state:'visible'});
 assert.match(await employee.locator('#codeResponseAllowance').innerText(),/30 responses total/);assert.match(await employee.locator('#collectionDestination').innerText(),/Imported availability/);
 assert.equal(await employee.locator('#loadBtn').count(),0);assert.equal(await employee.locator('#rosterList').count(),0);
 await employee.locator('#nameInput').fill('Alex');await employee.locator('[data-key="Mon_07"]').focus();await employee.keyboard.press('Space');await employee.locator('#commentInput').fill('<img src=x onerror=alert(1)>');
 await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Saved'));
 assert.equal(await employee.locator('#submitBtn').isDisabled(),true);
 await admin.locator('#refreshCodesBtn').click();await admin.waitForFunction(()=>CODE_ROWS.find(c=>c.id===Number(document.querySelector('[data-copycode]').dataset.copycode)) && CODE_ROWS.some(c=>c.received===1));
 assert.match(await admin.locator('#codesTable').innerText(),/1 \/ 30/);
 await admin.locator('[data-tab="overview"]').click();await admin.waitForFunction(()=>INTAKE_ROWS.length===1);await admin.locator('#viewHistoryBtn').click();await admin.locator('[data-intakerow]').click();assert.equal(await admin.locator('#historyDetails img').count(),0);await admin.locator('#closeHistoryBtn').click();
 // A valid code immediately adds the employee and saves their availability.
 assert.equal(await admin.evaluate(()=>INTAKE_ROWS[0].status),'Accepted');
 assert.ok(await admin.evaluate(()=>INTAKE_ROWS[0].employeeId));
 assert.equal(await admin.locator('[data-acceptintake],[data-rejectintake],[data-intakeemployee]').count(),0);
 assert.equal(await admin.evaluate(()=>OVERVIEW.comments.Alex),'<img src=x onerror=alert(1)>');
 await admin.locator('[data-tab="employees"]').click();await admin.waitForFunction(()=>EMPLOYEES.some(e=>e.name==='Alex'));
 assert.equal(await admin.evaluate(()=>EMPLOYEES.filter(e=>e.name==='Alex').length),1);
 await admin.locator('[data-tab="overview"]').click();
 await employee.locator('#newSheetBtn').click();assert.equal(await employee.locator('#submitBtn').isDisabled(),true);await employee.locator('#collectionCode').fill(code.code);await employee.locator('#unlockBtn').click();await employee.locator('#codeGate').waitFor({state:'hidden'});await employee.locator('#nameInput').fill('Alex');await employee.locator('#commentInput').fill('Updated sheet');await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>SAVED);
 await admin.locator('#refreshIntakeBtn').click();await admin.waitForFunction(()=>INTAKE_ROWS.length===2 && OVERVIEW.comments['Alex (2)']==='Updated sheet');
 assert.equal(await admin.evaluate(()=>INTAKE_ROWS.filter(r=>r.status==='Accepted').length),2);
 assert.equal(await admin.evaluate(()=>INTAKE_ROWS.filter(r=>r.status==='Superseded').length),0);
 assert.equal(await admin.evaluate(()=>OVERVIEW.comments.Alex),'<img src=x onerror=alert(1)>');
 await employee.evaluate(value=>window.CODE_FOR_TEST=value,code.code);
 // Quota fill using the employee's legitimate session (synthetic requests only).
 const results=await employee.evaluate(async()=>{const statuses=[];for(let i=0;i<29;i++){const gate=await fetch('/api/collection/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:CODE_FOR_TEST})});if(!gate.ok){statuses.push(gate.status);continue;}const unlocked=await gate.json();const r=await fetch('/api/availability',{method:'POST',headers:{'Content-Type':'application/json','X-Submission-CSRF':unlocked.csrf || CSRF},body:JSON.stringify({name:'Synthetic '+i,availability:{},comment:'',folderId:CONTEXT.folder.id,revision:CONTEXT.revision,requestId:crypto.randomUUID()})});statuses.push(r.status);}return statuses;});
 assert.equal(results.filter(s=>s===200).length,28);assert.equal(results.at(-1),409);
 await admin.locator('[data-tab="codes"]').click();await admin.evaluate(()=>renderCodes());await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.status==='Exhausted'));
 // Add two responses to only the exhausted code, retain the default for other codes.
 await admin.locator(`#extraResponses-${code.id}`).fill('2');
 const addedResponse=admin.waitForResponse(r=>r.url().endsWith(`/api/admin/codes/${code.id}/responses`) && r.request().method()==='POST');
 await admin.locator(`[data-addresponses="${code.id}"]`).click();const added=await addedResponse;assert.equal(added.status(),200,await added.text());assert.equal((await added.json()).responseLimit,32);
 await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.responseLimit===32 && c.remaining===2 && c.status==='Active'));
 // An already open form refreshes the allowance before sending, without losing its draft.
 await employee.locator('#newSheetBtn').click();assert.equal(await employee.locator('#submitBtn').isDisabled(),true);await employee.locator('#collectionCode').fill(code.code);await employee.locator('#unlockBtn').click();await employee.locator('#codeGate').waitFor({state:'hidden'});await employee.locator('#nameInput').fill('Fresh allowance');await employee.locator('#commentInput').fill('Keep this draft');
 await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>SAVED && CONTEXT.responseLimit===32);
 assert.match(await employee.locator('#codeResponseAllowance').innerText(),/32 responses total/);assert.equal(await employee.locator('#commentInput').inputValue(),'Keep this draft');
 assert.equal(await admin.evaluate(id=>CODE_ROWS.find(c=>c.id===id).responseLimit,second.id),30);
 await admin.reload();await admin.locator('#appArea').waitFor({state:'visible'});await admin.locator('[data-tab="codes"]').click();await admin.locator('#codesFolderSelect').selectOption(String(code.folderId));
 await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.responseLimit===32 && c.remaining===1));
 assert.match(await admin.locator('#codesTable').innerText(),/31 \/ 32/);
 await admin.locator(`[data-invitecode="${code.id}"]`).click();assert.match(await admin.evaluate(()=>navigator.clipboard.readText()),/32 responses total/);
 const extra=await employee.evaluate(async()=>{const statuses=[];for(let i=0;i<3;i++){const gate=await fetch('/api/collection/unlock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:CODE_FOR_TEST})});if(!gate.ok){statuses.push(gate.status);continue;}const unlocked=await gate.json();const r=await fetch('/api/availability',{method:'POST',headers:{'Content-Type':'application/json','X-Submission-CSRF':unlocked.csrf || CSRF},body:JSON.stringify({name:'Additional '+i,availability:{},comment:'',folderId:CONTEXT.folder.id,revision:CONTEXT.revision,requestId:crypto.randomUUID()})});statuses.push(r.status);}return statuses;});
 assert.deepEqual(extra,[200,409,409]);
 await admin.locator('#refreshCodesBtn').click();await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.responseLimit===32 && c.received===32 && c.remaining===0 && c.status==='Exhausted'));
 // Revocation stops an already unlocked session; deleting preserves received sheets.
 await employeeContext.clearCookies();await employee.goto('http://127.0.0.1:5102/');await employee.locator('#codeGate').waitFor({state:'visible'});await employee.locator('#collectionCode').fill(second.code);await employee.locator('#unlockBtn').click();await employee.locator('#availabilityForm').waitFor({state:'visible'});
 admin.once('dialog',d=>d.accept());await admin.locator(`[data-revokecode="${second.id}"]`).click();await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.status==='Revoked'));
 assert.equal(await admin.locator(`[data-addresponses="${second.id}"]`).count(),0);
 await employee.locator('#nameInput').fill('Blair');await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('not accepting responses'));
 admin.once('dialog',d=>d.accept());await admin.locator(`[data-deletecode="${second.id}"]`).click();await admin.waitForFunction(()=>CODE_ROWS.length===1);await admin.locator('#showDeletedCodes').check();await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.status==='Deleted'));
 assert.equal(await admin.locator(`[data-addresponses="${second.id}"]`).count(),0);
 await admin.setViewportSize({width:390,height:844});await admin.locator(`#extraResponses-${code.id}`).fill('1');await admin.locator(`[data-addresponses="${code.id}"]`).click();
 await admin.waitForFunction(()=>CODE_ROWS.some(c=>c.responseLimit===33 && c.remaining===1));
 assert.equal(errors.length,0,errors.join('\n'));
 fs.mkdirSync(path.join(__dirname,'test-browser-output'),{recursive:true});await admin.screenshot({path:path.join(__dirname,'test-browser-output/codes-supervisor.png'),fullPage:true});
 await employee.screenshot({path:path.join(__dirname,'test-browser-output/codes-employee-mobile.png'),fullPage:true});
 await admin.locator('#logoutBtn').click();await admin.locator('#loginCard').waitFor({state:'visible'});assert.equal(await admin.locator('#codesTable').innerText(),'');
 console.log('PASS collection browser: default 30, add per-code responses, persisted limits/invitations, exact quota, private form, independent employee entries and mandatory code re-entry, mobile controls, revoke/delete, logout clearing.');
}finally{if(browser)await browser.close();server.kill();fs.rmSync(dir,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
