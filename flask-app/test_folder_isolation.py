"""Folder isolation regressions against disposable databases, never production."""
import json
import os
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet

os.environ.pop('DATABASE_URL', None)
os.environ.setdefault('DATABASE_PATH', str(Path(tempfile.gettempdir()) / ('scope-' + uuid.uuid4().hex + '.db')))
os.environ.setdefault('ADMIN_PASSWORD', 'scope-test')
os.environ.setdefault('COLLECTION_ENCRYPTION_KEY', Fernet.generate_key().decode())
os.environ.setdefault('COLLECTION_VERIFIER_KEY', secrets.token_hex(32))
import app as web
from flask.testing import FlaskClient
from models import (db, init_db, Employee, Folder, FolderEmployee, FolderConfig,
                    FolderAvailability, Config, SchemaMigration, SavedSchedule, get_config, enroll_employee)


class FolderIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        if not db.is_closed(): db.close()
        db.init(str(Path(self.tmp.name) / 'test.db'))
        init_db()
        web.app.config['REQUEST_RETENTION_ENABLED'] = False
        self.client = FlaskClient(web.app)
        token = self.client.post('/api/admin/login', json={'password': web.ADMIN_PASSWORD}).json['token']
        self.headers = {'Authorization': 'Bearer ' + token}
        self.a = self.client.get('/api/folders', headers=self.headers).json['folders'][0]['id']
        self.b = self.client.post('/api/folders', headers=self.headers, json={'name': 'Winter'}).json['id']

    def tearDown(self):
        if not db.is_closed(): db.close()
        self.tmp.cleanup()

    def get(self, folder, route):
        return self.client.get(f'/api/folders/{folder}/{route}', headers=self.headers)

    def put(self, folder, route, data):
        return self.client.put(f'/api/folders/{folder}/{route}', headers=self.headers, json=data)

    def add(self, folder, name):
        result = self.client.post(f'/api/folders/{folder}/employees', headers=self.headers, json={'name':name})
        self.assertEqual(result.status_code, 201, result.json)
        return result.json

    def availability(self, folder, employee):
        with db.connection_context():
            FolderAvailability.create(folder=folder, employee=employee, data_json=json.dumps({'Mon_07':2,'Sat_07':1}))

    def test_roster_and_staffing_only_include_selected_folder(self):
        alex = self.add(self.a, 'Alex'); blair = self.add(self.b, 'Blair')
        self.assertEqual([e['id'] for e in self.get(self.a,'employees').json],[alex['id']])
        self.assertEqual([e['id'] for e in self.get(self.b,'staffing-plan').json['employees']],[blair['id']])
        overview = self.client.get(f'/api/availability?folderId={self.a}',headers=self.headers).json
        self.assertEqual(overview['missing'],['Alex'])
        self.assertEqual(overview['availability'],{})
        intake = self.client.get(f'/api/admin/intake?folderId={self.a}',headers=self.headers).json
        self.assertEqual(intake['employees'],[{'id':alex['id'],'name':'Alex'}])

    def test_settings_and_shared_employee_attributes_are_independent(self):
        alex = self.add(self.a,'Alex')
        with db.connection_context(): enroll_employee(Employee.get_by_id(alex['id']),self.b)
        before = self.get(self.b,'config').json
        self.assertEqual(self.put(self.a,'config',{'reqStaffOpen':3,'wFairness':123}).status_code,200)
        self.assertEqual(self.get(self.b,'config').json,before)
        self.assertEqual(self.put(self.a,f'employees/{alex["id"]}',{'isLead':True,'minHours':12,'maxHours':20}).status_code,200)
        self.assertEqual(self.get(self.b,'employees').json[0]['minHours'],0)
        self.assertFalse(self.get(self.b,'employees').json[0]['isLead'])
        self.assertEqual(self.get(self.a,'staffing-plan').json['allottedHours'],12)
        self.assertEqual(self.get(self.b,'staffing-plan').json['allottedHours'],0)
        c = self.client.post('/api/folders',headers=self.headers,json={'name':'New'}).json['id']
        self.assertEqual(self.get(c,'employees').json,[])
        self.assertEqual(self.get(c,'config').json['wFairness'],before['wFairness'])

    def test_missing_scope_and_foreign_employee_ids_are_rejected(self):
        alex=self.add(self.a,'Alex')
        for route in ['/api/config','/api/employees','/api/staffing-plan','/api/roster']:
            self.assertEqual(self.client.get(route,headers=self.headers).status_code,400)
        self.assertEqual(self.client.put('/api/config',headers=self.headers,json={'wFairness':1}).status_code,400)
        self.assertEqual(self.put(self.b,f'employees/{alex["id"]}',{'minHours':1}).status_code,404)
        self.assertEqual(self.client.delete(f'/api/folders/{self.b}/employees/{alex["id"]}',headers=self.headers).status_code,404)
        self.assertEqual(self.get(99999,'config').status_code,404)

    def test_removal_preserves_history_identity_and_other_folder(self):
        alex=self.add(self.a,'Alex')
        with db.connection_context(): enroll_employee(Employee.get_by_id(alex['id']),self.b)
        self.availability(self.a,alex['id']); self.availability(self.b,alex['id'])
        self.assertEqual(self.client.delete(f'/api/folders/{self.a}/employees/{alex["id"]}',headers=self.headers).status_code,200)
        self.assertEqual(self.get(self.a,'employees').json,[])
        self.assertEqual(len(self.get(self.b,'employees').json),1)
        overview=self.client.get(f'/api/availability?folderId={self.a}',headers=self.headers).json
        self.assertEqual(overview['availability'],{})
        with db.connection_context():
            self.assertEqual(FolderAvailability.select().count(),2)
            self.assertTrue(Employee.get_by_id(alex['id']))
        init_db()
        self.assertEqual(self.get(self.a,'employees').json,[])

    def test_same_names_do_not_silently_link_different_folders(self):
        one=self.add(self.a,'Alex'); two=self.add(self.b,'Alex')
        self.assertNotEqual(one['id'],two['id'])
        self.assertEqual(len(self.get(self.a,'employees').json),1)
        self.assertEqual(len(self.get(self.b,'employees').json),1)

    def test_migration_is_repeatable_preserves_unassigned_and_snapshots(self):
        with db.connection_context():
            SchemaMigration.delete().where(SchemaMigration.name=='folder-isolation-v1').execute()
            FolderEmployee.delete().execute(); FolderConfig.delete().execute()
            Config.update(data_json=json.dumps({'wFairness':123})).execute()
            employee=Employee.create(name='Legacy',is_lead=True,min_hours=9,max_hours=20)
            unassigned=Employee.create(name='Unassigned',min_hours=7)
            FolderAvailability.create(folder=self.a,employee=employee,data_json='{"Mon_07":1}')
            FolderAvailability.create(folder=self.b,employee=employee,data_json='{"Tue_07":1}')
            SavedSchedule.create(folder=self.a,snapshot_json='{"original":true}')
        init_db(); init_db()
        self.assertEqual(self.get(self.a,'config').json['wFairness'],123)
        self.assertEqual(self.get(self.b,'employees').json[0]['minHours'],9)
        with db.connection_context():
            self.assertEqual(FolderEmployee.select().count(),2)
            self.assertEqual(json.loads(SavedSchedule.get().snapshot_json),{'original':True})
        self.assertEqual(self.client.get('/api/employees/unassigned',headers=self.headers).json,[{'id':unassigned.id,'name':'Unassigned'}])
        self.assertEqual(self.client.post(f'/api/folders/{self.a}/employees',headers=self.headers,json={'employeeId':unassigned.id}).status_code,201)
        self.assertEqual(self.get(self.a,'staffing-plan').json['allottedHours'],16)
        self.assertEqual(self.get(self.b,'staffing-plan').json['allottedHours'],9)
        self.put(self.a,'config',{'wFairness':456}); init_db()
        self.assertEqual(self.get(self.a,'config').json['wFairness'],456)
        self.assertEqual(self.get(self.b,'config').json['wFairness'],123)

    def test_generation_and_diagnostics_use_folder_settings_and_roster(self):
        alex=self.add(self.a,'Alex'); blair=self.add(self.b,'Blair')
        self.availability(self.a,alex['id']); self.availability(self.b,blair['id'])
        self.put(self.a,'config',{'wFairness':123}); self.put(self.b,'config',{'wFairness':456})
        diagnostic=self.client.get(f'/api/diagnostics?folderId={self.a}',headers=self.headers)
        self.assertEqual(diagnostic.status_code,200)
        self.assertEqual(diagnostic.json['config']['wFairness'],123)
        captured=[]
        def solve(roster,availability,cfg,seed=None):
            captured.append((roster,availability,cfg)); return {'status':'IMPOSSIBLE'}
        with patch.object(web.solver_module,'generate_schedule',solve):
            response=self.client.post('/api/generate',headers=self.headers,json={'folderId':self.a,'employeeIds':[alex['id']]})
        self.assertEqual(response.status_code,200)
        self.assertEqual([e['id'] for e in captured[0][0]],[alex['id']])
        self.assertEqual(captured[0][2]['wFairness'],123)
        self.assertEqual(self.client.post('/api/generate',headers=self.headers,json={'folderId':self.a,'employeeIds':[blair['id']]}).status_code,404)
        weekend={'start_date':'2026-10-09','end_date':'2026-10-11','fixed_assignments':{},'rotating_employees':[blair['id']]}
        self.assertEqual(self.client.post('/api/generate_weekend',headers=self.headers,json={'folderId':self.a,'config':weekend}).status_code,400)

    def test_code_authorized_context_and_submission_use_codes_folder(self):
        self.put(self.a,'config',{'hourStart':8,'hourEnd':20}); self.put(self.b,'config',{'hourStart':9,'hourEnd':19})
        code=self.client.post('/api/admin/codes',headers=self.headers,json={'folderId':self.b}).json
        unlocked=self.client.post('/api/collection/unlock',json={'code':code['code']}).json
        self.assertEqual(unlocked['folder']['id'],self.b)
        self.assertEqual(unlocked['config']['hourStart'],9)
        response=self.client.post('/api/availability',headers={'X-Submission-CSRF':unlocked['csrf']},json={
            'name':'Code person','folderId':self.b,'revision':unlocked['revision'],'requestId':uuid.uuid4().hex,'availability':{'Mon_09':1},'comment':''})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.get(self.a,'employees').json,[])
        self.assertEqual(len(self.get(self.b,'employees').json),1)

    def test_folder_deletion_removes_only_its_memberships_and_settings(self):
        alex=self.add(self.a,'Alex')
        with db.connection_context(): enroll_employee(Employee.get_by_id(alex['id']),self.b)
        before=self.get(self.b,'config').json
        preview=self.get(self.a,'deletion-preview').json
        self.assertEqual(preview['counts']['folderEmployees'],1)
        response=self.client.delete(f'/api/folders/{self.a}',headers=self.headers,json={'confirmationName':preview['folder']['name'],'previewVersion':preview['previewVersion']})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.get(self.b,'config').json,before)
        self.assertEqual(len(self.get(self.b,'employees').json),1)
        with db.connection_context(): self.assertIsNone(FolderConfig.get_or_none(FolderConfig.folder==self.a))


if __name__=='__main__': unittest.main()
