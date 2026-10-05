// Real administrator interactions against an isolated local SQLite database.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn,spawnSync}=require('node:child_process');
const {chromium}=require('playwright');
const {testKeys}=require('./browser_collection_fixture.cjs');
const python=process.env.PYTHON||'python', base='http://127.0.0.1:5117';
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'folder-isolation-browser-'));
const env={...process.env,...testKeys(python),DATABASE_URL:'',DATABASE_PATH:path.join(directory,'disposable.db'),ADMIN_PASSWORD:'browser-test',PYTHONWARNINGS:'ignore'};
const fixture=`
import json
import app
from models import *
with db.connection_context(), write_transaction():
    Folder.update(name='Fall').where(Folder.id==1).execute()
    winter=Folder.create(name='Winter')
    save_config(dict(reqStaffOpen=1,requireLeadDuringOpen=False),1)
    save_config(dict(reqStaffOpen=2,requireLeadDuringOpen=False),winter.id)
    shared=Employee.create(name='Shared')
    for fid,name,minimum in [(1,'Alpha',6),(winter.id,'Beta',20)]:
        employee=Employee.create(name=name)
        for person in [employee,shared]:
            member=enroll_employee(person,fid)
            member.min_hours=minimum; member.max_hours=40; member.save()
            FolderAvailability.create(employee=person,folder=fid,data_json=json.dumps({'Mon_07':1,'Sat_07':1}))
    Employee.create(name='Unassigned legacy',min_hours=5)
app.app.config['REQUEST_RETENTION_ENABLED']=False
app.app.run(host='127.0.0.1',port=5117,use_reloader=False)
`;
(async()=>{
 let browser;const errors=[];
 const server=spawn(python,['-c',fixture],{cwd:__dirname,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
 let logs='';server.stdout.on('data',d=>logs+=d);server.stderr.on('data',d=>logs+=d);
 try {
  for(let i=0;i<80;i++) {try{const r=await fetch(base+'/admin.html');if(r.ok)break;}catch{} if(server.exitCode!==null)throw Error(logs);await new Promise(r=>setTimeout(r,100));}
  browser=await chromium.launch({headless:true,...process.env.BROWSER_CHANNEL?{channel:process.env.BROWSER_CHANNEL}:{}});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.on('pageerror',e=>errors.push(e.message));
  await page.route('https://cdnjs.cloudflare.com/**',r=>r.abort());
  await page.goto(base+'/admin.html');await page.locator('#passwordInput').fill('browser-test');await page.locator('#loginBtn').click();
  await page.waitForFunction(()=>!FOLDER_LOADING && STAFFING_PLAN);
  const token=await page.evaluate(()=>TOKEN),headers={Authorization:'Bearer '+token};
  const switchFolder=async id=>{await page.locator('#folderSelect').selectOption(String(id));await page.waitForFunction(id=>FOLDER_ID===id&&!FOLDER_LOADING&&STAFFING_PLAN,id);};
  await switchFolder(1);
  for(const tab of ['employees','settings','weekend','overview','diagnostics','generate','codes']) {
    await page.locator(`[data-tab="${tab}"]`).click();
    await switchFolder(2);
    assert.equal(await page.locator(`.tabs [data-tab="${tab}"]`).getAttribute('class'),'active');
    assert.equal(await page.locator('#tab-'+tab).isVisible(),true);
    assert.match(await page.locator('#employeeTableWrap').innerText(),/Beta/);
    assert.doesNotMatch(await page.locator('#employeeTableWrap').innerText(),/Alpha/);
    assert.equal(await page.locator('#cfg_reqStaffOpen').inputValue(),'2');
    assert.match(await page.locator('#wkndFixedTable').innerText(),/Beta/);
    assert.doesNotMatch(await page.locator('#wkndFixedTable').innerText(),/Alpha/);
    assert.doesNotMatch(await page.locator('#overviewArea').innerText(),/Alpha/);
    await switchFolder(1);
    assert.match(await page.locator('#employeeTableWrap').innerText(),/Alpha/);
    assert.doesNotMatch(await page.locator('#employeeTableWrap').innerText(),/Beta/);
    assert.equal(await page.locator('#cfg_reqStaffOpen').inputValue(),'1');
  }
  await page.locator('[data-tab="settings"]').click();await page.locator('#cfg_reqStaffOpen').fill('3');await page.locator('#saveSettingsBtn').click();
  await page.waitForFunction(()=>document.querySelector('#settingsMsg').textContent.includes('Settings saved'));
  await switchFolder(2);assert.equal(await page.locator('#cfg_reqStaffOpen').inputValue(),'2');
  await switchFolder(1);assert.equal(await page.locator('#cfg_reqStaffOpen').inputValue(),'3');
  await page.locator('[data-tab="employees"]').click();
  const shared=page.locator('#employeeTableWrap tr[data-name="Shared"]');
  await shared.locator('[data-field="minHours"]').fill('9');await shared.getByRole('button',{name:'Save',exact:true}).click();
  await page.waitForFunction(()=>STAFFING_PLAN.employees.find(e=>e.name==='Shared')?.minHours===9);
  await switchFolder(2);assert.equal(await shared.locator('[data-field="minHours"]').inputValue(),'20');
  await switchFolder(1);assert.equal(await shared.locator('[data-field="minHours"]').inputValue(),'9');
  await page.locator('#newEmpName').fill('Manual member');await page.locator('#addEmpBtn').click();
  await page.getByRole('cell',{name:'Manual member',exact:true}).waitFor();
  await switchFolder(2);assert.doesNotMatch(await page.locator('#employeeTableWrap').innerText(),/Manual member/);
  await switchFolder(1);await page.locator('#unassignedEmployees summary').click();await page.locator('[data-importemployee]').click();
  await page.getByRole('cell',{name:'Unassigned legacy',exact:true}).waitFor();
  await switchFolder(2);assert.doesNotMatch(await page.locator('#employeeTableWrap').innerText(),/Unassigned legacy/);
  await switchFolder(1);
  if(process.env.SCOPE_SCREENSHOT)await page.locator('#tab-employees').screenshot({path:process.env.SCOPE_SCREENSHOT});
  assert.deepEqual(errors,[]);
  console.log('PASS: all seven tabs retain folder scope; settings and shared-employee edits are independent; manual employees and explicit legacy imports stay in their folder.');
 } finally {if(browser)await browser.close();server.kill();}
})().catch(e=>{console.error(e);process.exitCode=1;});
