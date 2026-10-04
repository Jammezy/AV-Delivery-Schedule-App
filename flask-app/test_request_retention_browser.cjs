// Real app visits trigger cleanup on disposable storage, without a scheduler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const python = process.env.PYTHON || 'python';

async function check(viewport) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'request-retention-browser-'));
  const env = {...process.env, DATABASE_URL:'', DATABASE_PATH:path.join(directory,'test.db'),
    REQUEST_RETENTION_ENABLED:'true', ADMIN_PASSWORD:'browser-test', PORT:'5099'};
  function execute(code) {
    const result = spawnSync(python, ['-c',code], {cwd:__dirname,env,encoding:'utf8'});
    assert.equal(result.status,0,result.stderr || String(result.error));
  }
  execute(`
import app, datetime
from models import *
with db.connection_context():
    f=Folder.get_by_id(1)
    Folder.update(created_at=datetime.datetime(2020,1,1)).execute()
    e=Employee.create(name='Retained employee')
    FolderAvailability.create(folder=f,employee=e,submitted_at=datetime.datetime(2020,1,1))
    SavedSchedule.create(folder=f,created_at=datetime.datetime(2020,1,1),snapshot_json='{}')
    SavedWeekendSchedule.create(folder=f,snapshot_json='{}')
`);
  const server = spawn(python,['app.py'],{cwd:__dirname,env,stdio:'ignore'});
  let browser;
  try {
    let healthy = false;
    for (let i=0;i<100;i++) {
      try { if ((await fetch('http://127.0.0.1:5099/healthz')).ok) { healthy=true; break; } } catch (_) {}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert.ok(healthy,'Disposable app must start');
    execute(`from models import *
with db.connection_context():
    assert FolderAvailability.select().count()==1
    assert SavedSchedule.select().count()==1
`);
    browser=await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
    const page=await browser.newPage({viewport});
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.abort());
    await page.goto('http://127.0.0.1:5099/');
    await page.waitForFunction(()=>typeof document.querySelector('#submitBtn').onclick==='function');
    assert.equal(await page.locator('#rosterList').count(),0);
    assert.equal(await page.locator('#codeGate').isVisible(),true);
    execute(`from models import *
with db.connection_context():
    assert FolderAvailability.select().count()==0
    assert SavedSchedule.select().count()==0
    assert SavedWeekendSchedule.select().count()==1
    assert Folder.select().count()==1
    assert Employee.select().count()==1
`);
    assert.equal(await page.locator('#submitBtn').isEnabled(),false);
    assert.equal(await page.locator('#availabilityForm').isVisible(),false);
    console.log('PASS first website visit removes only expired records at '+viewport.width+'x'+viewport.height);
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise(resolve=>server.exitCode!==null ? resolve() : server.once('exit',resolve));
  }
}
(async()=>{for(const viewport of [{width:1280,height:900},{width:375,height:812}]) await check(viewport);})()
  .catch(error=>{console.error(error);process.exitCode=1;});
