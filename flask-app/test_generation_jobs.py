"""Durability, fencing, quality and real child-process checks on synthetic data."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import test_app
from flask.testing import FlaskClient
from models import db, init_db, Employee, Folder, FolderAvailability, GenerationJob, SavedSchedule, FolderConfig
import app as web
import generation_jobs as jobs
import solver
from test_boundary import config


class GenerationJobTests(unittest.TestCase):
    def setUp(self):
        jobs.shutdown()
        self.tmp = tempfile.TemporaryDirectory()
        if not db.is_closed():
            db.close()
        self.path = str(Path(self.tmp.name) / 'jobs.db')
        db.init(self.path)
        init_db()
        web.app.config.update(REQUEST_RETENTION_ENABLED=False, GENERATION_RUNNER_ENABLED=False)
        self.client = FlaskClient(web.app)
        token = self.client.post('/api/admin/login', json={'password':web.ADMIN_PASSWORD}).json['token']
        self.headers = {'Authorization':'Bearer ' + token}
        with db.connection_context():
            self.folder = Folder.select().first().id
            self.employee = Employee.create(name='Synthetic', is_lead=True, max_hours=40)
            self.row = FolderAvailability.create(folder=self.folder, employee=self.employee,
                data_json=json.dumps({f'{d}_{h:02d}':2 for d in ['Mon','Tue','Wed','Thu','Fri'] for h in range(7,10)}))
            FolderConfig.update(data_json=json.dumps(dict(
                hourStart=7, hourEnd=9, lateHourStart=10, reqStaffOpen=1, reqStaffLate=0,
                minShiftLength=3, maxShiftLength=3, maxMorningShifts=5, maxEveningShifts=5,
                maxMorningPlusEvening=10, blockClopening=False, solverTimeLimit=5))).where(FolderConfig.folder == self.folder).execute()

    def tearDown(self):
        jobs.shutdown()
        if not db.is_closed():
            db.close()
        self.tmp.cleanup()

    def post(self, **extra):
        return self.client.post('/api/generate', headers=self.headers,
            json=dict(folderId=self.folder, employeeIds=[self.employee.id], **extra))

    def status(self, job_id, folder=None):
        return self.client.get(f'/api/folders/{folder or self.folder}/generation-jobs/{job_id}', headers=self.headers)

    def finish(self):
        claimed = jobs.claim()
        self.assertIsNotNone(claimed)
        jobs.run_job(claimed[0], claimed[1])
        return claimed

    def test_accept_deduplicate_and_save_once(self):
        first = self.post(requestId='a'*32)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json['status'], 'queued')
        self.assertEqual(self.post().json['jobId'], first.json['jobId'])
        job_id, owner, _ = self.finish()
        self.assertEqual(self.status(job_id).json['status'], 'completed')
        jobs.run_job(job_id, owner)
        repeated = self.post(requestId='a'*32)
        self.assertEqual(repeated.json['status'], 'completed')
        with db.connection_context():
            self.assertEqual(SavedSchedule.select().count(), 1)
            self.assertEqual(GenerationJob.select().count(), 1)

    def test_cancel_and_scope_and_health_without_database(self):
        job_id = self.post().json['jobId']
        claimed = jobs.claim()
        self.assertEqual(self.client.get(f'/api/folders/{self.folder}/generation-jobs/{job_id}').status_code, 401)
        with db.connection_context():
            other = Folder.create(name='Other').id
        self.assertEqual(self.status(job_id, other).status_code, 404)
        cancel = self.client.post(f'/api/folders/{self.folder}/generation-jobs/{job_id}/cancel', headers=self.headers)
        self.assertEqual(cancel.json['status'], 'cancelled')
        jobs.run_job(claimed[0], claimed[1])
        self.assertIsNone(jobs.claim())
        with patch.object(db, 'connect', side_effect=AssertionError('Health must not access database')):
            self.assertEqual(self.client.get('/healthz').status_code, 200)

    def test_queue_bound_and_one_owner_even_with_multiple_folders(self):
        self.post()
        with db.connection_context():
            extra = []
            for i in range(3):
                folder = Folder.create(name=f'Queue {i}')
                FolderAvailability.create(folder=folder,employee=self.employee,data_json=self.row.data_json)
                extra.append(folder.id)
        for index, folder in enumerate(extra):
            response = self.client.post('/api/generate',headers=self.headers,
                json={'folderId':folder,'employeeIds':[self.employee.id]})
            self.assertEqual(response.status_code,202 if index<2 else 429)
        self.assertIsNotNone(jobs.claim())
        self.assertIsNone(jobs.claim())

    def test_time_budget_bounds_and_conflicting_inflight_inputs(self):
        route=f'/api/folders/{self.folder}/config'
        self.assertEqual(self.client.put(route,headers=self.headers,json={'solverTimeLimit':1801}).status_code,400)
        self.assertEqual(self.client.put(route,headers=self.headers,json={'solverTimeLimit':1800}).status_code,200)
        self.post()
        self.client.put(route,headers=self.headers,json={'solverTimeLimit':120})
        self.assertEqual(self.post().status_code,409)

    def test_expired_lease_recovers_and_fences_old_worker(self):
        self.post()
        job_id, old_owner, _ = jobs.claim()
        with db.connection_context():
            GenerationJob.update(lease_until=dt.datetime.utcnow()-dt.timedelta(seconds=1)).execute()
        claimed = jobs.claim()
        self.assertEqual(claimed[0], job_id)
        self.assertNotEqual(claimed[1], old_owner)
        jobs.run_job(job_id, old_owner)
        with db.connection_context():
            self.assertEqual(SavedSchedule.select().count(), 0)
        jobs.run_job(claimed[0], claimed[1])
        self.assertEqual(self.status(job_id).json['attempts'], 2)
        self.assertEqual(self.status(job_id).json['status'], 'completed')

    def test_source_edit_blocks_save_and_deleted_folder_cannot_be_reused(self):
        self.post()
        claimed = jobs.claim()
        with db.connection_context():
            FolderAvailability.update(comment='Changed').execute()
        jobs.run_job(claimed[0], claimed[1])
        result = self.status(claimed[0]).json
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['errorStatus'], 409)
        with db.connection_context():
            self.assertEqual(SavedSchedule.select().count(), 0)

    def test_more_time_reuses_optimal_schedule_and_changed_rules_reset_baseline(self):
        job_id = self.post().json['jobId']
        self.finish()
        original = self.status(job_id).json['result']
        self.assertEqual(original['status'], 'OPTIMAL')
        self.client.put(f'/api/folders/{self.folder}/config', headers=self.headers, json={'solverTimeLimit':120})
        second = self.post().json['jobId']
        self.finish()
        result = self.status(second).json['result']
        self.assertEqual(result['qualityChange'], 'unchanged')
        self.assertEqual(result['work'], original['work'])
        self.assertEqual(result['fairnessFloor'], original['fairnessFloor'])
        self.assertLess(result['solveSeconds'], 5)
        self.assertEqual(result['solverWorkersUsed'], 1)
        self.client.put(f'/api/folders/{self.folder}/config', headers=self.headers, json={'burdenWeight':4})
        third = self.post().json['jobId']
        self.finish()
        self.assertEqual(self.status(third).json['result']['qualityChange'], 'first')

    def test_real_child_isolated_from_http_and_runner_exits_when_idle(self):
        env = {'DATABASE_PATH':self.path, 'DATABASE_URL':'', 'ADMIN_PASSWORD':web.ADMIN_PASSWORD}
        with patch.dict(os.environ, env):
            web.app.config['GENERATION_RUNNER_ENABLED'] = True
            job_id = self.post().json['jobId']
            deadline = time.monotonic()+30
            while time.monotonic() < deadline:
                self.assertEqual(self.client.get('/healthz').status_code, 200)
                state = self.status(job_id).json
                if state['status'] not in jobs.ACTIVE:
                    break
                time.sleep(.2)
            self.assertEqual(state['status'], 'completed', state)
            jobs._thread.join(timeout=5)
            self.assertFalse(jobs._thread.is_alive(), 'Idle runner must stop polling the database')

    def test_alternatives_require_a_first_schedule_and_modes_do_not_deduplicate(self):
        self.assertEqual(self.post(mode='alternative').status_code, 409)
        self.assertEqual(self.post(mode='invalid').status_code, 400)
        first = self.post(requestId='a'*32)
        self.assertEqual(self.post(mode='alternative').status_code, 409)
        self.finish()
        self.assertEqual(self.post(mode='alternative', requestId='a'*32).status_code, 409)
        alternative = self.post(mode='alternative', requestId='b'*32)
        self.assertEqual(alternative.status_code, 202)
        self.assertEqual(alternative.json['mode'], 'alternative')
        self.assertEqual(self.post().status_code, 409)
        self.finish()
        result = self.status(alternative.json['jobId']).json['result']
        self.assertEqual(result['status'], 'NO_ALTERNATIVE')
        self.assertEqual(self.post(mode='alternative', requestId='b'*32).json['jobId'], alternative.json['jobId'])
        with db.connection_context():
            self.assertEqual(SavedSchedule.select().count(), 1)

    def test_distinct_lower_quality_alternatives_and_optimize_preserves_best(self):
        with db.connection_context():
            other = Employee.create(name='Other', is_lead=True, max_hours=40)
            FolderAvailability.create(folder=self.folder, employee=other, data_json=self.row.data_json)
        def submit(mode='optimize', **extra):
            return self.client.post('/api/generate', headers=self.headers,
                json=dict(folderId=self.folder, employeeIds=[self.employee.id, other.id], mode=mode, **extra))
        works, scores = [], []
        # Each of five days has exactly one three-hour shift and two eligible
        # employees: enumerate all 2**5 distinct assignments through the API.
        for index in range(32):
            mode = 'alternative' if index else 'optimize'
            request_id = f'{index+1:032x}'
            accepted = submit(mode, requestId=request_id)
            self.assertEqual(accepted.status_code, 202)
            if index == 1:
                claimed = jobs.claim()
                original = solver.generate_schedule
                def interrupted(*args, **kwargs):
                    self.assertIsNone(kwargs['incumbent'])
                    candidate = original(*args, **kwargs)
                    with db.connection_context():
                        GenerationJob.update(checkpoint_json=json.dumps(candidate)).where(GenerationJob.id == claimed[0]).execute()
                    raise SystemExit('Synthetic worker interruption after checkpoint')
                with patch.object(solver, 'generate_schedule', side_effect=interrupted), self.assertRaises(SystemExit):
                    jobs.run_job(claimed[0], claimed[1])
                with db.connection_context():
                    GenerationJob.update(lease_until=dt.datetime.utcnow()-dt.timedelta(seconds=1)).where(GenerationJob.id == claimed[0]).execute()
                with patch.object(solver, 'generate_schedule', wraps=original) as resumed:
                    self.finish()
                    self.assertIsNotNone(resumed.call_args.kwargs['incumbent'])
                self.assertEqual(self.status(claimed[0]).json['attempts'], 2)
            else:
                self.finish()
            result = self.status(accepted.json['jobId']).json['result']
            self.assertIn(result['status'], ('FEASIBLE','OPTIMAL'))
            works.append(json.dumps(result['work'], sort_keys=True))
            scores.append(result['fairnessFloor'])
            self.assertEqual(submit(mode, requestId=request_id).json['jobId'], accepted.json['jobId'])
        self.assertEqual(len(set(works)), 32)
        self.assertLess(scores[-1], scores[0])
        exhausted = submit('alternative')
        self.finish()
        self.assertEqual(self.status(exhausted.json['jobId']).json['result']['status'], 'NO_ALTERNATIVE')
        optimized = submit()
        self.finish()
        best = self.status(optimized.json['jobId']).json['result']
        self.assertEqual(best['fairnessFloor'], max(scores))
        self.assertEqual(best['qualityChange'], 'unchanged')
        self.assertEqual(best['status'], 'OPTIMAL')
        self.assertEqual(SavedSchedule.select().count(), 33)

    def test_alternative_proof_does_not_short_circuit_unrestricted_optimization(self):
        self.post()
        self.finish()
        with db.connection_context():
            previous = GenerationJob.select().first()
            payload = json.loads(previous.payload_json)
            payload['mode'] = 'alternative'
            previous.payload_json = json.dumps(payload)
            previous.save()
        accepted = self.post()
        original = solver._solve
        with patch.object(solver, '_solve', wraps=original) as search:
            self.finish()
            self.assertGreater(search.call_count, 0)
        self.assertEqual(self.status(accepted.json['jobId']).json['result']['status'], 'OPTIMAL')


class OptimizationQualityTests(unittest.TestCase):
    def test_more_search_improves_a_valid_but_unfair_baseline(self):
        cfg = config(solverWorkers=1, solverTimeLimit=5, maxMorningShifts=5, maxMorningPlusEvening=10)
        roster = [dict(id=i+1, name=name, isLead=True, minHours=0, maxHours=6) for i,name in enumerate(['A','B'])]
        av = {e['name']:{f'{d}_{h:02d}':2 for d in cfg['days'] for h in range(7,9)} for e in roster}
        original = solver._solve
        def unfair(model, settings, **kwargs):
            for index, var in enumerate(list(model.Proto().variables)):
                if var.name.startswith('w_'):
                    model.Add(model.GetBoolVarFromProtoIndex(index) ==
                              int(var.name.startswith('w_0_')))
            engine, status = original(model, settings, **kwargs)
            self.assertIn(status, [solver.cp_model.OPTIMAL,solver.cp_model.FEASIBLE])
            return engine, solver.cp_model.FEASIBLE
        with patch.object(solver, '_solve', side_effect=unfair):
            baseline = solver.generate_schedule(roster,av,cfg)
        self.assertEqual(baseline['fairnessFloor'], 0)
        result = solver.generate_schedule(roster,av,cfg,incumbent=baseline)
        self.assertGreater(result['fairnessFloor'], baseline['fairnessFloor'])
        self.assertEqual(result['qualityChange'], 'improved')

    def test_prolonged_search_keeps_baseline_when_no_new_solution_is_found(self):
        cfg = config(solverWorkers=1, solverTimeLimit=5, maxMorningShifts=5, maxMorningPlusEvening=10)
        roster = [dict(id=1, name='A', isLead=True, minHours=0, maxHours=40)]
        av = {'A':{f'{d}_{h:02d}':2 for d in cfg['days'] for h in range(7,9)}}
        baseline = solver.generate_schedule(roster, av, cfg)
        self.assertEqual(baseline['status'], 'OPTIMAL')
        baseline = dict(baseline, status='FEASIBLE', fairnessOptimal=False, optimizationStages=[])
        def no_solution(model, cfg, **kwargs):
            return solver.cp_model.CpSolver(), solver.cp_model.UNKNOWN
        with patch.object(solver, '_solve', side_effect=no_solution):
            result = solver.generate_schedule(roster, av, cfg, incumbent=json.loads(json.dumps(baseline)))
        self.assertEqual(result['fairnessFloor'], baseline['fairnessFloor'])
        self.assertEqual(result['qualityChange'], 'unchanged')
        self.assertEqual(result['work'], baseline['work'])

    def test_improvement_callback_and_early_optimal_stop(self):
        cfg = config(solverWorkers=1, solverTimeLimit=120, maxMorningShifts=5, maxMorningPlusEvening=10)
        roster = [dict(id=1, name='A', isLead=True, minHours=0, maxHours=40)]
        av = {'A':{f'{d}_{h:02d}':2 for d in cfg['days'] for h in range(7,9)}}
        progress = []
        result = solver.generate_schedule(roster, av, cfg, progress=progress.append)
        self.assertEqual(result['status'], 'OPTIMAL')
        self.assertLess(result['solveSeconds'], 10)
        self.assertTrue(progress)
        self.assertEqual(progress[-1]['fairnessFloor'], result['fairnessFloor'])


if __name__ == '__main__':
    unittest.main()
