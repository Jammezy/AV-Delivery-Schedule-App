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
  r=self.client.post('/api/admin/codes',headers=self.admin,json=dict(folderId=self.fid,**kw));self.assertEqual(r.status_code,201,r.json);return r.json
 def unlock(self,code,c=None):
  r=(c or self.client).post('/api/collection/unlock',json={'code':code['code']});self.assertEqual(r.status_code,200,r.json);return {'X-Submission-CSRF':r.json['csrf']}
 def submit(self,h,c=None,**kw):
  data=dict(name='Alex',folderId=self.fid,revision=self.ctx['revision'],availability={'Mon_07':2},comment='Synthetic',requestId=uuid.uuid4().hex);data.update(kw)
  return (c or self.client).post('/api/availability',headers=h,json=data)
 def intake(self):return self.client.get('/api/admin/intake?folderId='+str(self.fid),headers=self.admin).json
 def accept(self,row,eid,version=None):return self.client.post(f'/api/admin/intake/{row["id"]}/review',headers=self.admin,json=dict(action='accept',employeeId=eid,reviewVersion=row['reviewVersion'],acceptedVersion=version))
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
