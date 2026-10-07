"""Linux integration: actual Render command, long work, health and restart.

The temporary startup module substitutes a delayed synthetic solver only in
this test's child interpreters. No delay/test hook is present in production code.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen


@unittest.skipUnless(sys.platform.startswith('linux'), 'Gunicorn runs on Linux')
class GunicornGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(__file__).parent
        self.env = dict(os.environ, DATABASE_URL='', DATABASE_PATH=str(Path(self.tmp.name)/'jobs.db'),
                        ADMIN_PASSWORD='isolated-generation-test', REQUEST_RETENTION_ENABLED='false',
                        TEST_DELAY_SECONDS='65', PYTHONPATH=os.pathsep.join([self.tmp.name,str(self.root)]))
        seed = """
import json
from models import *
init_db()
folder=Folder.select().first()
employee=Employee.create(name='Synthetic',is_lead=True,max_hours=40)
FolderAvailability.create(folder=folder,employee=employee,data_json=json.dumps({f'{d}_{h:02d}':2 for d in ['Mon','Tue','Wed','Thu','Fri'] for h in range(7,10)}))
FolderConfig.update(data_json=json.dumps(dict(hourStart=7,hourEnd=9,lateHourStart=10,reqStaffOpen=1,reqStaffLate=0,minShiftLength=3,maxShiftLength=3,maxMorningShifts=5,maxEveningShifts=5,maxMorningPlusEvening=10,blockClopening=False,solverTimeLimit=90))).execute()
db.close()
"""
        subprocess.run([sys.executable,'-c',seed], cwd=self.root, env=self.env, check=True, timeout=20)
        Path(self.tmp.name,'sitecustomize.py').write_text("""
import os,time
import solver
original=solver.generate_schedule
def delayed(employees,availability,cfg,**kwargs):
    result=original(employees,availability,dict(cfg,solverTimeLimit=2),**kwargs)
    progress=kwargs.get('progress')
    if progress: progress(result)
    time.sleep(float(os.environ['TEST_DELAY_SECONDS']))
    return result
solver.generate_schedule=delayed
""")
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            self.port = listener.getsockname()[1]
        self.server = None
        self.log = open(Path(self.tmp.name,'server.log'),'w')
        self.start()
        self.token = self.api('/api/admin/login', {'password':self.env['ADMIN_PASSWORD']})['token']

    def start(self):
        self.server = subprocess.Popen([sys.executable,'-m','gunicorn','app:app','--workers','1','--timeout','120',
            '--bind',f'127.0.0.1:{self.port}'],cwd=self.root,env=self.env,stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                self.api('/healthz')
                return
            except Exception:
                time.sleep(.1)
        raise AssertionError('Gunicorn failed to start')

    def stop(self):
        if self.server and self.server.poll() is None:
            self.server.terminate()
            self.server.wait(timeout=15)

    def tearDown(self):
        self.stop()
        self.log.close()
        self.tmp.cleanup()

    def api(self, path, payload=None):
        headers={'Content-Type':'application/json'}
        if getattr(self,'token',None):
            headers['Authorization']='Bearer '+self.token
        data=json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(f'http://127.0.0.1:{self.port}'+path,data=data,headers=headers),timeout=5) as response:
            return json.load(response)

    def job(self, job_id):
        return self.api(f'/api/folders/1/generation-jobs/{job_id}')

    def test_over_sixty_seconds_keeps_health_and_other_requests_responsive(self):
        started=time.monotonic()
        job=self.api('/api/generate',dict(folderId=1,employeeIds=[1],requestId='a'*32))
        self.assertLess(time.monotonic()-started, 5)
        self.assertEqual(job['status'],'queued')
        while time.monotonic()-started < 100:
            before=time.monotonic()
            self.assertTrue(self.api('/healthz')['ok'])
            self.assertEqual(self.api('/api/folders')['folders'][0]['id'],1)
            self.assertLess(time.monotonic()-before,5)
            job=self.job(job['jobId'])
            if job['status'] not in ('queued','running'):
                break
            time.sleep(1)
        self.assertGreater(time.monotonic()-started,60)
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(len(self.api('/api/folders/1/schedules')),1)
        print('65-second generation completed while health and folder requests stayed responsive.')

    def test_restart_recovers_checkpoint_without_duplicate_save(self):
        self.env['TEST_DELAY_SECONDS']='8'
        # Restart to pass the shortened delay into the isolated child.
        self.stop()
        self.start()
        job=self.api('/api/generate',dict(folderId=1,employeeIds=[1]))
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            job=self.job(job['jobId'])
            if job['bestFairness'] is not None:
                break
            time.sleep(.2)
        self.assertIsNotNone(job['bestFairness'])
        self.stop()
        subprocess.run([sys.executable,'-c',
            "import datetime; from models import *; db.connect(); GenerationJob.update(lease_until=datetime.datetime.utcnow()-datetime.timedelta(seconds=1)).execute(); db.close()"],
            cwd=self.root, env=self.env, check=True,timeout=20)
        self.start()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            job=self.job(job['jobId'])
            if job['status'] not in ('queued','running'):
                break
            time.sleep(.2)
        self.assertEqual(job['status'],'completed',job)
        self.assertEqual(job['attempts'],2)
        self.assertEqual(len(self.api('/api/folders/1/schedules')),1)


if __name__=='__main__':
    unittest.main()
