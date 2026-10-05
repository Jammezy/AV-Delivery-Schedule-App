"""Security regression checks on a disposable synthetic database only."""
import concurrent.futures, datetime as dt, json, os, secrets, tempfile, unittest, uuid
from pathlib import Path
from cryptography.fernet import Fernet
os.environ.setdefault('ADMIN_PASSWORD','test-admin')
os.environ.setdefault('COLLECTION_ENCRYPTION_KEY',Fernet.generate_key().decode())
os.environ.setdefault('COLLECTION_VERIFIER_KEY',secrets.token_hex(32))
test_pg=os.environ.get('TEST_CODES_POSTGRES_URL')
if test_pg:
 from urllib.parse import urlsplit
 parsed=urlsplit(test_pg)
 if parsed.hostname!='127.0.0.1' or not parsed.path.startswith('/codes_test_'):raise RuntimeError('Disposable local codes database required')
 os.environ['DATABASE_URL']=test_pg
else:os.environ.pop('DATABASE_URL',None)
os.environ.setdefault('DATABASE_PATH',str(Path(tempfile.mkdtemp())/'initial.db'))
import app as web
from unittest.mock import patch
from flask.testing import FlaskClient
from models import *

class CollectionTests(unittest.TestCase):
 def setUp(self):
  web.app.config['REQUEST_RETENTION_ENABLED']=False
  self.tmp=tempfile.TemporaryDirectory();db.close()
  if test_pg:
   with db.connection_context():db.drop_tables([SchemaMigration,FolderEmployee,FolderConfig,RateBucket,SubmissionSession,EditGrant,IntakeSubmission,CollectionCode,CollectionSettings,AdminSession,SavedWeekendSchedule,SavedSchedule,FolderAvailability,SubmissionState,Folder,Availability,Employee,Config],cascade=True)
  else:db.init(str(Path(self.tmp.name)/'test.db'))
  init_db()
  self.client=FlaskClient(web.app)
  token=self.client.post('/api/admin/login',json={'password':web.ADMIN_PASSWORD}).json['token']
  self.admin={'Authorization':'Bearer '+token};self.ctx=self.client.get('/api/submission-context').json;self.fid=self.ctx['folder']['id']
 def tearDown(self):db.close();self.tmp.cleanup()
 def code(self,**kw):
  r=self.client.post('/api/admin/codes',headers=self.admin,json=dict({'folderId':self.fid},**kw));self.assertEqual(r.status_code,201,r.json);return r.json
 def unlock(self,code,c=None):
  r=(c or self.client).post('/api/collection/unlock',json={'code':code['code']});self.assertEqual(r.status_code,200,r.json);return {'X-Submission-CSRF':r.json['csrf']}
 def submit(self,h,c=None,reuse=False,**kw):
  client = c or self.client
  cookie = client.get_cookie('availability_session',path='/api/')
  if cookie and h and not reuse and 'requestId' not in kw:
   from collection_codes import digest, code_status
   with db.connection_context():
    session = SubmissionSession.get_or_none(SubmissionSession.token_hash==digest(cookie.value))
    code = session.code if session and session.submitted_request_key and not session.edit_grant_id and code_status(session.code)=='Active' else None
    raw = Fernet(os.environ['COLLECTION_ENCRYPTION_KEY'].encode()).decrypt(code.encrypted_code.encode()).decode() if code else None
   if raw:
    fresh=self.unlock({'code':raw},client)
    h.clear();h.update(fresh)
  with db.connection_context():
   cookie = client.get_cookie('availability_session',path='/api/')
   from collection_codes import digest
   current_session=SubmissionSession.get_or_none(SubmissionSession.token_hash==digest(cookie.value)) if cookie else None
   revision=current_session.state_revision if current_session else self.ctx['revision']
  edit_version=(client.get('/api/collection/context').json or {}).get('edit',{}).get('permissionVersion') if current_session and current_session.edit_grant_id else None
  data=dict(name='Alex',folderId=self.fid,revision=revision,availability={'Mon_07':2},comment='Synthetic',requestId=uuid.uuid4().hex);
  if edit_version: data['permissionVersion']=edit_version
  data.update(kw)
  return (c or self.client).post('/api/availability',headers=h,json=data)
 def intake(self):return self.client.get('/api/admin/intake?folderId='+str(self.fid),headers=self.admin).json
 def accept(self,row,eid,version=None):return self.client.post(f'/api/admin/intake/{row["id"]}/review',headers=self.admin,json=dict(action='accept',employeeId=eid,reviewVersion=row['reviewVersion'],acceptedVersion=version))
 def add_responses(self,code,amount,client=None,headers=None):
  return (client or self.client).post(f'/api/admin/codes/{code["id"]}/responses',headers=self.admin if headers is None else headers,json={'additionalResponses':amount})
 def test_folder_defaults_and_saved_limits_survive_restart(self):
  with db.connection_context():CollectionSettings.create(folder=self.fid,response_cap=300,received=12)
  new=self.client.post('/api/folders',headers=self.admin,json={'name':'Next week','activate':False}).json
  listed=self.client.get(f'/api/admin/codes?folderId={new["id"]}',headers=self.admin).json
  self.assertEqual(listed['collection'],{'received':0,'responseCap':100});self.assertEqual(listed['codes'],[])
  init_db()
  with db.connection_context():
   self.assertEqual(CollectionSettings.get(CollectionSettings.folder==self.fid).response_cap,300)
   self.assertEqual(CollectionSettings.get(CollectionSettings.folder==self.fid).received,12)
   self.assertEqual(CollectionSettings.get(CollectionSettings.folder==new['id']).response_cap,100)
 def test_folder_filtering_destination_and_live_code_allowance(self):
  one=self.code();h=self.unlock(one);self.add_responses(one,2)
  context=self.client.get('/api/collection/context').json
  self.assertEqual(context['responseLimit'],32);self.assertEqual(context['folder']['id'],self.fid)
  folder=self.client.post('/api/folders',headers=self.admin,json={'name':'Other folder','activate':False}).json
  two=self.code(folderId=folder['id']);self.assertEqual(two['status'],'Active')
  other=self.client.get(f'/api/admin/codes?folderId={folder["id"]}',headers=self.admin).json
  self.assertEqual([c['id'] for c in other['codes']],[two['id']]);self.assertEqual(other['collection']['responseCap'],100)
  self.assertEqual(self.client.get('/api/submission-context').json['folder']['id'],self.fid)
  self.assertEqual(self.submit(h,folderId=folder['id']).status_code,409)
  self.assertEqual(self.submit(h).status_code,200)
  self.client.patch(f'/api/folders/{folder["id"]}',headers=self.admin,json={'activate':True})
  self.assertEqual(self.submit(h).status_code,200)
  second_context=self.client.post('/api/collection/unlock',json={'code':two['code']}).json
  self.assertEqual(second_context['folder']['id'],folder['id']);self.assertEqual(second_context['responseLimit'],30)
  self.assertEqual(self.submit({'X-Submission-CSRF':second_context['csrf']},folderId=folder['id'],revision=second_context['revision']).status_code,200)
  with db.connection_context():
   self.assertEqual([(r.folder_id,r.code_id) for r in IntakeSubmission.select().order_by(IntakeSubmission.id)],[(self.fid,one['id']),(self.fid,one['id']),(folder['id'],two['id'])])
 # Isolate quota accounting from deliberately colliding fixed rate-limit buckets.
 @patch('collection_codes.rate_limit', lambda *args, **kwargs: None)
 def test_default_folder_cap_across_multiple_codes(self):
  one=self.code();two=self.code();self.add_responses(one,60);self.add_responses(two,10)
  h=self.unlock(one)
  for _ in range(90):self.assertEqual(self.submit(h).status_code,200)
  h=self.unlock(two)
  for _ in range(10):self.assertEqual(self.submit(h).status_code,200)
  blocked=self.submit(h);self.assertEqual(blocked.status_code,409);self.assertIn('folder has reached its total submission limit',blocked.json['error'])
  with db.connection_context():self.assertEqual(CollectionSettings.get().received,100)
 def test_add_responses_to_exhausted_code(self):
  one=self.code();two=self.code();self.assertEqual(one['responseLimit'],30);h=self.unlock(one)
  for _ in range(30):self.assertEqual(self.submit(h).status_code,200)
  self.assertEqual(self.submit(h).status_code,409)
  added=self.add_responses(one,2);self.assertEqual(added.status_code,200);self.assertEqual((added.json['responseLimit'],added.json['received'],added.json['remaining'],added.json['status']),(32,30,2,'Active'))
  fresh=FlaskClient(web.app);self.unlock(one,fresh)
  for _ in range(2):self.assertEqual(self.submit(h).status_code,200)
  self.assertEqual(self.submit(h).status_code,409)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(two['id']).response_limit,30)
  self.assertEqual(self.code()['responseLimit'],30)
 def test_add_responses_permissions_validation_and_closed_codes(self):
  code=self.code()
  self.assertEqual(self.add_responses(code,2,headers={}).status_code,401)
  for amount in [None,True,False,'2',2.5,0,-1,9971]:self.assertEqual(self.add_responses(code,amount).status_code,400)
  self.assertEqual(self.add_responses({'id':999999},1).status_code,404)
  self.assertEqual(self.add_responses(code,9970).json['responseLimit'],10000)
  self.assertEqual(self.add_responses(code,1).status_code,400)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(code['id']).response_limit,10000)
  for deleted in [False,True]:
   stopped=self.code();self.client.open(f'/api/admin/codes/{stopped["id"]}',method='DELETE' if deleted else 'PATCH',headers=self.admin,json={})
   self.assertEqual(self.add_responses(stopped,2).status_code,409)
   with db.connection_context():self.assertEqual(CollectionCode.get_by_id(stopped['id']).response_limit,30)
 def test_add_responses_does_not_reopen_expired_or_closed_collection(self):
  code=self.code();h=self.unlock(code)
  self.client.patch(f'/api/folders/{self.fid}',headers=self.admin,json={'activate':False})
  self.assertEqual(self.add_responses(code,2).json['status'],'Active');self.assertEqual(self.submit(h).status_code,200)
  with db.connection_context():CollectionCode.update(expires_at=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
  self.assertEqual(self.add_responses(code,2).json['status'],'Expired');self.assertEqual(self.submit(h).status_code,409)
 def test_add_responses_keeps_period_cap(self):
  code=self.code();h=self.unlock(code);self.client.put(f'/api/admin/collections/{self.fid}',headers=self.admin,json={'responseCap':30})
  for _ in range(30):self.assertEqual(self.submit(h).status_code,200)
  added=self.add_responses(code,2);self.assertEqual(added.json['status'],'Collection paused');self.assertEqual(added.json['remaining'],2)
  self.assertEqual(self.submit(h).status_code,409)
  self.client.put(f'/api/admin/collections/{self.fid}',headers=self.admin,json={'responseCap':32})
  self.assertEqual(self.submit(h).status_code,200)
  with db.connection_context():self.assertEqual(CollectionSettings.get().received,31)
 def test_parallel_additions_and_increased_limit(self):
  code=self.code()
  def add(_):return self.add_responses(code,1,client=FlaskClient(web.app)).status_code
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(list(pool.map(add,range(2))),[200,200])
  h=self.unlock(code);cookie=self.client.get_cookie('availability_session',path='/api/').value
  def send(i):
   c=FlaskClient(web.app);opened=c.post('/api/collection/unlock',json={'code':code['code']})
   if opened.status_code!=200:return opened.status_code
   return self.submit({'X-Submission-CSRF':opened.json['csrf']},c,name=f'Added {i}').status_code
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:statuses=list(pool.map(send,range(40)))
  self.assertEqual(statuses.count(200),32,statuses);self.assertEqual(statuses.count(409),8,statuses)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(code['id']).response_limit,32);self.assertEqual(IntakeSubmission.select().count(),32)
 def test_response_limit_migration_preserves_existing_data(self):
  code=self.code();h=self.unlock(code);self.submit(h);self.add_responses(code,2)
  init_db()
  with db.connection_context():
   row=CollectionCode.get_by_id(code['id']);self.assertEqual(row.response_limit,32);cipher=row.encrypted_code;verifier=row.verifier
   db.execute_sql('ALTER TABLE collectioncode DROP COLUMN response_limit')
  init_db();init_db()
  with db.connection_context():
   row=CollectionCode.get_by_id(code['id']);self.assertEqual((row.response_limit,row.received,row.encrypted_code,row.verifier),(30,1,cipher,verifier))
   self.assertEqual(IntakeSubmission.select().count(),1);self.assertEqual(SubmissionSession.select().count(),1)
  self.assertEqual(self.submit(h).status_code,200)
 def test_gate_privacy_and_permissions(self):
  self.assertEqual(self.submit({}).status_code,401)
  code=self.code();h=self.unlock(code);self.assertEqual(self.submit({}).status_code,403);self.assertEqual(self.submit(h).status_code,200)
  for method,url in [('get','/api/roster'),('get','/api/availability/Alex?folderId=1'),('get','/api/admin/codes?folderId=1'),('get','/api/admin/intake?folderId=1'),('post','/api/admin/codes'),('patch',f'/api/admin/codes/{code["id"]}'),('delete',f'/api/admin/codes/{code["id"]}'),('post','/api/admin/intake/1/review')]:
   self.assertEqual(getattr(self.client,method)(url).status_code,401)
  with db.connection_context():self.assertEqual(Employee.select().count(),1);self.assertEqual(FolderAvailability.select().count(),1);self.assertEqual(IntakeSubmission.select().count(),1)
  self.assertNotIn('edit',self.client.get('/api/collection/context').json)
 def test_quota_independent_codes_and_cumulative_counts(self):
  one=self.code(label='First');two=self.code(label='More');h=self.unlock(one)
  for i in range(30):self.assertEqual(self.submit(h,name=f'Person {i}').status_code,200)
  self.assertEqual(self.submit(h).status_code,409);h=self.unlock(two);self.assertEqual(self.submit(h).status_code,200)
  rows=self.client.get('/api/admin/codes?folderId=1',headers=self.admin).json['codes'];self.assertEqual([(r['received'],r['remaining'],r['status']) for r in rows],[(1,29,'Active'),(30,0,'Exhausted')])
  row=self.intake()['submissions'][0];self.client.post(f'/api/admin/intake/{row["id"]}/review',headers=self.admin,json={'action':'reject','reviewVersion':0})
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(two['id']).received,1)
  self.assertEqual(self.client.get('/api/admin/codes?folderId=1',headers=self.admin).headers['Cache-Control'],'no-store')
 def test_validation_and_idempotency(self):
  code=self.code();h=self.unlock(code)
  for kw in [dict(availability=[]),dict(name=''),dict(comment='x'*100),dict(availability={'Bad':1}),dict(allowExtraOpenings='false'),dict(unexpected='x')]:self.assertEqual(self.submit(h,**kw).status_code,400)
  rid=uuid.uuid4().hex;self.assertEqual(self.submit(h,requestId=rid).status_code,200);self.assertEqual(self.submit(h,requestId=rid).status_code,200);self.assertEqual(self.submit(h,requestId=rid,comment='Changed').status_code,409)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(code['id']).received,1);self.assertEqual(IntakeSubmission.select().count(),1)
  self.assertEqual(self.submit(h,comment='Latest').status_code,200);self.assertEqual(self.submit(h,reuse=True).status_code,409)
  with db.connection_context():self.assertEqual(FolderAvailability.select().count(),2);self.assertEqual(CollectionCode.get_by_id(code['id']).received,2)
 def test_parallel_limit(self):
  code=self.code();h=self.unlock(code);cookie=self.client.get_cookie('availability_session',path='/api/').value
  def send(i):
   c=FlaskClient(web.app);opened=c.post('/api/collection/unlock',json={'code':code['code']})
   if opened.status_code!=200:return opened.status_code
   return self.submit({'X-Submission-CSRF':opened.json['csrf']},c,name='Alex',comment=f'Response {i}').status_code
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:statuses=list(pool.map(send,range(45)))
  self.assertEqual(statuses.count(200),30,statuses);self.assertEqual(statuses.count(409),15,statuses)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(code['id']).received,30);self.assertEqual(IntakeSubmission.select().count(),30);self.assertEqual(Employee.select().count(),30);self.assertEqual(FolderAvailability.select().count(),30);self.assertEqual(len({e.name.casefold() for e in Employee.select()}),30)
 def test_revoke_and_delete(self):
  one=self.code();two=self.code();h=self.unlock(one);self.submit(h)
  self.client.patch(f'/api/admin/codes/{one["id"]}',headers=self.admin,json={});self.assertEqual(self.submit(h).status_code,409)
  h=self.unlock(two);self.submit(h);self.client.delete(f'/api/admin/codes/{two["id"]}',headers=self.admin);self.assertEqual(self.submit(h).status_code,409)
  normal=self.client.get('/api/admin/codes?folderId=1',headers=self.admin).json['codes'];allrows=self.client.get('/api/admin/codes?folderId=1&showDeleted=1',headers=self.admin).json['codes'];self.assertEqual(len(normal),1);self.assertEqual(len(allrows),2)
  with db.connection_context():self.assertEqual(IntakeSubmission.select().count(),2)
 def test_automatic_create_preserves_same_name_and_employee_settings(self):
  h=self.unlock(self.code());first_response=self.submit(h,comment='First');self.assertFalse(first_response.json['pendingReview']);first=self.intake()['submissions'][0]
  with db.connection_context():
   employee=Employee.get();self.assertEqual((employee.name,employee.is_lead,employee.min_hours,employee.max_hours),('Alex',False,0,40))
   employee.is_lead=True;employee.min_hours=5;employee.max_hours=20;employee.save()
   other=Folder.create(name='Preserved');FolderAvailability.create(folder=other,employee=employee,comment='Other folder');SavedSchedule.create(folder=self.fid,snapshot_json='{"original":true}')
  self.assertEqual(self.submit(h,name='  aLeX  ',comment='Second',availability={'Sat_07':2}).status_code,200)
  with db.connection_context():
   self.assertEqual(Employee.select().count(),2);employee=Employee.get(Employee.name=='Alex');self.assertEqual((employee.is_lead,employee.min_hours,employee.max_hours),(True,5,20))
   current=FolderAvailability.get((FolderAvailability.folder==self.fid)&(FolderAvailability.employee==Employee.get(Employee.name=='aLeX (2)')));self.assertEqual(current.comment,'Second');self.assertEqual(current.get_data(),{'Sat_07':2});self.assertEqual(FolderAvailability.get((FolderAvailability.folder==self.fid)&(FolderAvailability.employee==employee)).comment,'First')
   self.assertEqual(FolderAvailability.get(FolderAvailability.folder==other).comment,'Other folder');self.assertEqual(SavedSchedule.get().snapshot_json,'{"original":true}')
   self.assertEqual(IntakeSubmission.get_by_id(first['id']).comment,'First');self.assertEqual(IntakeSubmission.get_by_id(first['id']).status,'Accepted')
   self.assertEqual(IntakeSubmission.select().where(IntakeSubmission.status=='Accepted').count(),2)
 def test_automatic_save_rolls_back_employee_history_and_quota(self):
  from peewee import OperationalError
  code=self.code();h=self.unlock(code)
  with patch('collection_codes.FolderAvailability.save',side_effect=OperationalError('synthetic failure')):
   self.assertIn(self.submit(h).status_code,(500,503))
  with db.connection_context():
   self.assertEqual(Employee.select().count(),0);self.assertEqual(FolderAvailability.select().count(),0);self.assertEqual(IntakeSubmission.select().count(),0)
   self.assertEqual(CollectionCode.get_by_id(code['id']).received,0);self.assertEqual(CollectionSettings.get().received,0)
 def test_import_pending_preserves_newer_edits_and_counts(self):
  from collection_codes import import_pending_responses
  code=self.code()
  with db.connection_context():
   employee=Employee.create(name='Alex');FolderAvailability.create(employee=employee,folder=self.fid,comment='Newer supervisor edit')
   common=dict(folder=self.fid,code=code['id'],data_json='{"Mon_07":2}',payload_hash='legacy')
   old=IntakeSubmission.create(**common,name='Alex',comment='Old pending',submitted_at=dt.datetime.utcnow()-dt.timedelta(days=1),request_key='old')
   one=IntakeSubmission.create(**common,name='Bea',comment='First pending',submitted_at=dt.datetime.utcnow()-dt.timedelta(hours=2),request_key='first')
   two=IntakeSubmission.create(**common,name='Bea',comment='Latest pending',request_key='latest')
   rejected=IntakeSubmission.create(**common,name='Excluded',status='Rejected',request_key='rejected')
   CollectionCode.update(received=4).where(CollectionCode.id==code['id']).execute();CollectionSettings.update(received=4).execute()
  import_pending_responses(web.permission_version);import_pending_responses(web.permission_version)
  with db.connection_context():
   self.assertEqual(FolderAvailability.get(FolderAvailability.employee==employee).comment,'Newer supervisor edit')
   self.assertEqual(FolderAvailability.get(FolderAvailability.employee==Employee.get(Employee.name=='Bea')).comment,'First pending')
   self.assertEqual(IntakeSubmission.get_by_id(old.id).status,'Accepted');self.assertEqual(IntakeSubmission.get_by_id(one.id).status,'Accepted');self.assertEqual(IntakeSubmission.get_by_id(two.id).status,'Accepted')
   self.assertEqual(IntakeSubmission.get_by_id(rejected.id).status,'Rejected');self.assertIsNone(Employee.get_or_none(Employee.name=='Excluded'))
   self.assertEqual(CollectionCode.get_by_id(code['id']).received,4);self.assertEqual(CollectionSettings.get().received,4)
 def test_edit_scope_expiry_and_revocation(self):
  code=self.code();h=self.unlock(code);self.submit(h,comment='Original');row=self.intake()['submissions'][0]
  link=self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={}).json;c=FlaskClient(web.app)
  opened=c.post('/api/collection/unlock',json={'editToken':link['token']});self.assertEqual(opened.status_code,200);self.assertEqual(opened.json['edit']['comment'],'Original');eh={'X-Submission-CSRF':opened.json['csrf']}
  self.assertEqual(self.submit(eh,c,name='Someone else').status_code,400);self.assertEqual(self.submit(eh,c,comment='Correction').status_code,200)
  with db.connection_context():self.assertEqual(IntakeSubmission.select().count(),2);self.assertEqual(CollectionCode.get_by_id(code['id']).received,2);self.assertEqual(IntakeSubmission.get_by_id(row['id']).comment,'Original');self.assertEqual(FolderAvailability.get(FolderAvailability.employee==row['employeeId']).comment,'Correction');self.assertEqual(FolderAvailability.select().count(),1)
  self.assertEqual(c.post('/api/collection/unlock',json={'editToken':link['token']}).status_code,401)
  self.client.delete(f'/api/admin/edit-links/{link["grantId"]}',headers=self.admin);self.assertEqual(self.submit(eh,c).status_code,401)
  link=self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={}).json
  with db.connection_context():EditGrant.update(expires_at=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
  self.assertEqual(c.post('/api/collection/unlock',json={'editToken':link['token']}).status_code,401)
 def admin_edit(self, eid, comment, version=None, availability=None):
  if version is None:
   version=next(r['permissionVersion'] for r in self.client.get(f'/api/availability?folderId={self.fid}',headers=self.admin).json['submissions'] if r['employeeId']==eid)
  return self.client.put('/api/admin/availability',headers=self.admin,json=dict(employeeId=eid,folderId=self.fid,permissionVersion=version,availability=availability or {'Tue_08':1},comment=comment))
 def link(self, row):
  r=self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={});self.assertEqual(r.status_code,200,r.json);return r.json
 def open_link(self, row):
  c=FlaskClient(web.app);r=c.post('/api/collection/unlock',json={'editToken':self.link(row)['token']});self.assertEqual(r.status_code,200,r.json)
  return c,r.json,{'X-Submission-CSRF':r.json['csrf']}
 def test_edit_link_live_state_same_employee_history_and_solver_input(self):
  code=self.code();self.submit(self.unlock(code),comment='Original');row=self.intake()['submissions'][0];eid=row['employeeId']
  link=self.link(row)
  self.assertEqual(self.admin_edit(eid,'Administrator saved').status_code,200)
  c=FlaskClient(web.app);opened=c.post('/api/collection/unlock',json={'editToken':link['token']}).json
  self.assertEqual(opened['edit']['comment'],'Administrator saved');self.assertEqual(opened['edit']['availability'],{'Tue_08':1})
  h={'X-Submission-CSRF':opened['csrf']}
  self.assertEqual(self.submit(h,c,permissionVersion=opened['edit']['permissionVersion'],comment='Employee correction',availability={'Wed_09':2}).status_code,200)
  rows=self.intake()['submissions'];correction=rows[0]
  self.assertEqual(correction['employeeId'],eid);self.assertEqual(correction['parentId'],row['id']);self.assertEqual(rows[1]['comment'],'Original')
  with db.connection_context():
   self.assertEqual(Employee.select().where(Employee.id==eid).count(),1);self.assertEqual(FolderAvailability.select().count(),1)
   _,_,availability,_=web._load_inputs(self.fid,[eid]);self.assertEqual(availability['Alex'],{'Wed_09':2})
  c,context,h=self.open_link(correction);self.assertEqual(context['edit']['comment'],'Employee correction')
  self.assertEqual(self.admin_edit(eid,'Later admin').status_code,200)
  self.assertEqual(c.get('/api/collection/context').json['edit']['comment'],'Later admin')
  stale=self.submit(h,c,permissionVersion=context['edit']['permissionVersion'],comment='Stale employee');self.assertEqual(stale.status_code,409);self.assertIn('Reload current availability',stale.json['error'])
  fresh=c.get('/api/collection/context').json
  self.assertEqual(self.submit(h,c,permissionVersion=fresh['edit']['permissionVersion'],comment='Latest employee').status_code,200)
  self.assertEqual(self.admin_edit(eid,'Stale admin',version=fresh['edit']['permissionVersion']).status_code,409)
  with db.connection_context():self.assertEqual(FolderAvailability.get(FolderAvailability.employee==eid).comment,'Latest employee');self.assertEqual(IntakeSubmission.select().count(),3)
 def test_edit_link_missing_records_does_not_recreate_or_match_name(self):
  self.submit(self.unlock(self.code()));row=self.intake()['submissions'][0];link=self.link(row)
  with db.connection_context():FolderAvailability.delete().where(FolderAvailability.employee==row['employeeId']).execute()
  c=FlaskClient(web.app);r=c.post('/api/collection/unlock',json={'editToken':link['token']});self.assertEqual(r.status_code,404)
  self.assertEqual(self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={}).status_code,404)
  self.assertFalse(self.intake()['submissions'][0]['canCreateEditLink'])
  self.assertEqual(self.admin_edit(row['employeeId'],'Do not recreate',version='old').status_code,404)
  with db.connection_context():self.assertEqual(FolderAvailability.select().count(),0)
 def test_link_exposes_full_saved_collection_under_narrow_scheduling_hours(self):
  self.submit(self.unlock(self.code()));row=self.intake()['submissions'][0]
  narrowed=self.client.put(f'/api/folders/{self.fid}/config',headers=self.admin,json=dict(hourStart=9,hourEnd=16,lateHourStart=17,dayCloseHours={'Fri':13}))
  self.assertEqual(narrowed.status_code,200,narrowed.json)
  self.assertEqual(self.admin_edit(row['employeeId'],'Full saved grid',availability={'Mon_07':2,'Fri_21':1,'Sun_16':1}).status_code,200)
  c,context,h=self.open_link(row)
  self.assertEqual((context['config']['hourStart'],context['config']['hourEnd']),(7,21));self.assertEqual(context['config']['dayCloseHours']['Fri'],22)
  self.assertEqual(self.submit(h,c,permissionVersion=context['edit']['permissionVersion'],availability=context['edit']['availability']).status_code,200)
 def test_open_employee_form_conflicts_with_permission_save_and_deleted_employee(self):
  self.submit(self.unlock(self.code()));row=self.intake()['submissions'][0];c,context,h=self.open_link(row)
  saved=self.client.put('/api/admin/boundary-permissions',headers=self.admin,json=dict(employeeId=row['employeeId'],folderId=self.fid,permissionVersion=context['edit']['permissionVersion'],consentContext=context['boundaryContext']['token'],allowExtraOpenings=True,allowExtraClosings=False))
  self.assertEqual(saved.status_code,200)
  self.assertEqual(self.submit(h,c,permissionVersion=context['edit']['permissionVersion']).status_code,409)
  fresh=c.get('/api/collection/context').json;self.assertTrue(fresh['edit']['consent']['allowExtraOpenings'])
  self.client.put(f'/api/folders/{self.fid}/config',headers=self.admin,json={'maxMorningShifts':4})
  self.assertTrue(c.get('/api/collection/context').json['edit']['consent']['reconfirmationNeeded'])
  with db.connection_context():
   FolderAvailability.delete().where(FolderAvailability.employee==row['employeeId']).execute()
   FolderEmployee.delete().where(FolderEmployee.employee==row['employeeId']).execute()
   Employee.delete().where(Employee.id==row['employeeId']).execute()
  self.assertEqual(c.get('/api/collection/context').status_code,404)
 def test_history_folder_isolation_and_separate_same_names(self):
  code=self.code();h=self.unlock(code)
  for n in range(25):self.assertEqual(self.submit(h,name='Repeat' if n<2 else f'Person {n}').status_code,200)
  rows=self.intake()['submissions'];self.assertEqual(len(rows),25);self.assertNotEqual(rows[-1]['employeeId'],rows[-2]['employeeId']);self.assertTrue(all(r['canCreateEditLink'] for r in rows))
  folder=self.client.post('/api/folders',headers=self.admin,json={'name':'Empty','activate':False}).json
  self.assertEqual(self.client.get(f'/api/admin/intake?folderId={folder["id"]}',headers=self.admin).json['submissions'],[])
 def test_simultaneous_admin_employee_saves_commit_only_one(self):
  self.submit(self.unlock(self.code()));row=self.intake()['submissions'][0];c,context,h=self.open_link(row)
  version=context['edit']['permissionVersion'];import threading
  barrier=threading.Barrier(2)
  def admin_save():
   client=FlaskClient(web.app);barrier.wait()
   r=client.put('/api/admin/availability',headers=self.admin,json=dict(employeeId=row['employeeId'],folderId=self.fid,permissionVersion=version,availability={'Tue_08':1},comment='Admin winner'))
   return r.status_code,'Admin winner'
  def employee_save():
   barrier.wait();r=self.submit(h,c,permissionVersion=version,comment='Employee winner');return r.status_code,'Employee winner'
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   one=pool.submit(admin_save);two=pool.submit(employee_save);results=[one.result(),two.result()]
  self.assertEqual(sorted(r[0] for r in results),[200,409])
  with db.connection_context():
   current=FolderAvailability.get(FolderAvailability.employee==row['employeeId']);self.assertEqual(current.comment,next(comment for status,comment in results if status==200))
   self.assertEqual(IntakeSubmission.select().count(),2 if current.comment=='Employee winner' else 1)
   self.assertEqual(CollectionCode.get().received,IntakeSubmission.select().count())
 def test_additive_migration_preserves_old_links_sessions_and_history(self):
  if test_pg:self.skipTest('SQLite legacy schema fixture')
  self.submit(self.unlock(self.code()));row=self.intake()['submissions'][0]
  with db.connection_context(),write_transaction():
   SubmissionSession.drop_table();EditGrant.drop_table()
   db.execute_sql('ALTER TABLE folderavailability DROP COLUMN save_version')
   db.execute_sql('CREATE TABLE editgrant (id INTEGER PRIMARY KEY, submission_id INTEGER NOT NULL REFERENCES intakesubmission(id) ON DELETE CASCADE, token_hash VARCHAR(255) NOT NULL UNIQUE, expires_at DATETIME NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, redeemed INTEGER NOT NULL DEFAULT 0)')
   db.execute_sql('INSERT INTO editgrant (id,submission_id,token_hash,expires_at) VALUES (1,?,?,?)',(row['id'],'legacy-token',dt.datetime.utcnow()+dt.timedelta(hours=1)))
   SubmissionSession.create_table();SubmissionSession.create(code=row['codeId'],token_hash='legacy-session',state_revision=0,expires_at=dt.datetime.utcnow()+dt.timedelta(hours=1),edit_grant=1)
  init_db();init_db()
  with db.connection_context():
   grant=EditGrant.get_by_id(1);self.assertEqual((grant.employee_id,grant.folder_id),(row['employeeId'],self.fid));self.assertEqual(SubmissionSession.get().edit_grant_id,1)
   self.assertEqual(IntakeSubmission.get_by_id(row['id']).comment,'Synthetic');self.assertEqual(FolderAvailability.get().save_version,0)
   FolderAvailability.delete().execute();FolderEmployee.delete().execute();Employee.delete().where(Employee.id==row['employeeId']).execute();self.assertIsNone(EditGrant.get_by_id(1).employee_id)
 def test_close_reopen_and_expiry(self):
  code=self.code();h=self.unlock(code);self.client.patch(f'/api/folders/{self.fid}',headers=self.admin,json={'archived':True});self.assertEqual(self.submit(h).status_code,409)
  self.client.patch(f'/api/folders/{self.fid}',headers=self.admin,json={'archived':False});self.assertEqual(self.submit(h).status_code,200)
  self.ctx=self.client.get('/api/submission-context').json;h=self.unlock(code);self.assertEqual(self.submit(h).status_code,200)
  with db.connection_context():CollectionCode.update(expires_at=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
  self.assertEqual(self.submit(h).status_code,409)
 def test_encrypted_storage_and_bounded_rate_limits(self):
  code=self.code()
  with db.connection_context():row=CollectionCode.get_by_id(code['id']);self.assertNotIn(code['code'],row.encrypted_code);self.assertNotEqual(row.verifier,code['code'])
  with patch('collection_codes.time.time',return_value=1800000000):
   statuses=[self.client.post('/api/collection/unlock',json={'code':'invalid'},headers={'X-Forwarded-For':f'198.51.100.{i}'}).status_code for i in range(160)]
  self.assertIn(429,statuses)
  with db.connection_context():self.assertEqual(RateBucket.select().count(),2);self.assertEqual(SubmissionSession.select().count(),0);self.assertEqual(IntakeSubmission.select().count(),0)
 def test_period_cap_and_folder_deletion(self):
  code=self.code();h=self.unlock(code);self.client.put(f'/api/admin/collections/{self.fid}',headers=self.admin,json={'responseCap':30})
  for _ in range(30):self.submit(h)
  second=self.code();self.assertEqual(self.client.post('/api/collection/unlock',json={'code':second['code']}).status_code,409)
  self.client.put(f'/api/admin/collections/{self.fid}',headers=self.admin,json={'responseCap':60});self.unlock(second)
  preview=self.client.get(f'/api/folders/{self.fid}/deletion-preview',headers=self.admin).json;self.assertEqual(preview['counts']['unverifiedResponses'],30)
  r=self.client.delete(f'/api/folders/{self.fid}',headers=self.admin,json={'confirmationName':preview['folder']['name'],'previewVersion':preview['previewVersion']});self.assertEqual(r.status_code,200,r.json)
  with db.connection_context():
   for model in [IntakeSubmission,CollectionCode,SubmissionSession,EditGrant,CollectionSettings]:self.assertEqual(model.select().count(),0)
if __name__=='__main__':unittest.main()
