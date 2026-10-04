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
from flask.testing import FlaskClient
from models import *

class CollectionTests(unittest.TestCase):
 def setUp(self):
  web.app.config['REQUEST_RETENTION_ENABLED']=False
  self.tmp=tempfile.TemporaryDirectory();db.close()
  if test_pg:
   with db.connection_context():db.drop_tables([RateBucket,SubmissionSession,EditGrant,IntakeSubmission,CollectionCode,CollectionSettings,AdminSession,SavedWeekendSchedule,SavedSchedule,FolderAvailability,SubmissionState,Folder,Availability,Employee,Config],cascade=True)
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
 def submit(self,h,c=None,**kw):
  data=dict(name='Alex',folderId=self.fid,revision=self.ctx['revision'],availability={'Mon_07':2},comment='Synthetic',requestId=uuid.uuid4().hex);data.update(kw)
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
  two=self.code(folderId=folder['id']);self.assertEqual(two['status'],'Collection closed')
  other=self.client.get(f'/api/admin/codes?folderId={folder["id"]}',headers=self.admin).json
  self.assertEqual([c['id'] for c in other['codes']],[two['id']]);self.assertEqual(other['collection']['responseCap'],100)
  self.assertEqual(self.client.get('/api/submission-context').json['folder']['id'],self.fid)
  self.assertEqual(self.submit(h,folderId=folder['id']).status_code,409)
  self.assertEqual(self.submit(h).status_code,200)
  self.client.patch(f'/api/folders/{folder["id"]}',headers=self.admin,json={'activate':True})
  self.assertEqual(self.submit(h).status_code,409)
  second_context=self.client.post('/api/collection/unlock',json={'code':two['code']}).json
  self.assertEqual(second_context['folder']['id'],folder['id']);self.assertEqual(second_context['responseLimit'],30)
  self.assertEqual(self.submit({'X-Submission-CSRF':second_context['csrf']},folderId=folder['id'],revision=second_context['revision']).status_code,200)
  with db.connection_context():
   self.assertEqual([(r.folder_id,r.code_id) for r in IntakeSubmission.select().order_by(IntakeSubmission.id)],[(self.fid,one['id']),(folder['id'],two['id'])])
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
  self.assertEqual(self.add_responses(code,2).json['status'],'Collection closed');self.assertEqual(self.submit(h).status_code,409)
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
   c=FlaskClient(web.app);c.set_cookie('availability_session',cookie,path='/api/');return self.submit(h,c,name=f'Added {i}').status_code
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
  with db.connection_context():self.assertEqual(Employee.select().count(),0);self.assertEqual(FolderAvailability.select().count(),0);self.assertEqual(IntakeSubmission.select().count(),1)
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
 def test_parallel_limit(self):
  code=self.code();h=self.unlock(code);cookie=self.client.get_cookie('availability_session',path='/api/').value
  def send(i):
   c=FlaskClient(web.app);c.set_cookie('availability_session',cookie,path='/api/');return self.submit(h,c,name=f'Synthetic {i}').status_code
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:statuses=list(pool.map(send,range(45)))
  self.assertEqual(statuses.count(200),30,statuses);self.assertEqual(statuses.count(409),15,statuses)
  with db.connection_context():self.assertEqual(CollectionCode.get_by_id(code['id']).received,30);self.assertEqual(IntakeSubmission.select().count(),30)
 def test_revoke_and_delete(self):
  one=self.code();two=self.code();h=self.unlock(one);self.submit(h)
  self.client.patch(f'/api/admin/codes/{one["id"]}',headers=self.admin,json={});self.assertEqual(self.submit(h).status_code,409)
  h=self.unlock(two);self.submit(h);self.client.delete(f'/api/admin/codes/{two["id"]}',headers=self.admin);self.assertEqual(self.submit(h).status_code,409)
  normal=self.client.get('/api/admin/codes?folderId=1',headers=self.admin).json['codes'];allrows=self.client.get('/api/admin/codes?folderId=1&showDeleted=1',headers=self.admin).json['codes'];self.assertEqual(len(normal),1);self.assertEqual(len(allrows),2)
  with db.connection_context():self.assertEqual(IntakeSubmission.select().count(),2)
 def test_review_and_conflicts(self):
  with db.connection_context():eid=Employee.create(name='Alex').id
  h=self.unlock(self.code());self.submit(h,comment='First');first=self.intake()['submissions'][0];self.assertEqual(self.accept(first,eid).status_code,200)
  self.submit(h,comment='Second');data=self.intake();second=data['submissions'][0]
  with db.connection_context():self.assertEqual(FolderAvailability.get().comment,'First')
  self.assertEqual(self.accept(second,eid).status_code,409);version=data['acceptedVersions'][str(eid)];self.assertEqual(self.accept(second,eid,version).status_code,200);self.assertEqual(self.accept(second,eid,version).status_code,409)
  with db.connection_context():self.assertEqual(FolderAvailability.get().comment,'Second');self.assertEqual(IntakeSubmission.get_by_id(first['id']).comment,'First');self.assertEqual(IntakeSubmission.get_by_id(first['id']).status,'Superseded')
  self.submit(h,comment='Third');third=self.intake()['submissions'][0];version=self.intake()['acceptedVersions'][str(eid)]
  self.client.put('/api/admin/availability',headers=self.admin,json=dict(employeeId=eid,folderId=self.fid,availability={},comment='Supervisor'));self.assertEqual(self.accept(third,eid,version).status_code,409)
 def test_edit_scope_expiry_and_revocation(self):
  code=self.code();h=self.unlock(code);self.submit(h,comment='Original');row=self.intake()['submissions'][0]
  link=self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={}).json;c=FlaskClient(web.app)
  opened=c.post('/api/collection/unlock',json={'editToken':link['token']});self.assertEqual(opened.status_code,200);self.assertEqual(opened.json['edit']['comment'],'Original');eh={'X-Submission-CSRF':opened.json['csrf']}
  self.assertEqual(self.submit(eh,c,name='Someone else').status_code,400);self.assertEqual(self.submit(eh,c,comment='Correction').status_code,200)
  with db.connection_context():self.assertEqual(IntakeSubmission.select().count(),2);self.assertEqual(CollectionCode.get_by_id(code['id']).received,2);self.assertEqual(IntakeSubmission.get_by_id(row['id']).comment,'Original')
  self.assertEqual(c.post('/api/collection/unlock',json={'editToken':link['token']}).status_code,401)
  self.client.delete(f'/api/admin/edit-links/{link["grantId"]}',headers=self.admin);self.assertEqual(self.submit(eh,c).status_code,401)
  link=self.client.post(f'/api/admin/intake/{row["id"]}/edit-links',headers=self.admin,json={}).json
  with db.connection_context():EditGrant.update(expires_at=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
  self.assertEqual(c.post('/api/collection/unlock',json={'editToken':link['token']}).status_code,401)
 def test_close_reopen_and_expiry(self):
  code=self.code();h=self.unlock(code);self.client.patch(f'/api/folders/{self.fid}',headers=self.admin,json={'activate':False});self.assertEqual(self.submit(h).status_code,409)
  self.client.patch(f'/api/folders/{self.fid}',headers=self.admin,json={'activate':True});self.assertEqual(self.submit(h).status_code,409)
  self.ctx=self.client.get('/api/submission-context').json;h=self.unlock(code);self.assertEqual(self.submit(h).status_code,200)
  with db.connection_context():CollectionCode.update(expires_at=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
  self.assertEqual(self.submit(h).status_code,409)
 def test_encrypted_storage_and_bounded_rate_limits(self):
  code=self.code()
  with db.connection_context():row=CollectionCode.get_by_id(code['id']);self.assertNotIn(code['code'],row.encrypted_code);self.assertNotEqual(row.verifier,code['code'])
  statuses=[self.client.post('/api/collection/unlock',json={'code':'invalid'},headers={'X-Forwarded-For':f'198.51.100.{i}'}).status_code for i in range(160)];self.assertIn(429,statuses)
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
