"""Boundary consent regression tests. All API tests use disposable SQLite."""
import itertools
import json
import unittest
from unittest.mock import patch

import test_app
from test_app import web, db, Employee, FolderAvailability, Config, init_db
from boundary import boundary_context, consent_status, legacy_consent_token
import solver
from models import DEFAULT_CONFIG
from ortools.sat.python import cp_model


def config(**changes):
    cfg = dict(DEFAULT_CONFIG, days=['Mon', 'Tue', 'Wed'], hourStart=7, hourEnd=8,
               lateHourStart=9, reqStaffOpen=1, reqStaffLate=0,
               minShiftLength=2, maxShiftLength=2, requireLeadDuringOpen=False,
               requireLeadDuringLate=False, blockClopening=False,
               solverWorkers=1, solverTimeLimit=5)
    cfg.update(changes)
    return cfg


def grant(cfg, employee_id=1, opening=True, closing=False):
    cfg.setdefault('boundaryConsents', {})[str(employee_id)] = {
        'allowExtraOpenings': opening, 'allowExtraClosings': closing,
        'consentContext': boundary_context(cfg)['token']}


class BoundarySolverTests(unittest.TestCase):
    def test_persistent_permission_requalifies_and_maximum_remains_hard(self):
        cfg = config(days=['Mon'], hourEnd=10, lateHourStart=11, minShiftLength=3, maxShiftLength=4,
                     allowPreferredBoundaryExtras=True, maxMorningShifts=0, maxMorningPlusEvening=0)
        grid = {'Mon_07':2, 'Mon_08':2, 'Mon_09':2, 'Mon_10':1}
        grant(cfg)
        record = dict(cfg['boundaryConsents']['1'])
        employee = dict(id=1, name='A', minHours=0, maxHours=4)
        for minimum, candidates in [(3,['Mon']), (2,['Mon']), (4,[]), (2,['Mon'])]:
            cfg['minShiftLength'] = minimum
            status = consent_status(grid, record, cfg)
            self.assertTrue(status['allowExtraOpenings'])
            self.assertFalse(status['reconfirmationNeeded'])
            self.assertEqual(status['candidates']['openings'], candidates)
            self.assertEqual(status['effectiveOpenings'], bool(candidates))
            result = solver.generate_schedule([employee], {'A':grid}, cfg)
            self.assertEqual(result['status'] in ('OPTIMAL','FEASIBLE'), bool(candidates))
        cfg['maxShiftLength'] = 2
        self.assertEqual(boundary_context(cfg)['token'], record['consentContext'])
        self.assertNotIn(solver.generate_schedule([employee], {'A':grid}, cfg)['status'], ('OPTIMAL','FEASIBLE'))

    def test_context_only_changes_with_agreement(self):
        cfg = config()
        token = boundary_context(cfg)['token']
        for change in [dict(allowPreferredBoundaryExtras=True), dict(wFairness=0),
                       dict(burdenWeight=8), dict(reqStaffOpen=2), dict(maxShiftLength=3), dict(minShiftLength=1)]:
            self.assertEqual(boundary_context(dict(cfg, **change))['token'], token)
        for change in [dict(maxMorningShifts=1), dict(maxEveningShifts=0),
                       dict(maxMorningPlusEvening=0),
                       dict(dayCloseHours={'Mon': 8}), dict(hourStart=8)]:
            self.assertNotEqual(boundary_context(dict(cfg, **change))['token'], token)

    def test_preferred_blocks_lengths_calendar_and_strict_levels(self):
        for minimum in [2, 3, 4]:
            cfg = config(days=['Mon', 'Fri'], hourEnd=21, lateHourStart=19,
                         reqStaffLate=1, minShiftLength=minimum, maxShiftLength=6)
            grid = {f'Mon_{h:02d}': 2 for h in range(7, 22)}
            status = consent_status(grid, {}, cfg)
            self.assertEqual(status['candidates'], {'openings': ['Mon'], 'closings': ['Mon']})
            for value in [None, '2', 3, True, 1, 0, 2.0]:
                self.assertEqual(consent_status(dict(grid, Mon_08=value), {}, cfg)['candidates']['openings'], [])
            self.assertEqual(consent_status(dict(grid, Mon_20=1), {}, cfg)['candidates']['closings'], [])
            if minimum == 4:
                self.assertEqual(consent_status(dict(grid, Mon_18=1), {}, cfg)['candidates']['closings'], ['Mon'])
                self.assertEqual(consent_status(dict(grid, Mon_18=0), {}, cfg)['candidates']['closings'], [])
        cfg = config(minShiftLength=3, maxShiftLength=3)
        self.assertEqual(boundary_context(cfg)['blocks']['Mon']['opening'], [])

    def test_opening_extras_feature_consent_partial_zero_caps_and_hours(self):
        employee = dict(id=1, name='A', minHours=0, maxHours=6)
        grid = {f'{d}_{h:02d}': 2 for d in ['Mon','Tue','Wed'] for h in [7,8]}
        for enabled, opening, closing in itertools.product([False, True], repeat=3):
            cfg = config(allowPreferredBoundaryExtras=enabled)
            grant(cfg, opening=opening, closing=closing)
            result = solver.generate_schedule([employee], {'A': grid}, cfg)
            self.assertEqual(result['status'] in ('OPTIMAL', 'FEASIBLE'), enabled and opening)
            if enabled and opening:
                row = result['boundarySummary'][0]
                self.assertEqual((row['openings'], row['qualifyingOpenings']), (3,3))
        cfg = config(allowPreferredBoundaryExtras=True, maxMorningShifts=0, maxMorningPlusEvening=0)
        grant(cfg)
        self.assertEqual(solver.generate_schedule([employee], {'A':grid}, cfg)['status'], 'OPTIMAL')
        for invalid in [dict(grid, Mon_08=1), dict(grid, Mon_08=0)]:
            self.assertNotIn(solver.generate_schedule([employee], {'A':invalid}, cfg)['status'], ('OPTIMAL','FEASIBLE'))
        self.assertNotIn(solver.generate_schedule([dict(employee,maxHours=5)], {'A':grid}, cfg)['status'], ('OPTIMAL','FEASIBLE'))
        grant(cfg, employee_id=2)
        del cfg['boundaryConsents']['1']
        self.assertNotIn(solver.generate_schedule([employee], {'A':grid}, cfg)['status'], ('OPTIMAL','FEASIBLE'))

    def test_credit_is_assigned_and_combined_cap_is_independent(self):
        cfg = config(days=['Mon','Tue','Wed'], hourEnd=10, lateHourStart=9, reqStaffLate=1,
                     minShiftLength=2, maxShiftLength=4, allowPreferredBoundaryExtras=True)
        employees = [dict(id=i,name=n,minHours=0,maxHours=12) for i,n in [(1,'A'),(2,'B'),(3,'C')]]
        grid = {n:{f'{d}_{h:02d}':1 for d in cfg['days'] for h in range(7,11)} for n in ['A','B','C']}
        grid['A'].update(Mon_07=2,Mon_08=2)
        grant(cfg)
        built = solver._build(employees,grid,cfg)
        # A takes two openings and a closing, exceeding only the combined cap.
        for day, h in [('Mon',7),('Tue',7),('Wed',9)]: built['model'].Add(built['work'][0,day,h] == 1)
        self.assertIn(solver._solve(built['model'],cfg)[1], [cp_model.FEASIBLE,cp_model.OPTIMAL])
        # Its fully preferred Monday is unassigned: it cannot fund Tuesday/Wednesday.
        cfg.update(maxMorningShifts=1, maxMorningPlusEvening=10, maxEveningShifts=5)
        grant(cfg)
        built = solver._build(employees,grid,cfg)
        for day, value in [('Mon',0),('Tue',1),('Wed',1)]: built['model'].Add(built['work'][0,day,7] == value)
        self.assertEqual(solver._solve(built['model'],cfg)[1], cp_model.INFEASIBLE)

    def test_closing_extras_full_block_and_preceding_hour(self):
        for minimum in [2,3,4]:
            cfg = config(days=['Mon','Tue'],hourStart=18,hourEnd=21,lateHourStart=19,
                         reqStaffLate=1,minShiftLength=minimum,maxShiftLength=4,
                         maxMorningShifts=2,maxEveningShifts=0,maxMorningPlusEvening=2,
                         allowPreferredBoundaryExtras=True)
            grant(cfg,opening=False,closing=True)
            e = dict(id=1,name='A',minHours=0,maxHours=8)
            grid = {f'{d}_{h}':2 if h >= 19 else 1 for d in cfg['days'] for h in range(18,22)}
            result = solver.generate_schedule([e],{'A':grid},cfg)
            self.assertEqual(result['status'],'OPTIMAL')
            self.assertEqual(result['boundarySummary'][0]['qualifyingClosings'],2)
            for d in cfg['days']:
                for h in range(19,22): self.assertEqual(result['work'][d][h],['A'])
            self.assertNotIn(solver.generate_schedule([e],{'A':dict(grid,Mon_20=1)},cfg)['status'],('OPTIMAL','FEASIBLE'))

    def test_lexicographic_optimum_against_exhaustive_work_assignments(self):
        cfg = config(days=['Mon','Tue'], hourEnd=9, lateHourStart=9, reqStaffLate=1,
                     minShiftLength=1,maxShiftLength=3,maxEveningShifts=2,
                     allowPreferredBoundaryExtras=True,wFairness=0,wPreference=999999)
        employees = [dict(id=i+1,name=n,minHours=0,maxHours=4) for i,n in enumerate('ABC')]
        grids = [
            # Fairness 500 / 5 preferred beats a feasible 6-preferred schedule.
            {'A':[2,1,2,2,2,2], 'B':[1,2,2,1,2,1], 'C':[1,2,2,1,1,1]},
            {'A':[2,2,1,1,1,2], 'B':[2,1,1,2,2,1], 'C':[1,2,2,2,1,2]},
            {'A':[2,2,2,2,2,2], 'B':[1,1,1,1,1,1], 'C':[2,1,2,1,2,1]},
            # Equal fairness/preference with both zero and positive overruns.
            {'A':[2,1,1,1,1,2], 'B':[1,2,1,1,2,1], 'C':[2,2,1,2,2,2]},
            # A's isolated preferred hour cannot form a two-hour shift. At the
            # resulting equal fairness floor, B/C still maximize preference.
            {'A':[2,0,0,0,0,0], 'B':[2,2,2,2,1,1], 'C':[2,1,1,2,2,2]},
        ]
        slots = list(itertools.product(cfg['days'],range(7,10)))
        for values in grids:
            cfg['minShiftLength'] = 2 if values is grids[-1] else 1
            grid = {n:{f'{d}_{h:02d}':v for (d,h),v in zip(slots,lv)} for n,lv in values.items()}
            for e in employees: grant(cfg,e['id'],True,True)
            weights = solver.burden_weights(employees,grid,cfg)
            scores = set()
            # Enumerate the hard model and independently score every assignment.
            built = solver._build(employees,grid,cfg)
            class Collect(cp_model.CpSolverSolutionCallback):
                def on_solution_callback(self):
                    work, _ = solver._extract(self,built,employees)
                    fair = solver.fairness_report(work,employees,grid,cfg,weights)
                    rows = solver.boundary_report(work,employees,grid,cfg)
                    scores.add((min(r['dealScore'] for r in fair),sum(r['preferredSatisfied'] for r in fair),
                                -sum(r['overrun'] for r in rows),sum((r['openings']>0)+(r['closings']>0) for r in rows)))
            engine = cp_model.CpSolver()
            engine.parameters.enumerate_all_solutions = True
            engine.parameters.num_search_workers = 1
            engine.Solve(built['model'], Collect())
            result = solver.generate_schedule(employees,grid,cfg,seed=1)
            actual = tuple(stage['score'] * (-1 if stage['name']=='overruns' else 1) for stage in result['optimizationStages'])
            self.assertEqual(actual,max(scores))
            self.assertTrue(result['fairnessOptimal'])
            if values is grids[0]:
                self.assertGreater(max(x[1] for x in scores),actual[1])
            if values is grids[3]:
                self.assertTrue(any(x[:2] == actual[:2] and x[2] < actual[2] for x in scores))
            if values is grids[-1]:
                self.assertTrue(any(x[0] == actual[0] and x[1] < actual[1] for x in scores))

    def test_each_phase_timeout_keeps_last_locked_incumbent(self):
        cfg = config(days=['Mon'],maxMorningShifts=1)
        employees = [dict(id=1,name='A',minHours=0,maxHours=2)]
        grid = {'A':{'Mon_07':2,'Mon_08':2}}
        original = solver._solve
        for phase in range(4):
            calls = []
            def limited(model, configuration, **kw):
                calls.append(kw['budget'])
                if len(calls) == phase+1: return None, cp_model.UNKNOWN
                return original(model,configuration,**kw)
            with patch.object(solver,'_solve',side_effect=limited):
                result = solver.generate_schedule(employees,grid,cfg)
            self.assertEqual(result['status'],'UNKNOWN' if phase==0 else 'FEASIBLE')
            self.assertTrue(all(0 < n <= cfg['solverTimeLimit'] for n in calls))
            self.assertEqual(calls,sorted(calls,reverse=True))
            if phase: self.assertEqual(result['fairnessFloor'],1000)
        def feasible(model, configuration, **kw):
            engine, _ = original(model,configuration,**kw)
            return engine, cp_model.FEASIBLE
        with patch.object(solver,'_solve',side_effect=feasible) as mocked:
            result = solver.generate_schedule(employees,grid,cfg)
        self.assertFalse(result['fairnessOptimal'])
        self.assertEqual(mocked.call_count,1)


class ConsentApiTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    context = test_app.AppTests.context
    overview = test_app.AppTests.overview
    create_folder = test_app.AppTests.create_folder

    def submit(self, **changes):
        ctx = self.context()
        payload = dict(name='A',folderId=ctx['folder']['id'],revision=ctx['revision'],
                       availability={'Mon_07':2,'Mon_08':2,'Mon_09':2},comment='saved',
                       allowExtraOpenings=True,allowExtraClosings=False,
                       consentContext=ctx['boundaryContext']['token'])
        payload.update(changes)
        return self.client.post('/api/availability',json=payload)

    def record(self):
        fid = self.context()['folder']['id']
        overview = self.overview(fid)
        submitted = {r['employeeId'] for r in overview['submissions']}
        rows = [e for e in overview['employees'] if e['id'] in submitted and (e['name'] == 'A' or e['name'].startswith('A ('))]
        name = max(rows,key=lambda e:e['id'])['name'] if rows else 'A'
        return self.client.get('/api/availability/'+name,query_string={'folderId':fid}).json

    def permission_payload(self, employee_id=None, folder_id=None):
        folder_id = folder_id or self.context()['folder']['id']
        overview = self.overview(folder_id)
        employee_id = employee_id or self.record()['employeeId']
        row = next(s for s in overview['submissions'] if s['employeeId'] == employee_id)
        return dict(employeeId=row['employeeId'], folderId=folder_id,
                    permissionVersion=row['permissionVersion'], consentContext=overview['boundaryContext']['token'],
                    allowExtraOpenings=row['consent']['allowExtraOpenings'],
                    allowExtraClosings=row['consent']['allowExtraClosings'])

    def save_permissions(self, payload):
        return self.client.put('/api/admin/boundary-permissions',headers=self.headers,json=payload)

    def test_shift_length_changes_persist_permission_and_current_candidates(self):
        self.submit()
        self.client.put('/api/config',headers=self.headers,json={'allowPreferredBoundaryExtras':True})
        token = self.record()['consent']['consentContext']
        for minimum, candidates in [(2,['Mon']), (4,[]), (2,['Mon'])]:
            self.assertEqual(self.client.put('/api/config',headers=self.headers,json={'minShiftLength':minimum}).status_code,200)
            status = self.record()['consent']
            self.assertEqual(status['consentContext'], token)
            self.assertTrue(status['allowExtraOpenings'])
            self.assertFalse(status['reconfirmationNeeded'])
            self.assertEqual(status['candidates']['openings'], candidates)
        self.client.put('/api/config',headers=self.headers,json={'maxShiftLength':4})
        self.assertFalse(self.record()['consent']['reconfirmationNeeded'])
        for settings in [{'maxMorningPlusEvening':3}, {'hourStart':8}]:
            self.client.put('/api/config',headers=self.headers,json=settings)
            self.assertTrue(self.record()['consent']['reconfirmationNeeded'])
            self.submit()

    def test_supervisor_independent_choices_preserve_data_and_employee_can_update(self):
        self.submit(allowExtraOpenings=False)
        self.submit(name='B',allowExtraOpenings=False)
        fid = self.context()['folder']['id']
        other = self.create_folder('Other',False)['id']
        with db.connection_context():
            original = FolderAvailability.get(FolderAvailability.employee == self.record()['employeeId'])
            FolderAvailability.create(employee=original.employee_id,folder=other,data_json='{"Tue_08":1}',comment='other')
        unrelated = self.overview(other)
        b = self.overview(fid)['submissions'][1]
        before = self.record()
        for openings, closings in [(True,False),(False,True),(True,True),(False,False)]:
            payload = dict(self.permission_payload(),allowExtraOpenings=openings,allowExtraClosings=closings)
            response = self.save_permissions(payload)
            self.assertEqual(response.status_code,200,response.json)
            row = self.record()
            self.assertEqual((row['consent']['allowExtraOpenings'],row['consent']['allowExtraClosings']),(openings,closings))
            for key in ['availability','comment','submittedAt']:
                self.assertEqual(row[key],before[key])
            self.assertFalse(row['consent']['effectiveOpenings']) # Global feature remains off.
        self.assertEqual(self.overview(other),unrelated)
        self.assertEqual(self.overview(fid)['submissions'][1],b)
        self.save_permissions(dict(self.permission_payload(),allowExtraOpenings=True,allowExtraClosings=True))
        self.assertEqual(self.submit(allowExtraOpenings=False,allowExtraClosings=False).status_code,200)
        self.assertFalse(self.record()['consent']['allowExtraOpenings'])

    def test_supervisor_validation_conflicts_and_transaction_rollback(self):
        from peewee import OperationalError
        self.submit()
        payload = self.permission_payload()
        before = self.record()
        self.assertEqual(self.client.put('/api/admin/boundary-permissions',json=payload).status_code,401)
        for field, value in [('allowExtraOpenings',1),('allowExtraClosings','true'),('employeeId',True),
                             ('folderId','1'),('availability',{}),('comment','other')]:
            self.assertEqual(self.save_permissions(dict(payload,**{field:value})).status_code,400)
        self.assertEqual(self.save_permissions(dict(payload,employeeId=9999)).status_code,404)
        self.assertEqual(self.save_permissions(dict(payload,folderId=9999)).status_code,404)
        self.assertEqual(self.record(),before)
        with patch.object(FolderAvailability,'save',side_effect=OperationalError('disposable injected failure')):
            self.assertEqual(self.save_permissions(dict(payload,allowExtraOpenings=False)).status_code,503)
        self.assertEqual(self.record(),before)
        # Shift-length updates alone do not conflict with an explicit supervisor save.
        self.client.put('/api/config',headers=self.headers,json={'minShiftLength':2,'maxShiftLength':5})
        self.assertEqual(self.save_permissions(payload).status_code,200)
        payload = self.permission_payload()
        self.submit(comment='separate employee submission')
        self.assertEqual(self.save_permissions(payload).status_code,200)  # Separate submission cannot invalidate this employee's permissions.
        payload = self.permission_payload()
        self.client.put('/api/config',headers=self.headers,json={'maxMorningShifts':3})
        self.assertEqual(self.save_permissions(payload).status_code,409)
        self.assertTrue(self.record()['consent']['reconfirmationNeeded'])
        self.assertEqual(self.save_permissions(self.permission_payload()).status_code,200)
        self.assertFalse(self.record()['consent']['reconfirmationNeeded'])
        payload = self.permission_payload()
        self.save_permissions(dict(payload,allowExtraClosings=True))
        self.assertEqual(self.save_permissions(payload).status_code,409)

    def test_legacy_token_migration_only_upgrades_current_agreements(self):
        self.submit()
        self.submit(name='B')
        with db.connection_context():
            cfg = web.get_config()
            FolderAvailability.update(consent_context=legacy_consent_token(cfg)).where(
                FolderAvailability.employee == Employee.get(Employee.name=='A')).execute()
            stale = legacy_consent_token(dict(cfg,maxMorningShifts=99))
            FolderAvailability.update(consent_context=stale).where(
                FolderAvailability.employee == Employee.get(Employee.name=='B')).execute()
        init_db();init_db()
        rows = self.overview(self.context()['folder']['id'])['submissions']
        self.assertFalse(rows[0]['consent']['reconfirmationNeeded'])
        self.assertTrue(rows[0]['consent']['allowExtraOpenings'])
        self.assertTrue(rows[1]['consent']['reconfirmationNeeded'])
        self.assertEqual(rows[1]['consent']['consentContext'],stale)
        self.client.put('/api/config',headers=self.headers,json={'minShiftLength':4})
        self.assertFalse(self.record()['consent']['reconfirmationNeeded'])

    def test_supervisor_permissions_captured_in_generation_and_historical_snapshot(self):
        self.submit(allowExtraOpenings=False)
        payload = self.permission_payload()
        self.save_permissions(dict(payload,allowExtraOpenings=True,allowExtraClosings=True))
        def generate(roster,availability,cfg,seed=None):
            self.assertTrue(cfg['boundaryConsents'][str(payload['employeeId'])]['allowExtraClosings'])
            self.save_permissions(dict(self.permission_payload(),allowExtraClosings=False))
            return {'status':'FEASIBLE','work':{},'schedule':{},'fairness':[]}
        with patch.object(web.solver_module,'generate_schedule',side_effect=generate):
            response = self.client.post('/api/generate',headers=self.headers,
                json={'folderId':payload['folderId'],'employeeIds':[payload['employeeId']]})
        self.assertEqual(response.status_code,200,response.json)
        saved = self.client.get(f'/api/folders/{payload["folderId"]}/schedules/{response.json["savedScheduleId"]}',headers=self.headers).json
        self.assertTrue(saved['submissions'][0]['consent']['allowExtraClosings'])
        self.assertFalse(self.record()['consent']['allowExtraClosings'])

    def test_defaults_validation_atomic_conflict_and_reconfirmation(self):
        self.assertFalse(self.context()['config']['allowPreferredBoundaryExtras'])
        for v in ['false',1,None,{},[]]:
            self.assertEqual(self.client.put('/api/config',headers=self.headers,json={'allowPreferredBoundaryExtras':v}).status_code,400)
            self.assertEqual(self.submit(allowExtraOpenings=v).status_code,400)
        self.assertEqual(self.submit().status_code,200)
        before = self.record()
        self.assertEqual(self.submit(consentContext='stale',comment='lost',availability={}).status_code,409)
        self.assertEqual(self.record(),before)
        self.assertEqual(self.submit(revision=-1,comment='lost').status_code,409)
        self.assertEqual(self.record(),before)
        for update in [dict(wPreference=700),dict(allowPreferredBoundaryExtras=True)]:
            self.assertEqual(self.client.put('/api/config',headers=self.headers,json=update).status_code,200)
            self.assertFalse(self.record()['consent']['reconfirmationNeeded'])
        self.assertTrue(self.record()['consent']['effectiveOpenings'])
        self.client.put('/api/config',headers=self.headers,json={'maxMorningShifts':3})
        self.assertTrue(self.record()['consent']['reconfirmationNeeded'])
        self.assertTrue(self.record()['consent']['allowExtraOpenings'])
        self.assertFalse(self.record()['consent']['effectiveOpenings'])
        self.assertEqual(self.submit().status_code,200)
        self.assertTrue(self.record()['consent']['effectiveOpenings'])

    def test_independent_revocation_folder_isolation_and_admin_preservation(self):
        self.submit(allowExtraClosings=True)
        self.submit(allowExtraOpenings=False,allowExtraClosings=True)
        self.assertFalse(self.record()['consent']['allowExtraOpenings'])
        self.assertTrue(self.record()['consent']['allowExtraClosings'])
        original = self.record()
        fid = self.context()['folder']['id']
        payload = dict(employeeId=original['employeeId'],folderId=fid,availability={},comment='admin')
        for key in ['allowExtraOpenings','allowExtraClosings','consentContext']:
            self.assertEqual(self.client.put('/api/admin/availability',headers=self.headers,json=dict(payload,**{key:True})).status_code,400)
        self.assertEqual(self.record(),original)
        self.assertEqual(self.client.put('/api/admin/availability',headers=self.headers,json=payload).status_code,200)
        self.assertTrue(self.record()['consent']['allowExtraClosings'])
        self.assertEqual(self.record()['consent']['candidates']['closings'],[])
        self.create_folder('Different')
        self.assertFalse(self.record()['found'])
        self.assertEqual(self.submit(folderId=fid).status_code,409)
        ctx = self.context()
        self.client.post('/api/availability',json=dict(name='A',folderId=ctx['folder']['id'],revision=ctx['revision'],availability={'Mon_07':2}))
        self.assertFalse(self.record()['consent']['allowExtraClosings'])
        self.assertTrue(self.overview(fid)['submissions'][0]['consent']['allowExtraClosings'])
        self.assertNotIn('boundaryConsents',ctx['config'])
        self.assertNotIn('consent',ctx)

    def test_saved_snapshot_freezes_consent_and_settings_during_solve(self):
        self.submit()
        fid = self.context()['folder']['id']
        eid = self.record()['employeeId']
        def generate(roster,availability,cfg,seed=None):
            # Consent-only edits preserve the frozen agreement during solving.
            # A full resubmission now invalidates in-flight generation instead.
            self.client.put('/api/admin/boundary-permissions', headers=self.headers, json=dict(
                folderId=fid, employeeId=eid, permissionVersion=self.record()['permissionVersion'],
                consentContext=self.context()['boundaryContext']['token'],
                allowExtraOpenings=False, allowExtraClosings=False))
            self.client.put('/api/config',headers=self.headers,json={'maxMorningShifts':4})
            return {'status':'FEASIBLE','work':{},'schedule':{},'fairness':[]}
        with patch.object(web.solver_module,'generate_schedule',side_effect=generate):
            response = self.client.post('/api/generate',headers=self.headers,json={'folderId':fid,'employeeIds':[eid]})
        self.assertEqual(response.status_code,200,response.json)
        saved = self.client.get(f'/api/folders/{fid}/schedules/{response.json["savedScheduleId"]}',headers=self.headers).json
        self.assertTrue(saved['submissions'][0]['consent']['allowExtraOpenings'])
        self.assertTrue(saved['result']['config']['boundaryConsents'][str(eid)]['allowExtraOpenings'])
        self.assertEqual(saved['result']['config']['maxMorningShifts'],2)
        self.assertFalse(self.record()['consent']['allowExtraOpenings'])

    def test_additive_migration_preserves_legacy_submission_and_is_idempotent(self):
        self.submit()
        with db.connection_context():
            # Recreate just the disposable test table in its pre-feature shape.
            db.execute_sql('ALTER TABLE folderavailability DROP COLUMN allow_extra_openings')
            db.execute_sql('ALTER TABLE folderavailability DROP COLUMN allow_extra_closings')
            db.execute_sql('ALTER TABLE folderavailability DROP COLUMN consent_context')
        init_db(); init_db()
        row = self.record()
        self.assertEqual(row['comment'],'saved')
        self.assertEqual(row['availability'],{'Mon_07':2,'Mon_08':2,'Mon_09':2})
        self.assertFalse(row['consent']['allowExtraOpenings'])
        self.assertIsNone(row['consent']['consentContext'])


if __name__ == '__main__':
    unittest.main()
