// Real browser checks on a newly created local SQLite database only.
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
    cfg, errors = save_config(dict(reqStaffOpen=1,reqStaffLate=1,minShiftLength=2,
        maxShiftLength=6,requireLeadDuringOpen=False,blockClopening=False,
        solverWorkers=1,solverTimeLimit=3))
    assert not errors
    for name in ['Alex','Blair','Casey','Drew','Emery','Finley']:
        employee = Employee.create(name=name,min_hours=0,max_hours=40)
        grid = {f'{d}_{h:02d}':1 for d in cfg['days'] for h in range(7,22)}
        if name=='Alex':
            for d in ['Mon','Tue','Wed']:
                for h in [7,8]: grid[f'{d}_{h:02d}']=2
        if name=='Blair':
            for d in ['Mon','Tue','Thu']:
                for h in [19,20,21]: grid[f'{d}_{h:02d}']=2
        FolderAvailability.create(employee=employee,folder=1,data_json=json.dumps(grid),comment='Disposable comment')
`;
async function check(viewport) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'boundary-browser-'));
  const env={...process.env,...testKeys(process.env.PYTHON||'python'),DATABASE_URL:'',DATABASE_PATH:path.join(directory,'disposable.db'),ADMIN_PASSWORD:'browser-test',PORT:'5098'};
  const python=process.env.PYTHON||'python';
  const setup=spawnSync(python,['-c',fixture],{cwd:__dirname,env,encoding:'utf8'});
  assert.equal(setup.status,0,setup.error?.message||setup.stderr);
  const server=spawn(python,['app.py'],{cwd:__dirname,env,stdio:'ignore'});
  let browser;
  try {
    for(let i=0;i<100;i++) {
      try {if((await fetch('http://127.0.0.1:5098/healthz')).ok)break;}catch{}
      await new Promise(r=>setTimeout(r,100));
    }
    browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
    const page=await browser.newPage({viewport});
    // Hold configuration to verify the entire fieldset starts hidden, without a flash.
    let releaseContext;
    const contextGate=new Promise(resolve=>releaseContext=resolve);
    await page.route('**/api/submission-context',async route=>{await contextGate;await route.continue();});
    await page.goto('http://127.0.0.1:5098/',{waitUntil:'domcontentloaded'});
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false);
    releaseContext();

    const fixtureAPI=await collectionFixture(page,'http://127.0.0.1:5098');
    await fixtureAPI.open(page,'Alex');
    await page.waitForFunction(()=>document.querySelector('#openingConsentNote').textContent.includes('3 fully preferred'));
    assert.match(await page.locator('#boundaryStatus').innerText(),/has not enabled/);
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),false);
    await page.locator('#allowExtraOpenings').focus();await page.keyboard.press('Space');
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    assert.equal(await page.locator('#allowExtraClosings').isChecked(),false);
    await page.locator('#submitBtn').click();await page.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Saved'));
    await fixtureAPI.accept('Alex');await fixtureAPI.open(page,'Alex');
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    // Keyboard erasing/painting, day fill, copying and clearing all recalculate visibility.
    await page.locator('[data-mode="0"]').click();
    await page.locator('[data-key="Wed_07"]').focus();await page.keyboard.press('Space');
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false);
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    assert.equal(await page.locator('#boundaryConsentManagement').isVisible(),true);
    await page.locator('[data-mode="2"]').click();
    await page.locator('[data-key="Wed_07"]').focus();await page.keyboard.press('Space');
    assert.equal(await page.locator('#boundaryChoices').isVisible(),true);
    await page.locator('[data-fillday="Wed"]').click();
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false);
    await page.locator('#copyMonBtn').click();
    assert.equal(await page.locator('#boundaryChoices').isVisible(),true);
    page.once('dialog',dialog=>dialog.accept());await page.locator('#clearAllBtn').click();
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false);
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    await fixtureAPI.open(page,'Alex');
    await page.waitForFunction(()=>document.querySelector('#boundaryChoices').hidden===false);
    await page.locator('#boundaryChoices').scrollIntoViewIfNeeded();
    fs.mkdirSync(path.join(__dirname,'test-browser-output'),{recursive:true});
    await page.screenshot({path:path.join(__dirname,`test-browser-output/boundary-employee-${viewport.width}.png`),fullPage:true});
    const box=await page.locator('#boundaryChoices').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=viewport.width+1);
    await fixtureAPI.open(page,'Blair');
    await page.waitForFunction(()=>document.querySelector('#closingConsentNote').textContent.includes('3 fully preferred'));
    await page.locator('#allowExtraClosings').check();await page.locator('#submitBtn').click();
    await page.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Saved'));
    await fixtureAPI.accept('Blair');
    const login=await page.request.post('http://127.0.0.1:5098/api/admin/login',{data:{password:'browser-test'}});
    const headers={Authorization:'Bearer '+(await login.json()).token};
    const overview=await (await page.request.get('http://127.0.0.1:5098/api/availability?folderId=1',{headers})).json();
    const ids=overview.submissions.map(s=>s.employeeId);
    let generated;
    for(const enabled of [false,true]) {
      await page.request.put('http://127.0.0.1:5098/api/config',{headers,data:{allowPreferredBoundaryExtras:enabled}});
      generated=await (await page.request.post('http://127.0.0.1:5098/api/generate',{headers,data:{folderId:1,employeeIds:ids,seed:1}})).json();
      assert.ok(['OPTIMAL','FEASIBLE'].includes(generated.status),JSON.stringify(generated));
      for(const r of generated.boundarySummary) {
        assert.ok(r.openings-r.qualifyingOpenings<=r.caps.openings);
        assert.ok(r.closings-r.qualifyingClosings<=r.caps.closings);
        assert.ok(r.openings+r.closings-r.qualifyingOpenings-r.qualifyingClosings<=r.caps.combined);
        if(!enabled)assert.equal(r.overrun,0);
      }
    }
    await page.request.put('http://127.0.0.1:5098/api/config',{headers,data:{maxMorningShifts:3}});
    await page.reload();
    await fixtureAPI.open(page,'Alex');
    await page.locator('#reconfirmConsent').waitFor({state:'visible'});
    assert.equal(await page.locator('#submitBtn').isDisabled(),true);
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false); // Equality at 3.
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    assert.match(await page.locator('#boundaryConsentSummary').innerText(),/3 openings/);
    await page.locator('#reconfirmConsent').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('#submitBtn').isDisabled(),false);
    // Existing refresh receives saved supervisor caps and preserves the draft choice.
    await page.request.put('http://127.0.0.1:5098/api/config',{headers,data:{maxMorningShifts:2}});
    await page.evaluate(()=>refreshContext());
    assert.equal(await page.locator('#boundaryChoices').isVisible(),true);
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),true);
    await page.request.put('http://127.0.0.1:5098/api/config',{headers,data:{maxMorningShifts:3}});
    await page.evaluate(()=>refreshContext());
    assert.equal(await page.locator('#boundaryChoices').isVisible(),false);
    const managementBox=await page.locator('#boundaryConsentManagement').boundingBox();
    assert.ok(managementBox.x>=0&&managementBox.x+managementBox.width<=viewport.width+1);
    await page.screenshot({path:path.join(__dirname,`test-browser-output/boundary-hidden-consent-${viewport.width}.png`),fullPage:true});
    await page.locator('#clearBoundaryConsent').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('#allowExtraOpenings').isChecked(),false);
    assert.equal(await page.locator('#submitBtn').isDisabled(),false);
    await page.locator('#submitBtn').click();await page.waitForFunction(()=>document.querySelector('#msgArea').textContent.includes('Saved'));
    await fixtureAPI.accept('Alex');
    const saved=await (await page.request.get(`http://127.0.0.1:5098/api/folders/1/schedules/${generated.savedScheduleId}`,{headers})).json();
    assert.equal(saved.result.config.maxMorningShifts,2);
    assert.equal(saved.submissions.find(s=>s.employeeId===ids[0]).consent.allowExtraOpenings,true);
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.abort());
    await page.goto('http://127.0.0.1:5098/admin.html');
    await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
    await page.locator('[data-person]').first().waitFor({state:'attached'});
    await page.locator('[data-tab=overview]').click();
    await page.locator('[data-person="1"]').click();
    assert.match(await page.locator('#viewerConsent').innerText(),/Additional openings: not opted in/);
    await page.locator('[data-person="2"]').click();
    assert.match(await page.locator('#viewerConsent').innerText(),/Reconfirmation required/);
    const statusBox=await page.locator('#viewerConsent').boundingBox();
    assert.ok(statusBox.x>=0&&statusBox.x+statusBox.width<=viewport.width+1);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
    await page.screenshot({path:path.join(__dirname,`test-browser-output/boundary-supervisor-${viewport.width}.png`),fullPage:true});
    console.log(`Boundary consent browser checks passed: ${viewport.width}px`);
  } finally {if(browser)await browser.close();server.kill();}
}
(async()=>{for(const viewport of [{width:1280,height:900},{width:375,height:812}])await check(viewport);})().catch(error=>{console.error(error);process.exitCode=1;});
