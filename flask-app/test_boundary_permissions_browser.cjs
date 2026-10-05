// All settings and records below belong to a disposable local SQLite database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn,spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const {testKeys,collectionFixture}=require('./browser_collection_fixture.cjs');
const fixture = `
import json
import app
from models import *
with db.connection_context(), write_transaction():
    cfg,errors=save_config(dict(minShiftLength=3,maxShiftLength=6,reqStaffOpen=1,reqStaffLate=1,
        maxMorningShifts=0,maxEveningShifts=0,maxMorningPlusEvening=0,requireLeadDuringOpen=False),1)
    assert not errors
    for name in ['Alex','Blair']:
        e=Employee.create(name=name,min_hours=0,max_hours=40)
        grid={'Mon_07':2,'Mon_08':2,'Mon_09':2,'Mon_18':1,'Mon_19':2,'Mon_20':2,'Mon_21':2}
        FolderAvailability.create(employee=e,folder=1,data_json=json.dumps(grid),comment='Preserve comment')
    other=Folder.create(name='Other folder')
    FolderAvailability.create(employee=1,folder=other,data_json='{"Tue_08":1}',comment='Other comment')
`;
async function check(viewport) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'permission-browser-'));
  const env={...process.env,...testKeys(process.env.PYTHON||'python'),DATABASE_URL:'',DATABASE_PATH:path.join(directory,'test.db'),ADMIN_PASSWORD:'browser-test',PORT:'5096'};
  const python=process.env.PYTHON||'python';
  const setup=spawnSync(python,['-c',fixture],{cwd:__dirname,env,encoding:'utf8'});
  assert.equal(setup.status,0,setup.error?.message||setup.stderr);
  const server=spawn(python,['app.py'],{cwd:__dirname,env,stdio:'ignore'});
  let browser;
  try {
    for(let i=0;i<100;i++) {
      try {if((await fetch('http://127.0.0.1:5096/healthz')).ok)break;}catch{}
      await new Promise(r=>setTimeout(r,100));
    }
    browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
    const page=await browser.newPage({viewport});
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.abort());
    await page.goto('http://127.0.0.1:5096/admin.html');
    await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
    await page.locator('[data-person="1"]').waitFor({state:'attached'});
    await page.locator('#folderSelect').selectOption('1');
    await page.waitForFunction(()=>OVERVIEW?.folder?.id===1);
    await page.locator('[data-tab="overview"]').click();
    await page.locator('[data-person="1"]').click();
    await page.locator('#supervisorExtraOpenings').waitFor();
    const login=await page.request.post('http://127.0.0.1:5096/api/admin/login',{data:{password:'browser-test'}});
    const headers={Authorization:'Bearer '+(await login.json()).token};
    const overview=async id=>(await (await page.request.get(`http://127.0.0.1:5096/api/availability?folderId=${id}`,{headers})).json());
    const initial=await overview(1), other=await overview(2);
    const current=async()=>(await overview(1)).submissions.find(s=>s.employeeId===1);
    const save=async()=>{
      await page.locator('#saveBoundaryPermissions').click();
      await page.waitForFunction(()=>document.querySelector('#permissionSaveStatus')?.textContent.includes('saved'));
    };
    assert.equal(await page.locator('#supervisorExtraOpenings').isChecked(),false);
    assert.match(await page.locator('#viewerPermissionControls').innerText(),/disabled/);
    await page.locator('#supervisorExtraOpenings').focus();await page.keyboard.press('Space');
    await save();assert.equal((await current()).consent.allowExtraOpenings,true);
    await page.locator('#supervisorExtraClosings').check();await save();
    assert.equal((await current()).consent.allowExtraClosings,true);
    assert.deepEqual((await overview(2)).submissions,other.submissions);
    assert.deepEqual((await overview(1)).submissions.find(s=>s.employeeId===2),initial.submissions.find(s=>s.employeeId===2));
    for(const key of ['availability','comment','submittedAt'])assert.deepEqual((await current())[key],initial.submissions[0][key]);
    const employee=await browser.newPage({viewport});
    await employee.goto('http://127.0.0.1:5096/');
    const fixtureAPI=await collectionFixture(page,'http://127.0.0.1:5096');
    await fixtureAPI.open(employee,'Alex');
    const token=(await current()).consent.consentContext;
    for(const [minimum,openings] of [[2,1],[4,0],[2,1]]) {
      const response=await page.request.put('http://127.0.0.1:5096/api/folders/1/config',{headers,data:{minShiftLength:minimum,allowPreferredBoundaryExtras:true}});
      assert.equal(response.status(),200);
      await employee.evaluate(()=>refreshContext());
      const consent=(await current()).consent;
      assert.equal(consent.consentContext,token);assert.equal(consent.reconfirmationNeeded,false);
      assert.equal(consent.candidates.openings.length,openings);assert.equal(consent.candidates.closings.length,1);
      assert.equal(await employee.locator('#allowExtraOpenings').isChecked(),true);
      assert.equal(await employee.locator('#reconfirmConsent').isVisible(),false);
      assert.equal(await employee.locator('#submitBtn').isEnabled(),true);
      await page.evaluate(()=>renderOverview());
      assert.equal(await page.locator('#supervisorExtraOpenings').isChecked(),true);
    }
    await page.request.put('http://127.0.0.1:5096/api/folders/1/config',{headers,data:{maxShiftLength:4}});
    assert.equal((await current()).consent.reconfirmationNeeded,false);
    // A failed save keeps choices. An employee change conflicts with a stale draft.
    await page.locator('#supervisorExtraOpenings').uncheck();
    await page.route('**/api/admin/boundary-permissions',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"Injected save failure"}'}));
    await page.locator('#saveBoundaryPermissions').click();
    await page.waitForFunction(()=>document.querySelector('#permissionSaveStatus').textContent.includes('Injected'));
    assert.equal(await page.locator('#supervisorExtraOpenings').isChecked(),false);
    assert.equal((await current()).consent.allowExtraOpenings,true);
    await page.unroute('**/api/admin/boundary-permissions');
    await employee.locator('#allowExtraClosings').uncheck();await employee.locator('#submitBtn').click();
    await employee.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Saved'));
    await fixtureAPI.accept('Alex');
    // The employee submission is independent and cannot invalidate the original employee's draft.
    await save();
    assert.equal((await current()).consent.allowExtraOpenings,false);
    assert.equal((await current()).consent.allowExtraClosings,true);
    const after=await overview(1);
    assert.ok(after.submissions.length>initial.submissions.length);
    await page.request.put('http://127.0.0.1:5096/api/folders/1/config',{headers,data:{maxMorningShifts:1}});
    await page.evaluate(()=>renderOverview());
    assert.match(await page.locator('#viewerPermissionControls').innerText(),/Reconfirmation required/);
    await save();assert.equal((await current()).consent.reconfirmationNeeded,false);
    await page.locator('#supervisorExtraOpenings').uncheck();await save();
    assert.equal((await current()).consent.allowExtraOpenings,false);
    await page.locator('[data-person="2"]').click();assert.equal(await page.locator('#supervisorExtraOpenings').isChecked(),false);
    await page.locator('[data-person="1"]').click();
    const bounds=await page.locator('#viewerPermissionControls').boundingBox();
    assert.ok(bounds.x>=0 && bounds.x+bounds.width<=viewport.width+1);
    fs.mkdirSync(path.join(__dirname,'test-browser-output'),{recursive:true});
    await page.screenshot({path:path.join(__dirname,`test-browser-output/boundary-permissions-${viewport.width}.png`),fullPage:true});
    console.log(`PASS persistent permissions, supervisor controls, conflicts and keyboard: ${viewport.width}px`);
  } finally {
    if(browser)await browser.close();
    server.kill();await new Promise(r=>server.exitCode!==null?r():server.once('exit',r));
    fs.rmSync(directory,{recursive:true,force:true});
  }
}
(async()=>{for(const viewport of [{width:1280,height:900},{width:375,height:812}])await check(viewport);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
