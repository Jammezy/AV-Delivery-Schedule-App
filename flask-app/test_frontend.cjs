const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const ExcelJS = require('exceljs');
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
