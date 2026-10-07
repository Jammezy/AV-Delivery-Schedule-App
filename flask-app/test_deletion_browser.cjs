// Real browser checks use a new local SQLite database per viewport. No hosting
// configuration or production DATABASE_URL is read or used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');

const fixture = `
import json
import app
from models import *
with db.connection_context(), write_transaction():
    target = Folder.get_by_id(1)
    target.name = 'Disposable Fall <2026>'
    target.save()
    other = Folder.create(name='Preserved semester')
    cfg = get_config()
    employees = [Employee.create(name=n, min_hours=1, max_hours=40, is_lead=(n=='Alex')) for n in ['Alex', 'Blair']]
    for folder in [target, other]:
        for employee in employees:
            FolderAvailability.create(folder=folder, employee=employee, data_json='{"Mon_07":2,"Sat_07":1}', comment='Disposable comment')
        result = {'config':cfg, 'employees':[app.serialize(e,folder.id) for e in employees], 'fairness':[], 'schedule':{day:{7:{'DLA':'Alex'}} for day in cfg['days']}, 'work':{day:{h:[] for h in range(cfg['hourStart'],cfg['hourEnd']+1)} for day in cfg['days']}}
        SavedSchedule.create(folder=folder, snapshot_json=json.dumps({'result':result}))
        SavedWeekendSchedule.create(folder=folder, snapshot_json=json.dumps({'folderId':folder.id,
            'folderVersion':folder.created_at.isoformat(), 'employees':[app.serialize(e,folder.id) for e in employees],
            'assignments':[
                {'friday':'2026-09-04','date':'2026-09-04','shift':{'key':'friday_evening'},'assigned':employees[0].id,'origin':'fixed'},
                {'friday':'2026-09-04','date':'2026-09-06','shift':{'key':'sunday_afternoon'},'assigned':employees[1].id,'origin':'rotating'}],
            'rotating_counts':{employees[1].id:1}, 'effective_pool':[employees[1].id]}))
print(json.dumps({'target':target.id, 'other':other.id}))
`;
async function check(viewport) {
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),'deletion-browser-'));
  const env={...process.env,DATABASE_URL:'',DATABASE_PATH:path.join(directory,'disposable.db'),ADMIN_PASSWORD:'browser-test',PORT:'5099'};
  const setup=spawnSync(process.env.PYTHON||'python', ['-c',fixture], {cwd:__dirname,env,encoding:'utf8'});
  assert.equal(setup.status,0,setup.stderr);
  const ids=JSON.parse(setup.stdout.trim().split('\n').pop());
  const server=spawn(process.env.PYTHON||'python',['app.py'],{cwd:__dirname,env,stdio:'ignore'});
  let browser;
  try {
    for(let i=0;i<100;i++) {
      try {const r=await fetch('http://127.0.0.1:5099/healthz');if(r.ok)break;}catch(_){}
      await new Promise(r=>setTimeout(r,100));
    }
    browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
    const page=await browser.newPage({viewport});
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.abort()); // Export is covered by frontend XLSX regression.
    await page.goto('http://127.0.0.1:5099/admin.html');
    await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
    await page.locator('#folderSelect option').first().waitFor({state:'attached'});
    await page.locator('#folderSelect').selectOption(String(ids.target));
    await page.locator('[data-person]').first().waitFor({state:'attached'});
    await page.locator('[data-tab=generate]').click();
    await page.locator('#savedSchedules [data-open]').first().click();
    await page.locator('#scheduleOutput table').waitFor();
    await page.locator('[data-tab=weekend]').click();
    await page.locator('#wkndSavedSchedules button').first().click();
    await page.locator('#wkndPreviewArea table').first().waitFor();
    assert.match(await page.locator('#wkndPreviewArea').textContent(),/friday_evening/);
    assert.match(await page.locator('#wkndPreviewArea').textContent(),/sunday_afternoon/);
    await page.locator('#deleteFolderBtn').click();
    await page.locator('#deleteFolderDetails').getByText('Disposable Fall <2026>',{exact:true}).waitFor();
    const text=await page.locator('#deleteFolderDetails').textContent();
    for(const expected of ['2 accepted availability submissions','1 saved weekday','1 saved weekend','cannot be undone','roster memberships','stops submissions'])assert.ok(text.includes(expected),expected);
    const box=await page.locator('#deleteFolderDialog').boundingBox();
    assert.ok(box.x>=0&&box.x+box.width<=viewport.width&&box.height<=viewport.height);
    await page.locator('#deleteFolderConfirmation').fill('wrong');assert.equal(await page.locator('#deleteFolderConfirmBtn').isDisabled(),true);
    await page.locator('#deleteFolderCancelBtn').click();assert.equal(await page.locator('#deleteFolderDialog').isVisible(),false);
    const token=await page.evaluate(()=>sessionStorage.getItem('adminToken'));
    const api=async url=>{const r=await fetch('http://127.0.0.1:5099'+url,{headers:{Authorization:'Bearer '+token}});assert.ok(r.ok);return r.json();};
    assert.equal((await api('/api/folders')).folders.length,2);
    await page.locator('#deleteFolderBtn').click();
    await page.locator('#deleteFolderConfirmation').fill('Disposable Fall <2026>');
    await page.locator('#deleteFolderConfirmBtn').waitFor();
    fs.mkdirSync(path.join(__dirname,'test-browser-output'),{recursive:true});
    await page.screenshot({path:path.join(__dirname,'test-browser-output',`confirmation-${viewport.width}.png`),fullPage:true});
    let release,started;
    const begin=new Promise(r=>started=r), hold=new Promise(r=>release=r);
    await page.route(`**/api/folders/${ids.target}`,async route=>{
      if(route.request().method()==='DELETE'){started();await hold;}
      await route.continue();
    });
    await page.locator('#deleteFolderConfirmBtn').click();await begin;
    for(const id of ['deleteFolderConfirmBtn','deleteFolderCancelBtn','deleteFolderConfirmation'])assert.equal(await page.locator('#'+id).isDisabled(),true);
    release();await page.locator('#folderMsg').getByText(/was permanently deleted/).waitFor();
    assert.equal(await page.locator('#folderSelect').inputValue(),String(ids.other));
    const folders=await api('/api/folders');assert.equal(folders.activeFolderId,null);assert.equal(folders.folders.length,1);
    const overview=await api(`/api/availability?folderId=${ids.other}`);assert.equal(overview.submissions.length,2);
    assert.equal((await api(`/api/folders/${ids.other}/schedules`)).length,1);
    assert.equal((await api(`/api/folders/${ids.other}/weekend_schedules`)).length,1);
    assert.equal((await api(`/api/folders/${ids.other}/employees`)).length,2);
    for(const id of ['scheduleOutput','fairnessOutput','wkndPreviewArea'])assert.equal(await page.locator('#'+id).textContent(),'');
    assert.equal(await page.evaluate(()=>LAST_RESULT),null);assert.equal(await page.evaluate(()=>LAST_WKND_PREVIEW),null);
    await page.locator('#deleteFolderBtn').click();await page.locator('#deleteFolderConfirmation').fill('Preserved semester');
    await page.locator('#deleteFolderConfirmBtn').click();await page.locator('#overviewArea').getByText(/No folders yet/).waitFor({state:'attached'});
    assert.equal(await page.locator('#createFolderBtn').isEnabled(),true);assert.equal(await page.locator('#saveAllEmployeesBtn').isEnabled(),false);
    assert.match(await page.locator('#employeeTableWrap').textContent(),/Create or select a folder/);
    await page.screenshot({path:path.join(__dirname,'test-browser-output',`empty-${viewport.width}.png`),fullPage:true});
    console.log(`PASS real browser confirmation/deletion and final-folder flow at ${viewport.width}×${viewport.height}`);
  } finally {
    if(browser)await browser.close();
    if(server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}
    fs.rmSync(directory,{recursive:true,force:true});
  }
}
(async()=>{for(const viewport of [{width:1280,height:900},{width:375,height:812}])await check(viewport);})().catch(e=>{console.error(e);process.exitCode=1;});
