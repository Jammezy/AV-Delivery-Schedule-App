// Real Chromium, disposable local database, no live credentials or data.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process'),{chromium}=require('playwright');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'response-history-')),python=process.env.PYTHON||'python';
const keys=spawnSync(python,['-c','from cryptography.fernet import Fernet;import secrets,json;print(json.dumps(dict(COLLECTION_ENCRYPTION_KEY=Fernet.generate_key().decode(),COLLECTION_VERIFIER_KEY=secrets.token_hex(32))))'],{encoding:'utf8'});
assert.equal(keys.status,0,keys.stderr);
const env={...process.env,...JSON.parse(keys.stdout),DATABASE_URL:'',DATABASE_PATH:path.join(dir,'synthetic.db'),ADMIN_PASSWORD:'browser-test',PORT:'5108',REQUEST_RETENTION_ENABLED:'false'};
const server=spawn(python,['app.py'],{cwd:__dirname,env:{...env,PYTHONIOENCODING:'utf-8'},stdio:['ignore','pipe','pipe']}),base='http://127.0.0.1:5108';
let serverOutput='';server.stdout.on('data',d=>serverOutput+=d);server.stderr.on('data',d=>serverOutput+=d);
(async()=>{let browser;try {
  for(let i=0;i<100;i++){try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  assert.equal(server.exitCode,null,serverOutput);
  browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}: {}});
  const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']}),page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.route('**/cdnjs.cloudflare.com/**',r=>r.abort());
  const login=await context.request.post(base+'/api/admin/login',{data:{password:'browser-test'}}),headers={Authorization:'Bearer '+(await login.json()).token};
  const folder=(await (await context.request.get(base+'/api/submission-context')).json()).folder.id;
  const code=await (await context.request.post(base+'/api/admin/codes',{headers,data:{folderId:folder,label:'Team label'}})).json();
  for(let n=0;n<25;n++) {
    const gate=await (await context.request.post(base+'/api/collection/unlock',{data:{code:code.code}})).json();
    const r=await context.request.post(base+'/api/availability',{headers:{'X-Submission-CSRF':gate.csrf},data:{name:n<2?'Repeat':`Person ${n}`,availability:{Mon_07:2},comment:`Comment ${n}`,folderId:folder,revision:gate.revision,requestId:crypto.randomUUID()}});
    assert.equal(r.status(),200,await r.text());
  }
  const empty=await (await context.request.post(base+'/api/folders',{headers,data:{name:'Empty',activate:false}})).json();
  await page.goto(base+'/admin.html');await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
  await page.locator('#appArea').waitFor({state:'visible'});await page.locator('#folderSelect').selectOption(String(folder));
  await page.locator('[data-tab="overview"]').click();await page.waitForFunction(()=>INTAKE_ROWS.length===25);
  assert.equal(await page.locator('#tab-overview h2').first().innerText(),'Current availability');
  assert.equal(await page.locator('#responseHistoryDialog').isVisible(),false);assert.equal(await page.locator('#intakeArea [data-intakerow]').count(),0);
  assert.equal(await page.locator('#viewHistoryBtn').innerText(),'View response history (25)');
  const rows=await page.evaluate(()=>INTAKE_ROWS),repeat=rows.find(r=>r.name==='Repeat'),single=rows.find(r=>r.name==='Person 24');
  await page.locator(`[data-person="${single.employeeId}"]`).click();assert.equal(await page.locator('#viewerPermissionControls [data-editintake]').count(),1);
  assert.ok(!(await page.locator('#viewerPermissionControls').innerText()).includes('Multiple responses'));
  await page.locator(`[data-person="${repeat.employeeId}"]`).click();assert.match(await page.locator('#viewerPermissionControls').innerText(),/Multiple responses/);
  await page.locator('#viewHistoryBtn').focus();await page.keyboard.press('Enter');assert.equal(await page.locator('#historySearch').evaluate(e=>e===document.activeElement),true);
  assert.equal(await page.locator('[data-intakerow]').count(),20);assert.equal(await page.locator('#historyDetails table').count(),0);
  assert.equal(await page.locator('[data-intakerow]').first().getAttribute('data-intakerow'),String(single.id));
  await page.locator('[data-intakerow]').first().focus();await page.keyboard.press('Enter');
  assert.equal(await page.locator('#historyDetailsTitle').evaluate(e=>e===document.activeElement),true);assert.match(await page.locator('#historyDetails').innerText(),/Comment 24/);
  if (process.env.TEST_OUTPUT_DIR) { fs.mkdirSync(process.env.TEST_OUTPUT_DIR,{recursive:true}); await page.locator('#responseHistoryDialog').evaluate(e=>e.scrollTop=0); await page.screenshot({path:path.join(process.env.TEST_OUTPUT_DIR,'response-history.png')}); }
  await page.locator('#historyNextBtn').click();assert.equal(await page.locator('[data-intakerow]').count(),5);assert.equal(await page.locator('#historyDetails table').count(),0);
  await page.locator('#historySearch').fill('repeat');assert.equal(await page.locator('[data-intakerow]').count(),2);
  await page.locator('#historySearch').fill('team label');assert.equal(await page.locator('[data-intakerow]').count(),20);
  await page.locator('#historySearch').fill('no match');assert.equal(await page.locator('[data-intakerow]').count(),0);
  await page.locator('#historySearch').fill('Person 24');await page.locator('[data-intakerow]').click();
  // Tab and Shift+Tab stay within the native modal dialog.
  await page.locator('#closeHistoryBtn').focus();await page.keyboard.press('Shift+Tab');assert.equal(await page.evaluate(()=>document.activeElement.closest('dialog')?.id),'responseHistoryDialog');
  await page.keyboard.press('Escape');assert.equal(await page.locator('#responseHistoryDialog').isVisible(),false);assert.equal(await page.locator('#viewHistoryBtn').evaluate(e=>e===document.activeElement),true);
  await page.locator('#viewerPermissionControls [data-editintake]').click();await page.waitForFunction(()=>!document.querySelector('#viewerPermissionControls [data-editintake]').disabled);const link=await page.evaluate(()=>navigator.clipboard.readText());
  // Administrator saves after link creation: employee loads that current state.
  const current=await (await context.request.get(base+`/api/availability?folderId=${folder}`,{headers})).json(),saved=current.submissions.find(r=>r.employeeId===repeat.employeeId);
  assert.equal((await context.request.put(base+'/api/admin/availability',{headers,data:{employeeId:repeat.employeeId,folderId:folder,permissionVersion:saved.permissionVersion,availability:{Tue_08:1},comment:'Admin newer'}})).status(),200);
  assert.equal((await context.request.put(base+`/api/folders/${folder}/config`,{headers,data:{hourStart:9,hourEnd:16,lateHourStart:17,dayCloseHours:{Fri:13}}})).status(),200);
  const employeeContext=await browser.newContext(),employee=await employeeContext.newPage();employee.on('pageerror',e=>errors.push(e.message));
  await employee.goto(link);await employee.locator('#availabilityForm').waitFor({state:'visible'});assert.equal(await employee.locator('#commentInput').inputValue(),'Admin newer');assert.equal(await employee.locator('[data-key="Tue_08"]').getAttribute('data-level'),'1');
  await employee.locator('#commentInput').fill('Employee newest');await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>SAVED);
  await page.locator('#refreshIntakeBtn').click();await page.waitForFunction(()=>INTAKE_ROWS.length===26);assert.equal(await page.evaluate(()=>OVERVIEW.employees.filter(e=>e.name.startsWith('Repeat')).length),2);
  // Another administrator save conflicts with a loaded employee draft.
  await page.locator(`[data-person="${repeat.employeeId}"]`).click(); // Refresh may preserve the pin; ensure it is pinned below.
  await page.evaluate(id=>{PINNED=id;drawViewer();},repeat.employeeId);
  await page.locator('#viewerPermissionControls [data-editintake]').click();await page.waitForFunction(()=>!document.querySelector('#viewerPermissionControls [data-editintake]').disabled);await employee.goto(await page.evaluate(()=>navigator.clipboard.readText()));await employee.locator('#availabilityForm').waitFor({state:'visible'});
  const newer=await (await context.request.get(base+`/api/availability?folderId=${folder}`,{headers})).json(),v=newer.submissions.find(r=>r.employeeId===repeat.employeeId).permissionVersion;
  assert.equal((await context.request.put(base+'/api/admin/availability',{headers,data:{employeeId:repeat.employeeId,folderId:folder,permissionVersion:v,availability:{Wed_09:2},comment:'Later admin'}})).status(),200);
  await employee.locator('#commentInput').fill('Stale draft');await employee.locator('#submitBtn').click();await employee.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Reload this page'));
  await employee.reload();await employee.locator('#availabilityForm').waitFor({state:'visible'});assert.equal(await employee.locator('#commentInput').inputValue(),'Later admin');
  await page.locator('#viewHistoryBtn').click();await page.locator('#closeHistoryBtn').click();await page.locator('#folderSelect').selectOption(String(empty.id));await page.waitForFunction(()=>INTAKE_ROWS.length===0 && OVERVIEW?.folder.id===FOLDER_ID);
  assert.equal(await page.locator('#viewHistoryBtn').innerText(),'View response history (0)');assert.equal(await page.locator('[data-editintake]').count(),0);assert.equal(await page.locator('#historyDetails').innerText(),'');
  assert.deepEqual(errors,[]);console.log('PASS response history, 20-row pagination, search, details, conditional notices, pinned edit controls, keyboard focus, live edit state, stale employee form, folder isolation');
} finally {if(browser)await browser.close();server.kill();fs.rmSync(dir,{recursive:true,force:true});}})().catch(e=>{console.error(e);process.exitCode=1;});
