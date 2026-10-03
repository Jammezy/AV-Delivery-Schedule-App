// All records and configuration changes use a newly created local SQLite database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');

const fixture = `
import json
import app
from boundary import boundary_context
from models import *
with db.connection_context(), write_transaction():
    cfg, errors = save_config(dict(reqStaffOpen=1,reqStaffLate=1,minShiftLength=1,
        maxShiftLength=6,requireLeadDuringOpen=False,blockClopening=False,
        dayCloseHours={'Fri':19},solverWorkers=1,solverTimeLimit=3))
    assert not errors
    employee=Employee.create(name='Alex',min_hours=0,max_hours=40)
    grid={'Mon_07':2,'Mon_08':1,'Fri_19':1,'Fri_20':2,'Fri_21':1,'Sat_21':2,'Sun_16':1}
    FolderAvailability.create(employee=employee,folder=1,data_json=json.dumps(grid),comment='Keep comment',
        allow_extra_openings=True,allow_extra_closings=False,consent_context=boundary_context(cfg)['token'])
    other=Folder.create(name='Other folder')
    FolderAvailability.create(employee=employee,folder=other,data_json='{"Tue_08":1}',comment='Other comment')
`;

async function check(viewport) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(),'supervisor-availability-'));
  const env = {...process.env,DATABASE_URL:'',DATABASE_PATH:path.join(directory,'disposable.db'),
    ADMIN_PASSWORD:'browser-test',PORT:'5097'};
  const python = process.env.PYTHON || 'python';
  const setup = spawnSync(python,['-c',fixture],{cwd:__dirname,env,encoding:'utf8'});
  assert.equal(setup.status,0,setup.error?.message || setup.stderr);
  const server = spawn(python,['app.py'],{cwd:__dirname,env,stdio:'ignore'});
  let browser;
  try {
    for (let i=0;i<100;i++) {
      try {if ((await fetch('http://127.0.0.1:5097/healthz')).ok) break;} catch {}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    browser = await chromium.launch({headless:true});
    const page = await browser.newPage({viewport});
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.abort());
    await page.goto('http://127.0.0.1:5097/admin.html');
    await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
    await page.locator('[data-person="1"]').waitFor({state:'attached'});
    // The dashboard initially selects the newest folder (our isolation fixture).
    // Select the record being edited explicitly instead of relying on list order.
    const selectFixtureFolder = async()=>{
      await page.locator('#folderSelect').selectOption('1');
      await page.waitForFunction(()=>FOLDER_ID===1 && OVERVIEW?.folder?.id===1 && document.querySelector('[data-person="1"]'));
    };
    await selectFixtureFolder();
    await page.locator('[data-tab="overview"]').click();
    await page.locator('[data-person="1"]').click();
    const login = await page.request.post('http://127.0.0.1:5097/api/admin/login',{data:{password:'browser-test'}});
    const headers = {Authorization:'Bearer '+(await login.json()).token};
    const overview = async id=>(await (await page.request.get(`http://127.0.0.1:5097/api/availability?folderId=${id}`,{headers})).json());
    const original = (await overview(1)).submissions[0];
    const otherOriginal = (await overview(2)).submissions;
    const open = async()=>{await page.locator('#openEditAvailBtn').click();await page.locator('#editAvailabilityModal').waitFor({state:'visible'});};
    const cell = key=>page.locator(`#editAvailGrid [data-key="${key}"]`);
    const mode = value=>page.locator(`#editAvailabilityModal [data-mode="${value}"]`).click();
    const keyboardPaint = async key=>{await cell(key).focus();await page.keyboard.press('Space');};
    await open();
    assert.equal(await page.locator('#editAvailGrid .wg-cell:not([disabled])').count(),100);
    for (const day of ['Mon','Tue','Wed','Thu','Fri','Sat']) for (const hour of ['07','21']) {
      assert.equal(await cell(`${day}_${hour}`).isEnabled(),true);
    }
    assert.equal(await cell('Sun_16').isEnabled(),true);
    for (const hour of [17,18,19,20,21]) assert.equal(await cell(`Sun_${hour}`).isDisabled(),true);
    assert.equal(await cell('Fri_20').getAttribute('data-level'),'2');
    await mode(2);
    await cell('Fri_21').scrollIntoViewIfNeeded();
    const first=await cell('Fri_19').boundingBox(), last=await cell('Fri_21').boundingBox();
    await page.mouse.move(first.x+first.width/2,first.y+first.height/2);await page.mouse.down();
    await page.mouse.move(last.x+last.width/2,last.y+last.height/2,{steps:6});await page.mouse.up();
    for (const hour of [19,20,21]) assert.equal(await cell(`Fri_${hour}`).getAttribute('data-level'),'2');
    assert.match(await cell('Fri_21').getAttribute('aria-label'),/Fri 9PM–10PM: Preferred/);
    await mode(0);await keyboardPaint('Fri_20');
    assert.equal(await cell('Fri_20').getAttribute('data-level'),'0');
    assert.equal(await cell('Fri_20').getAttribute('aria-pressed'),'false');
    await mode(1);await keyboardPaint('Fri_20');
    assert.match(await cell('Fri_20').getAttribute('aria-label'),/Available/);
    // Cancel must leave the persisted record untouched.
    await page.locator('#editAvailCancelBtn').click();
    assert.deepEqual((await overview(1)).submissions[0],original);
    await open();
    // Day fills include late Friday/Saturday blocks and stop Sunday at 5 PM.
    for (const day of ['Fri','Sat','Sun']) {
      await page.locator(`#editAvailGrid [data-fillday="${day}"]`).click();
      assert.equal(await cell(`${day}_${day==='Sun'?'16':'21'}`).getAttribute('data-level'),day==='Sat'?'2':'1');
      await page.locator(`#editAvailGrid [data-fillday="${day}"]`).click();
      assert.equal(await cell(`${day}_${day==='Sun'?'16':'21'}`).getAttribute('data-level'),'0');
    }
    assert.equal(await cell('Sun_17').getAttribute('data-level'),'0');
    page.once('dialog',dialog=>dialog.accept());await page.locator('#editAvailClearBtn').click();
    assert.equal(await page.locator('#editAvailGrid [data-level="1"], #editAvailGrid [data-level="2"]').count(),0);
    await page.locator('#editAvailCancelBtn').click();
    // Narrow saved scheduling settings must not discard any collection entries.
    const configResponse = await page.request.put('http://127.0.0.1:5097/api/config',{headers,data:{hourStart:9,hourEnd:16,
      lateHourStart:17,minShiftLength:1,maxShiftLength:6,reqStaffLate:0,dayCloseHours:{Fri:13}}});
    assert.equal(configResponse.status(),200);
    await page.evaluate(async()=>{CONFIG=await apiGet('/api/config');await renderOverview();});
    await open();
    for (const key of ['Mon_07','Fri_19','Fri_20','Fri_21','Sat_21','Sun_16']) assert.notEqual(await cell(key).getAttribute('data-level'),'0');
    await mode(2);await keyboardPaint('Tue_07');
    // A failed save retains the draft and all unrelated saved entries.
    await page.route('**/api/admin/availability',route=>route.fulfill({status:503,
      contentType:'application/json',body:JSON.stringify({error:'Disposable failure'})}));
    await page.locator('#editAvailSaveBtn').click();
    await page.waitForFunction(()=>document.querySelector('#editAvailMsg').textContent.includes('Disposable failure'));
    assert.equal(await page.locator('#editAvailabilityModal').isVisible(),true);
    assert.equal(await cell('Tue_07').getAttribute('data-level'),'2');
    assert.equal(await page.locator('#editAvailComment').inputValue(),'Keep comment');
    await page.unroute('**/api/admin/availability');
    // New Friday cells can also be edited and saved after the cap/window change.
    for (const hour of [19,20,21]) await keyboardPaint(`Fri_${hour}`);
    const modalBox=await page.locator('#editAvailabilityModal').boundingBox();
    assert.ok(modalBox.x>=0&&modalBox.x+modalBox.width<=viewport.width+1);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
    fs.mkdirSync(path.join(__dirname,'test-browser-output'),{recursive:true});
    await page.screenshot({path:path.join(__dirname,`test-browser-output/supervisor-availability-${viewport.width}.png`),fullPage:true});
    await page.locator('#editAvailSaveBtn').click();
    await page.locator('#editAvailabilityModal').waitFor({state:'hidden'});
    const saved=(await overview(1)).submissions[0];
    assert.deepEqual(saved.availability,{...original.availability,Tue_07:2,Fri_19:2,Fri_20:2,Fri_21:2});
    assert.equal(saved.comment,original.comment);
    for (const key of ['allowExtraOpenings','allowExtraClosings','consentContext']) assert.equal(saved.consent[key],original.consent[key]);
    assert.deepEqual((await overview(2)).submissions,otherOriginal);
    await page.waitForFunction(()=>document.querySelector('#openEditAvailBtn')!==null);
    const row=page.locator('#viewerGrid tbody tr').last();
    assert.equal(await row.locator('td').nth(4).innerText(),'Preferred');
    await open();assert.equal(await cell('Fri_21').getAttribute('data-level'),'2');
    assert.equal(await cell('Sat_21').getAttribute('data-level'),'2');
    assert.equal(await cell('Sun_16').getAttribute('data-level'),'1');
    await page.locator('#editAvailCancelBtn').click();
    await page.reload();await page.locator('[data-person="1"]').waitFor({state:'attached'});
    await selectFixtureFolder();
    await page.locator('[data-tab="overview"]').click();await page.locator('[data-person="1"]').click();
    await open();assert.equal(await cell('Fri_21').getAttribute('data-level'),'2');
    console.log(`PASS supervisor full-window availability editing, persistence, keyboard and layout: ${viewport.width}px`);
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));
    fs.rmSync(directory,{recursive:true,force:true});
  }
}
(async()=>{for (const viewport of [{width:1280,height:900},{width:375,height:812}]) await check(viewport);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
