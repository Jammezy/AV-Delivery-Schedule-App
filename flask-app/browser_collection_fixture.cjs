// Synthetic fixture helpers. Every write follows the real code-authorized submission APIs.
const assert=require('node:assert/strict');
const {randomUUID}=require('node:crypto');
const {spawnSync}=require('node:child_process');
function testKeys(python){const r=spawnSync(python,['-c','from cryptography.fernet import Fernet;import secrets,json;print(json.dumps(dict(COLLECTION_ENCRYPTION_KEY=Fernet.generate_key().decode(),COLLECTION_VERIFIER_KEY=secrets.token_hex(32))))'],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);}
async function collectionFixture(page,base){
 const login=await page.request.post(base+'/api/admin/login',{data:{password:'browser-test'}});
 const headers={Authorization:'Bearer '+(await login.json()).token};
 const intake=async()=>(await (await page.request.get(base+'/api/admin/intake?folderId=1',{headers})).json());
 async function accept(name){const data=await intake();assert.ok(data.submissions.some(r=>r.name===name&&r.status==='Accepted'),'Automatically saved '+name);}
 async function open(employee,name){
  let data=await intake(),row=data.submissions.find(r=>r.name===name&&r.status==='Accepted');
  if(!row){
   const previous=await (await page.request.get(base+'/api/availability/'+encodeURIComponent(name)+'?folderId=1',{headers})).json();
   const made=await page.request.post(base+'/api/admin/codes',{headers,data:{folderId:1}});assert.equal(made.status(),201);
   const ctx=await (await page.request.post(base+'/api/collection/unlock',{data:{code:(await made.json()).code}})).json();
   const sent=await page.request.post(base+'/api/availability',{headers:{'X-Submission-CSRF':ctx.csrf},data:{name,availability:previous.availability,comment:previous.comment||'',folderId:1,revision:ctx.revision,requestId:randomUUID(),allowExtraOpenings:previous.consent.allowExtraOpenings,allowExtraClosings:previous.consent.allowExtraClosings,consentContext:ctx.boundaryContext.token}});assert.equal(sent.status(),200,await sent.text());
   await accept(name);data=await intake();row=data.submissions.find(r=>r.name===name&&r.status==='Accepted');
  }
  const grant=await page.request.post(base+`/api/admin/intake/${row.id}/edit-links`,{headers,data:{}});assert.equal(grant.status(),200,await grant.text());
  await employee.goto('about:blank');await employee.goto(base+'/#edit='+(await grant.json()).token);await employee.locator('#availabilityForm').waitFor({state:'visible'});
 }
 return {open,accept,headers};
}
module.exports={testKeys,collectionFixture};
