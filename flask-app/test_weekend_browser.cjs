// Generate, save, reopen and download a weekend schedule using synthetic data.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process'),{chromium}=require('playwright'),ExcelJS=require('exceljs');
const {testKeys}=require('./browser_collection_fixture.cjs');
const python=process.env.PYTHON||'python',base='http://127.0.0.1:5126';
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'weekend-browser-'));
const output=process.env.WEEKEND_TEST_OUTPUT||path.join(__dirname,'test-browser-output');
fs.mkdirSync(output,{recursive:true});
const env={...process.env,...testKeys(python),DATABASE_URL:'',DATABASE_PATH:path.join(directory,'disposable.db'),ADMIN_PASSWORD:'browser-test'};
const fixture=`
import json
import app
from models import *
with db.connection_context(), write_transaction():
    Folder.update(name='Fall sample').where(Folder.id==1).execute()
    for name in ['Alex', 'Blair', 'Casey', 'Devon', 'Ellis', 'Finley']:
        employee=Employee.create(name=name)
        enroll_employee(employee,1)
        FolderAvailability.create(employee=employee,folder=1,data_json=json.dumps({f'{d}_{h:02d}':1 for d in ['Fri','Sat','Sun'] for h in range(7,22)}))
app.app.config['REQUEST_RETENTION_ENABLED']=False
app.app.run(host='127.0.0.1',port=5126,use_reloader=False)
`;
(async()=>{
  let browser;const errors=[];
  const server=spawn(python,['-c',fixture],{cwd:__dirname,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
  let logs='';server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
  try {
    let ready=false;
    for(let i=0;i<300;i++) {try{if((await fetch(base+'/healthz')).ok){ready=true;break;}}catch{} if(server.exitCode!==null)throw Error(logs);await new Promise(r=>setTimeout(r,100));}
    if(!ready)throw Error('Local test server did not start: '+logs);
    browser=await chromium.launch({headless:true,...process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{}});
    const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://cdnjs.cloudflare.com/**',route=>route.fulfill({path:require.resolve('exceljs/dist/exceljs.min.js'),contentType:'application/javascript'}));
    await page.goto(base+'/admin.html');await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
    await page.waitForFunction(()=>!FOLDER_LOADING&&STAFFING_PLAN);
    await page.locator('[data-tab="weekend"]').click();
    await page.locator('#wkndStart').fill('2026-09-04');await page.locator('#wkndEnd').fill('2026-10-04');
    await page.locator('#wkndFixedTable tbody tr').nth(1).locator('select').selectOption({label:'Alex'});
    await page.locator('#wkndFixedTable tbody tr').nth(2).locator('select').selectOption({label:'Blair'});
    for(const date of ['2026-09-11','2026-09-20']) {
      await page.locator('#newExclDate').fill(date);await page.locator('#newExclLabel').fill('Additional hours');await page.locator('#addExclBtn').click();
    }
    await page.locator('#wkndGenerateBtn').click();await page.waitForSelector('#wkndDownloadBtn');
    const data=await page.evaluate(()=>LAST_WKND_PREVIEW);
    assert.equal(data.signup_shifts.length,3);
    assert.equal(data.assignments.length,27);
    assert.equal(await page.locator('.weekend-signups tbody tr').count(),3);
    assert.equal(await page.locator('.weekend-assignments tbody tr').count(),27);
    assert.ok(data.assignments.every(ds=>!['2026-09-11','2026-09-20'].includes(ds.date)));
    const downloadPromise=page.waitForEvent('download');await page.locator('#wkndDownloadBtn').click();
    const download=await downloadPromise;assert.equal(download.suggestedFilename(),'Weekend_Schedule_2026-09-04_2026-10-04.xlsx');
    const file=path.join(output,download.suggestedFilename());await download.saveAs(file);
    const wb=new ExcelJS.Workbook();await wb.xlsx.readFile(file);
    assert.equal(wb.getWorksheet('Signup Shifts').getCell('C5').value,null);
    assert.equal(wb.getWorksheet('Signup Shifts').rowCount,7);
    assert.equal(wb.getWorksheet('Weekend Shifts').getCell('A5').value,'Friday, Sep 4, 2026: 7PM–10PM');
    await page.locator('#wkndSaveBtn').click();await page.waitForFunction(()=>document.querySelector('#wkndMsg').textContent.includes('Saved successfully'));
    // Reopen uses the saved snapshot even when the form's current exclusions change.
    await page.locator('#wkndExcludedList button').first().click();
    await page.evaluate(()=>{LAST_WKND_PREVIEW=null;});
    await page.locator('#wkndSavedSchedules').getByRole('button',{name:'View',exact:true}).first().click();
    await page.waitForFunction(()=>LAST_WKND_PREVIEW?.config.excluded_dates.length===2);
    await page.locator('.weekend-formatted').screenshot({path:path.join(output,'weekend-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.locator('#wkndDownloadBtn').isVisible(),true);
    const mobile=await page.evaluate(()=>({width:window.innerWidth,scrollWidth:document.documentElement.scrollWidth,
      overflow:[...document.querySelectorAll('#tab-weekend *')].filter(el=>el.getBoundingClientRect().right>window.innerWidth)
        .slice(0,5).map(el=>({tag:el.tagName,id:el.id,text:el.textContent.slice(0,60)}))}));
    assert.ok(mobile.scrollWidth<=mobile.width,JSON.stringify(mobile));
    await page.locator('.weekend-formatted').screenshot({path:path.join(output,'weekend-mobile.png')});
    assert.deepEqual(errors,[]);
    console.log('Weekend browser: generated, saved, reopened, downloaded and checked desktop/mobile layouts.');
  } finally {if(browser)await browser.close();server.kill();}
})().catch(error=>{console.error(error);process.exitCode=1;});
