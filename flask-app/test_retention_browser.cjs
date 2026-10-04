// Background cleanup runs only on a freshly created disposable local database.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const {chromium} = require('playwright');
const python = process.env.PYTHON || 'python';

async function check(viewport) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'retention-browser-'));
  const env = {...process.env, REQUEST_RETENTION_ENABLED:'false', DATABASE_URL:'', DATABASE_PATH:path.join(directory,'test.db'), ADMIN_PASSWORD:'browser-test', PORT:'5098'};
  function execute(code) {
    const result = spawnSync(python, ['-c', code], {cwd:__dirname, env, encoding:'utf8'});
    assert.equal(result.status, 0, result.stderr || String(result.error));
    return result.stdout;
  }
  execute(`
import app, datetime, json
from models import *
with db.connection_context():
    f = Folder.get_by_id(1)
    f.created_at = datetime.datetime(2020,1,1); f.save()
    e = Employee.create(name='Expired fixture')
    FolderAvailability.create(folder=f, employee=e, submitted_at=datetime.datetime(2020,1,1), data_json='{"Mon_07":2}')
    SavedSchedule.create(folder=f, created_at=datetime.datetime(2020,1,1), snapshot_json='{}')
    SavedWeekendSchedule.create(folder=f, snapshot_json='{}')
`);
  const server = spawn(python, ['app.py'], {cwd:__dirname, env, stdio:'ignore'});
  let browser;
  try {
    for (let i=0; i<100; i++) {
      try { if ((await fetch('http://127.0.0.1:5098/healthz')).ok) break; } catch (_) {}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    browser = await chromium.launch({headless:true,...process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH,args:['--no-sandbox']}: {}});
    const page = await browser.newPage({viewport});
    page.on('pageerror', error=>console.error('Browser error:', error.message));
    await page.route('https://cdnjs.cloudflare.com/**', route=>route.abort());
    await page.goto('http://127.0.0.1:5098/admin.html');
    await page.locator('#passwordInput').fill('browser-test');
    await page.locator('#loginBtn').click();
    await page.locator('[data-person]').first().waitFor({state:'attached'});
    await page.locator('[data-tab=generate]').click();
    await page.locator('#savedSchedules [data-open]').first().waitFor({state:'attached'});
    await page.waitForLoadState('networkidle');
    execute(`from models import db
from retention import run_retention
try:
    r=run_retention(apply=True); assert r['deleted']['availability']==1; assert r['deleted']['weekdaySchedules']==1; assert r['deleted']['folders']==0
finally: db.close()`);
    // The still-visible old Open button must recover from the background 404.
    await page.locator('#savedSchedules [data-open]').first().click();
    try {
      await page.waitForFunction(()=>document.querySelector('#savedSchedules').textContent.includes('No saved schedules'));
    } catch (error) {
      console.error(await page.evaluate(()=>({message:document.querySelector('#folderMsg').textContent,
        saved:document.querySelector('#savedSchedules').textContent, revision:VIEW_REVISION, folder:FOLDER_ID,
        recovering:RECOVERING_CONTENT, folders:FOLDERS.length, loggedIn:!!TOKEN})));
      throw error;
    }
    assert.equal(await page.evaluate(()=>SELECTED.size), 0);
    assert.equal(await page.evaluate(()=>LAST_RESULT), null);
    assert.equal(await page.locator('[data-person]').count(), 0);
    assert.equal(await page.locator('#downloadBtn').isVisible(), false);
    execute(`import datetime
from models import db,SavedWeekendSchedule
from retention import run_retention
try:
    db.connect(); SavedWeekendSchedule.update(created_at=datetime.datetime(2020,1,1)).execute()
    r=run_retention(apply=True); assert r['deleted']['folders']==1
finally: db.close()`);
    await page.evaluate(()=>apiGet('/api/availability?folderId=1'));
    await page.waitForFunction(()=>FOLDER_ID === null);
    assert.equal(await page.locator('#generateBtn').isDisabled(), true);
    assert.equal(await page.locator('#wkndGenerateBtn').isDisabled(), true);
    assert.equal(await page.evaluate(()=>OVERVIEW), null);
    assert.equal(await page.evaluate(()=>LAST_WKND_PREVIEW), null);
    assert.equal(await page.locator('#createFolderBtn').isDisabled(), false);
    console.log(`PASS background retention recovery at ${viewport.width}×${viewport.height}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
    await new Promise(resolve=>server.exitCode !== null ? resolve() : server.once('exit',resolve));
  }
}
(async()=>{for (const viewport of [{width:1280,height:900},{width:375,height:812}]) await check(viewport);})().catch(error=>{console.error(error);process.exitCode=1;});
