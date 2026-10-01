const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const ExcelJS = require('exceljs');

async function planningAdmin() {
  const {dom, run, doc} = await admin();
  let roster = [{id:1,name:'Alex',minHours:20,maxHours:100,isLead:false}];
  let required = 50, failSave = false, failLoad = false;
  const alerts = [];
  dom.window.alert = message => alerts.push(message);
  dom.window.confirm = () => true;
  dom.window.fetch = async (url, options = {}) => {
    if (url === '/api/staffing-plan') {
      if (failLoad) throw new Error('offline');
      const allotted = roster.reduce((sum,e)=>sum+e.minHours,0);
      return {ok:true,json:async()=>({employees:structuredClone(roster),requiredHours:required,allottedHours:allotted,remainingHours:required-allotted})};
    }
    if (url.startsWith('/api/employees/')) {
      if (failSave) return {ok:false,json:async()=>({error:'Save failed'})};
      const name = decodeURIComponent(url.split('/').pop());
      if (options.method === 'DELETE') roster = roster.filter(e=>e.name!==name);
      else {
        const data = JSON.parse(options.body), existing = roster.find(e=>e.name===name);
        const employee = {...data,name,id:existing?.id||2,minHours:Number(data.minHours),maxHours:Number(data.maxHours)};
        if (existing) Object.assign(existing,employee); else roster.push(employee);
      }
      return {ok:true,json:async()=>({})};
    }
    throw new Error('Unexpected request: '+url);
  };
  run('FOLDER_ID=null');
  await run('renderEmployees()');
  return {dom,run,doc,alerts,setFailSave:v=>failSave=v,setFailLoad:v=>failLoad=v,setRequired:v=>required=v};
}

test('planning totals preview, save, failure, invalid input and tab reload stay consistent',async()=>{
  const t = await planningAdmin(), {dom,run,doc} = t;
  const text = () => doc.getElementById('employeesStaffingPlan').textContent;
  const edit = value => {
    const input = doc.querySelector('[data-field="minHours"]');
    input.value=value; input.dispatchEvent(new dom.window.Event('input'));
  };
  assert.match(text(),/30 hours remaining/);
  edit('50'); assert.match(text(),/Unsaved preview/); assert.match(text(),/minimum hours match demand/);
  edit('65'); assert.match(text(),/−15/);
  assert.equal(doc.getElementById('diagnosticsStaffingPlan').innerHTML,doc.getElementById('employeesStaffingPlan').innerHTML);
  await run('renderEmployees()'); assert.equal(doc.querySelector('[data-field="minHours"]').value,'65');
  t.setFailSave(true); await doc.querySelector('[data-action="save"]').onclick();
  assert.match(text(),/Unsaved preview/); assert.deepEqual(t.alerts,['Save failed']);
  t.setFailSave(false); await doc.querySelector('[data-action="save"]').onclick();
  assert.match(text(),/Saved totals/); assert.match(text(),/−15/);
  await run('renderEmployees()'); assert.equal(doc.querySelector('[data-field="minHours"]').value,'65');
  for (const invalid of ['', '-1', '1.5', '101']) {
    edit(invalid); assert.match(text(),/Unsaved input is invalid/);
    await doc.querySelector('[data-action="save"]').onclick();
    assert.equal(run('STAFFING_PLAN.allottedHours'),65);
  }
  edit('0'); await doc.querySelector('[data-action="save"]').onclick();
  assert.match(text(),/50 hours remaining/);
  t.setRequired(60); await run('refreshDiagnostics()'); assert.match(text(),/60 hours remaining/);
  doc.getElementById('newEmpName').value='Blair'; await doc.getElementById('addEmpBtn').onclick();
  assert.equal(run('STAFFING_PLAN.employees.length'),2);
  await doc.querySelector('[data-action="delete"]').onclick();
  await doc.querySelector('[data-action="delete"]').onclick();
  assert.equal(run('STAFFING_PLAN.allottedHours'),0); assert.match(text(),/60 hours remaining/);
  dom.window.close();
});

test('planning loading, errors, out-of-order requests and logout never show stale totals',async()=>{
  const t=await planningAdmin(), {dom,run,doc}=t;
  t.setFailLoad(true); await run('refreshStaffingPlan()');
  assert.match(doc.getElementById('employeesStaffingPlan').textContent,/Could not load/);
  const resolvers=[]; dom.window.fetch=()=>new Promise(resolve=>resolvers.push(resolve));
  const older=run('refreshStaffingPlan()'), newer=run('refreshStaffingPlan()');
  assert.match(doc.getElementById('employeesStaffingPlan').textContent,/Loading/);
  const reply = remaining => ({ok:true,json:async()=>({employees:[],allottedHours:0,requiredHours:remaining,remainingHours:remaining})});
  resolvers[1](reply(90)); await newer; resolvers[0](reply(80)); await older;
  assert.equal(run('STAFFING_PLAN.remainingHours'),90);
  const oldFolder=run('refreshStaffingPlan()'); run('VIEW_REVISION++; FOLDER_ID=42; SELECTED=new Set([999])');
  const newFolder=run('refreshStaffingPlan()');
  resolvers[3](reply(90)); await newFolder;
  resolvers[2](reply(10)); await oldFolder;
  assert.equal(run('STAFFING_PLAN.remainingHours'),90);
  const pending=run('refreshStaffingPlan()'); run('clearSession()');
  resolvers[4](reply(70)); await pending;
  assert.equal(doc.getElementById('employeesStaffingPlan').textContent,'');
  assert.equal(doc.getElementById('diagnosticsStaffingPlan').textContent,'');
  assert.equal(run('EMPLOYEE_DRAFTS.size'),0);
  dom.window.close();
});
const config = {days:['Mon','Tue','Wed','Thu','Fri'], availabilityDays:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],hourStart:7,hourEnd:8,reqStaffOpen:1,reqStaffLate:1,slotNames:['DLA'],minShiftLength:1};
const overview = {employees:[{id:1,name:'Alex'},{id:2,name:'Blair'},{id:3,name:'Casey'}],availability:{Alex:{Mon_07:2,Sat_07:1,Sun_08:1},Blair:{Mon_07:1,Invalid_99:1},Casey:{}},missing:['Missing'],comments:{Blair:'<img src=x onerror=alert(1)>'},submittedAt:{Alex:'2026-01-01T00:00:00Z',Blair:'2026-01-01T00:00:00Z'}};
async function admin() {
  const dom = new JSDOM(fs.readFileSync(__dirname+'/public/admin.html','utf8'),{url:'http://localhost/admin.html',runScripts:'outside-only'});
  const ctx = dom.getInternalVMContext(), run = code => vm.runInContext(code,ctx);
  dom.window.fetch = async () => ({ok:true,status:200,json:async()=>structuredClone(overview)});
  for(const f of ['folders.js','admin.js']) run(fs.readFileSync(__dirname+'/public/js/'+f,'utf8'));
  run(`TOKEN='test'; CONFIG=${JSON.stringify(config)}; FOLDER_ID=1;`);
  await run('renderOverview(true)');
  return {dom,run,doc:dom.window.document};
}
test('viewer sorts seven days, pins across hover, unpins, separates missing and escapes comments',async()=>{
  const {dom,run,doc}=await admin();
  const [alex,blair,casey]=doc.querySelectorAll('[data-person]');
  assert.match(alex.textContent,/Alex — 3 hrs/); assert.match(blair.textContent,/Blair — 1 hrs/); assert.match(casey.textContent,/Casey — 0 hrs/);
  assert.match(doc.getElementById('overviewArea').textContent,/Missing/);
  alex.dispatchEvent(new dom.window.MouseEvent('mouseenter')); assert.match(doc.getElementById('viewerCaption').textContent,/Preview: Alex/);
  alex.click(); blair.dispatchEvent(new dom.window.MouseEvent('mouseenter')); assert.match(doc.getElementById('viewerCaption').textContent,/Pinned: Alex/);
  blair.click(); assert.match(doc.getElementById('viewerComment').textContent,/<img/); assert.equal(doc.querySelector('#viewerComment img'),null);
  assert.equal(run('SELECTED.size'),3);
  blair.click(); assert.match(doc.getElementById('viewerCaption').textContent,/All submissions/);
  alex.focus(); assert.match(doc.getElementById('viewerCaption').textContent,/Preview/);
  alex.click(); doc.body.click(); assert.equal(run('PINNED'),null);
  alex.click(); doc.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape'})); assert.equal(run('PINNED'),null);
  dom.window.close();
});
test('logout clears private data and discards pending responses',async()=>{
  const {dom,run,doc}=await admin();
  let resolve;
  dom.window.fetch=()=>new Promise(r=>resolve=r);
  const pending=run('apiGet("/api/availability")');
  run('clearSession()');
  resolve({ok:true,status:200,json:async()=>overview});
  assert.equal(await pending,null); assert.equal(doc.getElementById('overviewArea').textContent,'');
  assert.equal(run('OVERVIEW'),null); assert.equal(run('SELECTED.size'),0);
  dom.window.close();
});
test('Excel export uses saved settings even after current hours change',async()=>{
  const {dom,run}=await admin();
  // Run the shipped browser build in the page realm. ExcelJS uses instanceof
  // Array internally, so injecting Node's build rejects page-created rows.
  run(fs.readFileSync(require.resolve('exceljs/dist/exceljs.min.js'),'utf8'));
  run(`const BrowserWorkbook = ExcelJS.Workbook;
    ExcelJS.Workbook = class extends BrowserWorkbook {
      constructor() { super(); window.exportWorkbook = this; }
    };`);
  dom.window.URL.createObjectURL=()=> 'blob:test'; dom.window.URL.revokeObjectURL=()=>{};
  dom.window.HTMLAnchorElement.prototype.click=()=>{};
  const work={},schedule={}; for(const d of config.days){work[d]={7:['Alex'],8:[]};schedule[d]={7:{DLA:'Alex'},8:{DLA:''}};}
  run(`LAST_RESULT=${JSON.stringify({config,work,schedule,employees:[{name:'Alex'}],fairness:[{name:'Alex',hours:5,preferredMarked:6,preferredSatisfied:5,preferredPct:83.33,unwantedHours:0,dealScore:1}]})}; CONFIG={...CONFIG,hourStart:14,hourEnd:20};`);
  await run('downloadExcel()');
  // Reopen actual XLSX bytes with the independent Node reader.
  const bytes = await dom.window.exportWorkbook.xlsx.writeBuffer();
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.load(Buffer.from(bytes));
  assert.deepEqual(workbook.worksheets.map(s=>s.name),['Raw_Logic','Printable_Schedule','Fairness']);
  assert.equal(workbook.getWorksheet('Raw_Logic').rowCount,11);
  assert.equal(workbook.getWorksheet('Raw_Logic').getCell('B2').value,1);
  assert.equal(workbook.getWorksheet('Raw_Logic').getCell('B3').value,0);
  assert.equal(workbook.getWorksheet('Raw_Logic').getCell('A11').value,'Fri 8:00');
  assert.equal(workbook.getWorksheet('Fairness').getCell('A2').value,'Alex');
  assert.equal(workbook.getWorksheet('Fairness').getCell('B2').value,5);
  assert.equal(workbook.getWorksheet('Fairness').getCell('D2').value,5);
  assert.equal(workbook.getWorksheet('Printable_Schedule').rowCount,4);
  assert.equal(workbook.getWorksheet('Printable_Schedule').getCell('I1').value,'Day: Fri');
  assert.equal(workbook.getWorksheet('Raw_Logic').getCell('A2').value,'Mon 7:00');
  assert.equal(workbook.getWorksheet('Printable_Schedule').getCell('B3').value,'Alex');
  assert.equal(workbook.getWorksheet('Printable_Schedule').columnCount,10);
  dom.window.close();
});
test('employee saves preferences and comments to displayed folder and retains failed edits',async()=>{
  const dom=new JSDOM(fs.readFileSync(__dirname+'/public/index.html','utf8'),{url:'http://localhost/',runScripts:'outside-only'});
  const ctx=dom.getInternalVMContext(), run=code=>vm.runInContext(code,ctx), doc=dom.window.document;
  dom.window.HTMLElement.prototype.scrollIntoView=()=>{};
  dom.window.confirm=()=>true;
  let captured;
  dom.window.fetch=async(url,opts)=>{
    if(url==='/api/submission-context')return {ok:true,json:async()=>({folder:{id:4,name:'Fall'},revision:2,config})};
    if(url==='/api/roster')return {ok:true,json:async()=>({names:[],allowSelfRegister:true})};
    captured=JSON.parse(opts.body);return {ok:false,json:async()=>({error:'Please retry'})};
  };
  await run(fs.readFileSync(__dirname+'/public/js/employee.js','utf8'));
  assert.equal(doc.querySelectorAll('[data-day="Sat"]').length,2);
  doc.getElementById('nameInput').value='Alex';doc.getElementById('commentInput').value='Keep this';
  run('state={Mon_07:2,Sat_07:1}'); await run('submitAvailability()');
  assert.equal(captured.folderId,4);assert.equal(captured.revision,2);assert.equal(captured.availability.Mon_07,2);
  assert.equal(doc.getElementById('commentInput').value,'Keep this');assert.equal(run('state.Mon_07'),2);
  const cell=doc.querySelector('[data-key="Sun_07"]'); cell.dispatchEvent(new dom.window.MouseEvent('click',{bubbles:true,detail:0}));
  assert.equal(run('state.Sun_07'),1);
  doc.getElementById('commentInput').value='🙂'.repeat(99);run('updateCommentCount()');assert.match(doc.getElementById('commentCount').textContent,/99 \/ 99/);
  dom.window.close();
});


async function deletionAdmin({last=false}={}) {
  const t=await admin(), {dom,run,doc}=t;
  let folders=[{id:1,name:'Fall <2026>',archived:false},...last?[]:[{id:2,name:'Summer',archived:false}]];
  let active=1, fail=false, conflict=false, deletionCalls=0;
  const preview={folder:folders[0],counts:{availabilitySubmissions:3,weekdaySchedules:2,weekendSchedules:4},acceptsSubmissions:true,previewVersion:'version-1'};
  const requests=[];
  const reply=(data,ok=true,status=200)=>({ok,status,json:async()=>structuredClone(data)});
  const normalFetch=async(url,opts={})=>{
    requests.push({url,opts});
    if (url.endsWith('/deletion-preview')) return reply({...preview,folder:folders.find(f=>url.includes(`/folders/${f.id}/`))});
    if (opts.method==='DELETE' && /^\/api\/folders\/\d+$/.test(url)) {
      deletionCalls++;
      if (fail) return reply({error:'Delete failed'},false,503);
      if (conflict) {conflict=false;return reply({error:'Scope changed; confirm again'},false,409);}
      const id=Number(url.split('/').pop()); folders=folders.filter(f=>f.id!==id);if(active===id)active=null;
      return reply({ok:true,deletedFolderId:id});
    }
    if(url==='/api/folders')return reply({folders,activeFolderId:active});
    if(url==='/api/staffing-plan')return reply({employees:overview.employees,requiredHours:30,allottedHours:10,remainingHours:20});
    if(url.startsWith('/api/availability?'))return reply({...overview,availability:{},submissions:[],comments:{}});
    if(url.startsWith('/api/diagnostics?'))return reply({});
    if(url.endsWith('/schedules')||url.endsWith('/weekend_schedules'))return reply([]);
    throw Error('Unexpected request '+url);
  };
  dom.window.fetch=normalFetch;
  run(`FOLDERS=${JSON.stringify(folders)}; ACTIVE_FOLDER=1; FOLDER_ID=1; renderFolderControls();
    renderDiagnostics=()=>{}; LAST_RESULT={deleted:true}; LAST_DIAG={deleted:true}; LAST_WKND_PREVIEW={deleted:true};
    WKND_FIXED={friday_evening:1}; WKND_ROTATING_ORDER=[1]; WKND_EXCLUDED=[{date:'2026-10-01'}];`);
  for(const id of ['scheduleOutput','fairnessOutput','wkndPreviewArea','wkndSavedSchedules','savedSchedules','diagArea'])doc.getElementById(id).textContent='Deleted-folder data';
  doc.getElementById('downloadBtn').style.display='inline-block';
  return {...t,requests,normalFetch,reply,setFail:v=>fail=v,setConflict:v=>conflict=v,deletionCalls:()=>deletionCalls};
}

test('deletion dialog shows escaped exact name, counts, preservation, active warning and typed confirmation',async()=>{
  const t=await deletionAdmin(), {doc,run,dom}=t;
  await doc.getElementById('deleteFolderBtn').onclick();
  const details=doc.getElementById('deleteFolderDetails');
  assert.match(details.textContent,/Fall <2026>/); assert.equal(details.querySelector('2026'),null);
  for(const text of ['3 availability','2 saved weekday','4 saved weekend','cannot be undone','roster entries','other folders','stops submissions','No other folder will be activated'])assert.ok(details.textContent.includes(text));
  const input=doc.getElementById('deleteFolderConfirmation'),button=doc.getElementById('deleteFolderConfirmBtn');
  input.value='fall <2026>';input.oninput();assert.equal(button.disabled,true);
  input.value='Fall <2026> ';input.oninput();assert.equal(button.disabled,true);
  input.value='Fall <2026>';input.oninput();assert.equal(button.disabled,false);
  doc.getElementById('deleteFolderCancelBtn').onclick();assert.equal(doc.getElementById('deleteFolderDialog').open,false);
  assert.equal(t.deletionCalls(),0);assert.equal(run('LAST_RESULT.deleted'),true);dom.window.close();
});

test('committed deletion refreshes both lists, clears cached results and edit state, preserves roster planning',async()=>{
  const t=await deletionAdmin(), {doc,run,dom}=t;
  await run('openFolderDeletion()');
  run('editAvailEmployee={id:1}; editAvailState={Mon_07:2}; PINNED=1; PREVIEWED=1;');
  doc.getElementById('editAvailComment').value='Deleted comment';
  doc.getElementById('deleteFolderConfirmation').value='Fall <2026>';
  await run('confirmFolderDeletion()');
  assert.equal(run('FOLDER_ID'),2); assert.equal(run('ACTIVE_FOLDER'),null);
  assert.equal(run('LAST_RESULT'),null);assert.equal(run('LAST_WKND_PREVIEW'),null);assert.equal(run('LAST_DIAG'),null);
  assert.equal(run('SELECTED.size'),0);assert.equal(run('PINNED'),null);assert.equal(run('editAvailEmployee'),null);
  assert.equal(doc.getElementById('editAvailComment').value,'');
  assert.equal(run('WKND_ROTATING_ORDER.length'),0);assert.equal(run('Object.keys(WKND_FIXED).length'),0);
  assert.equal(doc.getElementById('wkndPreviewArea').textContent,'');assert.equal(doc.getElementById('downloadBtn').style.display,'none');
  assert.match(doc.getElementById('savedSchedules').textContent,/No saved/);assert.match(doc.getElementById('wkndSavedSchedules').textContent,/No saved/);
  assert.match(doc.getElementById('diagnosticsStaffingPlan').textContent,/20 hours remaining/);
  assert.match(doc.getElementById('folderMsg').textContent,/permanently deleted/);
  dom.window.close();
});

test('final-folder deletion leaves empty state and available shared-roster and creation controls',async()=>{
  const {run,doc,dom,requests}=await deletionAdmin({last:true});
  await run('openFolderDeletion()');doc.getElementById('deleteFolderConfirmation').value='Fall <2026>';
  await run('confirmFolderDeletion()');assert.equal(run('FOLDER_ID'),null);
  assert.match(doc.getElementById('overviewArea').textContent,/No folders yet/);
  for(const id of ['deleteFolderBtn','generateBtn','wkndGenerateBtn'])assert.equal(doc.getElementById(id).disabled,true);
  for(const id of ['createFolderBtn','addEmpBtn'])assert.equal(doc.getElementById(id).disabled,false);
  assert.match(doc.getElementById('employeesStaffingPlan').textContent,/20 hours remaining/);
  assert.ok(!requests.some(r=>/folderId=(null|0)|folders\/(null|0)/.test(r.url)));
  dom.window.close();
});

test('failure retains view and permits retry; conflict refresh requires freshly typed confirmation',async()=>{
  const t=await deletionAdmin(), {doc,run,dom}=t;
  t.setFail(true);await run('openFolderDeletion()');doc.getElementById('deleteFolderConfirmation').value='Fall <2026>';
  await run('confirmFolderDeletion()');assert.equal(run('FOLDER_ID'),1);assert.equal(run('LAST_RESULT.deleted'),true);
  assert.equal(doc.getElementById('scheduleOutput').textContent,'Deleted-folder data');
  assert.match(doc.getElementById('deleteFolderError').textContent,/Delete failed/);assert.equal(doc.getElementById('deleteFolderConfirmBtn').disabled,false);
  t.setFail(false);t.setConflict(true);await run('confirmFolderDeletion()');
  assert.equal(doc.getElementById('deleteFolderConfirmation').value,'');assert.equal(doc.getElementById('deleteFolderConfirmBtn').disabled,true);
  assert.match(doc.getElementById('deleteFolderError').textContent,/Scope changed/);
  assert.equal(run('FOLDER_ID'),1);dom.window.close();
});

test('busy deletion prevents repeated submits and captures its original folder across a view switch',async()=>{
  const t=await deletionAdmin(), {doc,run,dom}=t;
  await run('openFolderDeletion()');doc.getElementById('deleteFolderConfirmation').value='Fall <2026>';
  let release;
  dom.window.fetch=(url,opts)=>opts?.method==='DELETE'?new Promise(resolve=>release=()=>t.normalFetch(url,opts).then(resolve)):t.normalFetch(url,opts);
  const pending=run('confirmFolderDeletion()');
  for(const id of ['deleteFolderConfirmBtn','deleteFolderCancelBtn','deleteFolderConfirmation','deleteFolderBtn'])assert.equal(doc.getElementById(id).disabled,true);
  await run('confirmFolderDeletion()');assert.equal(t.deletionCalls(),0);
  doc.getElementById('folderSelect').value='2';await run('changeFolder()');
  release();await pending;
  const mutation=t.requests.find(r=>r.opts.method==='DELETE');assert.equal(mutation.url,'/api/folders/1');
  assert.equal(JSON.parse(mutation.opts.body).confirmationName,'Fall <2026>');assert.equal(run('FOLDER_ID'),2);assert.equal(t.deletionCalls(),1);
  dom.window.close();
});

test('switching folders or cancelling while preview loads discards the original confirmation target',async()=>{
  const t=await deletionAdmin(), {doc,run,dom}=t;
  let release;dom.window.fetch=(url,opts)=>url.endsWith('/deletion-preview')?new Promise(resolve=>release=()=>t.normalFetch(url,opts).then(resolve)):t.normalFetch(url,opts);
  const pending=run('openFolderDeletion()');doc.getElementById('folderSelect').value='2';await run('changeFolder()');
  release();await pending;assert.equal(run('DELETION_PREVIEW'),null);assert.equal(doc.getElementById('deleteFolderDialog').open,false);
  const second=run('openFolderDeletion()');run('cancelFolderDeletion()');release();await second;
  assert.equal(run('DELETION_PREVIEW'),null);await run('confirmFolderDeletion()');assert.equal(t.deletionCalls(),0);dom.window.close();
});

test('pending weekday, weekend and overview responses cannot repopulate a deleted view',async()=>{
  const t=await deletionAdmin({last:true}), {doc,run,dom}=t;
  await run('openFolderDeletion()');doc.getElementById('deleteFolderConfirmation').value='Fall <2026>';
  const held=[];
  dom.window.fetch=(url,opts)=>['/api/availability?folderId=1','/api/folders/1/schedules','/api/folders/1/weekend_schedules'].includes(url)?new Promise(resolve=>held.push({url,resolve})):t.normalFetch(url,opts);
  const pending=[run('renderOverview()'),run('loadSavedSchedules()'),run('renderSavedWeekendSchedules()')];
  await run('confirmFolderDeletion()');
  for(const h of held)h.resolve(t.reply(h.url.includes('availability')?overview:[{id:99,createdAt:'2026-01-01'}]));
  await Promise.all(pending);
  assert.equal(run('OVERVIEW'),null);assert.equal(run('SELECTED.size'),0);
  assert.match(doc.getElementById('overviewArea').textContent,/No folders yet/);
  assert.equal(doc.getElementById('savedSchedules').textContent,'');assert.equal(doc.getElementById('wkndSavedSchedules').textContent,'');dom.window.close();
});

test('weekend generate and save use API response data and retain the folder identity',async()=>{
  const {doc,run,dom}=await admin();
  const data={folderId:1,folderVersion:'2026-01-01',employees:[],assignments:[],rotating_counts:{},effective_pool:[]};
  let saved;
  dom.window.fetch=async(url,opts)=>{
    if(url==='/api/generate_weekend')return {ok:true,status:200,json:async()=>data};
    if(url==='/api/save_weekend'){saved=JSON.parse(opts.body);return {ok:true,status:200,json:async()=>({savedScheduleId:9})};}
    return {ok:true,status:200,json:async()=>[]};
  };
  await doc.getElementById('wkndGenerateBtn').onclick();assert.equal(run('LAST_WKND_PREVIEW.folderId'),1);
  await doc.getElementById('wkndSaveBtn').onclick();assert.equal(saved.snapshot.folderVersion,data.folderVersion);assert.equal(saved.folderId,1);
  assert.match(doc.getElementById('wkndMsg').textContent,/Saved successfully/);
  const stale=doc.getElementById('wkndSaveBtn').onclick;run('VIEW_REVISION++; FOLDER_ID=2; clearWeekendView()');saved=null;
  await stale();assert.equal(saved,null);dom.window.close();
});
