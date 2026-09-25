"""Integration test against a disposable local PostgreSQL database only.

Run with TEST_POSTGRES_PORT pointing at a dedicated temporary local server.
Never reads the production DATABASE_URL.
"""
import concurrent.futures
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql

port = int(os.environ["TEST_POSTGRES_PORT"])
database = "recovery_test_" + uuid.uuid4().hex
admin = psycopg2.connect(host="127.0.0.1", port=port, user="recovery_test", dbname="postgres")
admin.autocommit = True
with admin.cursor() as cursor:
    cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
admin.close()
os.environ["DATABASE_URL"] = f"postgresql://recovery_test@127.0.0.1:{port}/{database}"
os.environ["ADMIN_PASSWORD"] = "test-admin"
with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cursor:
        cursor.execute("""CREATE TABLE employee (id SERIAL PRIMARY KEY, name VARCHAR(255) UNIQUE NOT NULL,
            is_lead BOOLEAN NOT NULL, min_hours INTEGER NOT NULL, max_hours INTEGER NOT NULL);
            CREATE TABLE availability (id SERIAL PRIMARY KEY, employee_name VARCHAR(255) UNIQUE NOT NULL,
            data_json TEXT NOT NULL, submitted_at TIMESTAMP NOT NULL);
            CREATE TABLE config (id SERIAL PRIMARY KEY, data_json TEXT NOT NULL);
            INSERT INTO employee (name,is_lead,min_hours,max_hours) VALUES ('Legacy',true,0,40);
            INSERT INTO availability (employee_name,data_json,submitted_at)
              VALUES ('Legacy','{"Mon_07":2,"Tue_07":true}', '2026-01-01');
            INSERT INTO config (data_json) VALUES ('{"wFairness":123}');""")

def initialize_worker(_):
    result = subprocess.run([sys.executable,"-c","import app; print('initialized')"],
        cwd=Path(__file__).parent, capture_output=True,text=True,env=os.environ)
    assert result.returncode == 0, result.stderr

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
    list(workers.map(initialize_worker, range(2)))

import app as web
from models import db, Folder, FolderAvailability, SavedSchedule, get_config
client = web.app.test_client()
token = client.post('/api/admin/login',json={'password':'test-admin'}).json['token']
headers = {'Authorization':'Bearer '+token}
context = client.get('/api/submission-context').json
spring = context['folder']['id']
rows = client.get(f'/api/availability?folderId={spring}',headers=headers).json
assert len(rows['submissions']) == 1
assert rows['availability']['Legacy'] == {'Mon_07':2,'Tue_07':1}
assert get_config()['wFairness'] == 123

def submit(name, ctx=context):
    with web.app.test_client() as c:
        r = c.post('/api/availability',json={'name':name,'folderId':ctx['folder']['id'],
            'revision':ctx['revision'],'availability':{'Mon_07':2,'Sat_07':1},'comment':'Persistent'})
        assert r.status_code == 200, r.json

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
    list(workers.map(submit,['Alex','Blair','Casey']))
summer = client.post('/api/folders',headers=headers,json={'name':'Summer','activate':True}).json
stale = client.post('/api/availability',json={'name':'Alex','folderId':spring,'revision':context['revision'],'availability':{}})
assert stale.status_code == 409
summer_context = client.get('/api/submission-context').json
submit('Alex',summer_context)
data = client.get(f'/api/availability?folderId={summer["id"]}',headers=headers).json
chosen = data['submissions'][0]['employeeId']
from unittest.mock import patch
with patch.object(web.solver_module,'generate_schedule',return_value={'status':'FEASIBLE','schedule':{},'work':{}}):
    result = client.post('/api/generate',headers=headers,json={'folderId':summer['id'],'employeeIds':[chosen]})
    assert result.status_code == 200, result.json
    schedule_id = result.json['savedScheduleId']
snapshot = client.get(f'/api/folders/{summer["id"]}/schedules/{schedule_id}',headers=headers).json
assert snapshot['submissions'][0]['comment'] == 'Persistent'
client.post('/api/admin/logout',headers=headers)
assert client.get('/api/folders',headers=headers).status_code == 401
db.connect(reuse_if_open=True)
FolderAvailability.update(submitted_at=datetime.datetime(2026,1,1)).execute()
db.close()
code = "from app import app; c=app.test_client(); x=c.get('/api/submission-context').json; r=c.get('/api/availability/Alex?folderId='+str(x['folder']['id'])); assert r.json['comment']=='Persistent'; assert r.json['availability']['Mon_07']==2"
result = subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).parent,env=os.environ,capture_output=True,text=True)
assert result.returncode == 0, result.stderr
print('PASS PostgreSQL: concurrent startup/migration, legacy preferences/settings, concurrent submissions, folder isolation, stale form rejection, saved snapshot, logout, and fresh-process persistence.')
