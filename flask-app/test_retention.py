"""Retention regressions shared by isolated SQLite/PostgreSQL host suites."""
import concurrent.futures
import datetime as dt
import json
import os
import subprocess
import sys
import threading
from unittest.mock import patch

import app as web
from models import (db, write_transaction, init_db, Folder, FolderAvailability,
                    Employee, Config, SubmissionState, AdminSession, Availability,
                    SavedSchedule, SavedWeekendSchedule)
from retention import run_retention, cutoff_for, utc_naive

NOW = dt.datetime(2026, 10, 3, 12, 0, 0, 123456)
CUTOFF = dt.datetime(2025, 4, 3, 12, 0, 0, 123456)
OLD = CUTOFF - dt.timedelta(microseconds=1)


class RetentionTests:
    def request_cleanup(self):
        from request_retention import RequestRetention
        gate = RequestRetention()
        self.addCleanup(web.app.config.update, REQUEST_RETENTION_ENABLED=False)
        web.app.config['REQUEST_RETENTION_ENABLED'] = True
        return patch.object(web, 'request_retention', gate)

    def test_request_retention_visit_after_four_months_preserves_boundary(self):
        folder_id = self.retention_fixture()
        with db.connection_context():
            SavedSchedule.create(folder=folder_id, created_at=CUTOFF, snapshot_json='{}')
            SavedWeekendSchedule.create(folder=folder_id, created_at=CUTOFF + dt.timedelta(days=1), snapshot_json='{}')
        with self.request_cleanup(), patch('request_retention.run_retention',
                side_effect=lambda **kw: run_retention(now=NOW, **kw)) as cleanup:
            # No visit means no cleanup; the first API read performs the real pass.
            self.assertEqual(cleanup.call_count, 0)
            response = self.client.get('/api/submission-context')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['folder']['id'], folder_id)
            self.client.get('/api/submission-context')
            self.assertEqual(cleanup.call_count, 1)
        with db.connection_context():
            self.assertEqual(FolderAvailability.select().count(), 0)
            self.assertEqual(SavedSchedule.select().count(), 1)
            self.assertEqual(SavedWeekendSchedule.select().count(), 1)

    def test_request_retention_failure_rolls_back_and_does_not_break_visit(self):
        folder_id = self.retention_fixture()
        with db.connection_context():
            SavedSchedule.create(folder=folder_id, created_at=OLD, snapshot_json='{}')
        with self.request_cleanup(), patch.object(SavedSchedule, 'delete', side_effect=RuntimeError('private payload')):
            with self.assertLogs(web.app.logger, level='WARNING') as logs:
                response = self.client.get('/api/submission-context')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('private payload', ''.join(logs.output))
            self.assertEqual(self.client.get('/api/submission-context').status_code, 200)
        with db.connection_context():
            self.assertEqual(FolderAvailability.select().count(), 1)
            self.assertEqual(SavedSchedule.select().count(), 1)

    def test_request_retention_rate_limit_backlog_retry_and_static_exclusion(self):
        from request_retention import RequestRetention
        gate = RequestRetention()
        with self.request_cleanup(), patch.object(web, 'request_retention', gate), \
                patch('request_retention.time.monotonic', return_value=100) as clock, \
                patch('request_retention.run_retention', return_value={'backlogRemaining': True}) as cleanup:
            self.client.get('/healthz')
            self.client.get('/')
            self.assertEqual(cleanup.call_count, 0)
            self.client.get('/api/submission-context')
            clock.return_value = 159
            self.client.get('/api/submission-context')
            self.assertEqual(cleanup.call_count, 1)
            clock.return_value = 160
            cleanup.return_value = {'backlogRemaining': False}
            self.client.get('/api/submission-context')
            clock.return_value = 161
            self.client.get('/api/submission-context')
            self.assertEqual(cleanup.call_count, 2)
            clock.return_value = 86560
            self.client.get('/api/submission-context')
            self.assertEqual(cleanup.call_count, 3)

    def test_retention_restores_connection_timeouts(self):
        from peewee import PostgresqlDatabase
        with db.connection_context():
            query = 'SHOW lock_timeout' if isinstance(db, PostgresqlDatabase) else 'PRAGMA busy_timeout'
            before = db.execute_sql(query).fetchone()
            run_retention(now=NOW, apply=True, timeout_ms=200, statement_timeout_ms=5000)
            self.assertEqual(db.execute_sql(query).fetchone(), before)

    def test_request_retention_concurrent_visits_skip_busy_pass(self):
        from request_retention import RequestRetention
        gate = RequestRetention()
        entered, release = threading.Event(), threading.Event()
        def cleanup(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'backlogRemaining': False}
        with patch('request_retention.run_retention', side_effect=cleanup) as run:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(gate.maybe_run, web.app.logger)
                try:
                    self.assertTrue(entered.wait(5))
                    second = pool.submit(gate.maybe_run, web.app.logger)
                    second.result(timeout=1)
                    self.assertEqual(run.call_count, 1)
                finally:
                    release.set()
                first.result(timeout=5)

    def cleanup(self, **kwargs):
        with db.connection_context():
            return run_retention(now=NOW, **kwargs)

    def retention_fixture(self):
        folder_id = self.context()['folder']['id']
        # Legacy accepted rows predate collection-code intake.
        with db.connection_context():
            employee = Employee.create(name='Old')
            FolderAvailability.create(employee=employee, folder=folder_id, data_json='{}', comment='Sensitive fixture')
        with db.connection_context():
            Folder.update(created_at=OLD).where(Folder.id == folder_id).execute()
            FolderAvailability.update(submitted_at=OLD).where(FolderAvailability.folder == folder_id).execute()
        return folder_id

    def test_retention_calendar_and_utc(self):
        self.assertEqual(cutoff_for(NOW), CUTOFF)
        for now, expected in [(dt.datetime(2025, 8, 31, 23, 12), dt.datetime(2024, 2, 29, 23, 12)),
                              (dt.datetime(2026, 8, 31), dt.datetime(2025, 2, 28)),
                              (dt.datetime(2024, 2, 29), dt.datetime(2022, 8, 29))]:
            self.assertEqual(cutoff_for(now), expected)
        local = NOW.replace(tzinfo=dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-6)))
        self.assertEqual(cutoff_for(local), CUTOFF)
        self.assertEqual(utc_naive(NOW), NOW)

    def test_retention_strict_boundary_all_records_and_preservation(self):
        folder_id = self.retention_fixture()
        with db.connection_context():
            preserved = [(m, list(m.select().dicts())) for m in (Employee, Config, AdminSession)]
            for stamp in (OLD, CUTOFF, CUTOFF + dt.timedelta(microseconds=1)):
                for model in (SavedSchedule, SavedWeekendSchedule):
                    model.create(folder=folder_id, created_at=stamp, snapshot_json='{"secret":"fixture"}')
                Availability.create(employee_name=str(stamp), submitted_at=stamp, data_json='{}')
            newer = Folder.create(name='Young empty', created_at=CUTOFF)
            archived = Folder.create(name='Old archived', created_at=OLD, archived=True)
        result = self.cleanup(apply=True)
        self.assertEqual(result['deleted'], dict(availability=1, weekdaySchedules=1, weekendSchedules=1, legacyAvailability=1, folders=1, intakeResponses=0, collectionCodes=0))
        with db.connection_context():
            self.assertIsNotNone(Folder.get_or_none(Folder.id == folder_id))
            self.assertIsNotNone(Folder.get_or_none(Folder.id == newer.id))
            self.assertIsNone(Folder.get_or_none(Folder.id == archived.id))
            self.assertEqual(SubmissionState.get_by_id(1).active_folder_id, folder_id)
            for m, rows in preserved:
                self.assertEqual(list(m.select().dicts()), rows)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('Sensitive fixture', json.dumps(result))
        self.assertFalse(result['backlogRemaining'])
        self.assertEqual(sum(self.cleanup(apply=True)['deleted'].values()), 0)
        # Surviving active folders permit a fresh ordinary submission.
        self.assertEqual(self.submit('Old').status_code, 200)

    def test_retention_availability_boundary_and_resubmission(self):
        folder_id = self.context()['folder']['id']  # young parent doesn't exempt old children
        for name in ['Before', 'Equal', 'After', 'Resubmit', 'Consent']:
            self.submit(name)
        with db.connection_context():
            for name, stamp in [('Before', OLD), ('Equal', CUTOFF), ('After', CUTOFF + dt.timedelta(microseconds=1)), ('Resubmit', OLD), ('Consent', OLD)]:
                FolderAvailability.update(submitted_at=stamp).where(FolderAvailability.employee == Employee.get(Employee.name == name)).execute()
            row = FolderAvailability.get(FolderAvailability.employee == Employee.get(Employee.name == 'Consent'))
            version = web.permission_version(row)
        ctx = self.context()
        self.assertEqual(self.client.put('/api/admin/boundary-permissions', headers=self.headers, json=dict(
            folderId=folder_id, employeeId=row.employee_id, permissionVersion=version,
            consentContext=ctx['boundaryContext']['token'], allowExtraOpenings=True, allowExtraClosings=False)).status_code, 200)
        self.assertEqual(self.submit('Resubmit').status_code, 200)
        result = self.cleanup(apply=True)
        self.assertEqual(result['deleted']['availability'], 3)
        self.assertEqual(set(self.overview(folder_id)['availability']), {'Equal', 'After', 'Resubmit (2)'})

    def test_retention_dry_run_changes_no_rows_or_schema(self):
        self.retention_fixture()
        with db.connection_context():
            tables = db.get_tables()
            before = {t: (db.get_columns(t), db.execute_sql('SELECT * FROM "' + t + '" ORDER BY 1').fetchall()) for t in tables}
        result = self.cleanup()
        self.assertEqual(result['candidates']['folders'], 1)
        self.assertEqual(sum(result['deleted'].values()), 0)
        with db.connection_context():
            self.assertEqual(db.get_tables(), tables)
            for t, (columns, rows) in before.items():
                self.assertEqual(db.get_columns(t), columns)
                self.assertEqual(db.execute_sql('SELECT * FROM "' + t + '" ORDER BY 1').fetchall(), rows)

    def test_retention_active_folder_restart_and_stale_preview(self):
        folder_id = self.retention_fixture()
        context = self.context()
        self.create_folder('Do not activate', False)
        preview = self.preview(folder_id)
        with db.connection_context():
            # A recent independent snapshot keeps the folder during child cleanup.
            saved = SavedSchedule.create(folder=folder_id, created_at=NOW, snapshot_json='{}')
        self.cleanup(apply=True)
        self.assertEqual(self.delete_folder(folder_id, preview).status_code, 409)
        with db.connection_context():
            saved.delete_instance()
            Availability.create(employee_name='Legacy old', data_json='{}', submitted_at=OLD)
        result = self.cleanup(apply=True)
        self.assertEqual(result['deleted']['folders'], 1)
        self.assertIsNone(self.context()['folder'])
        self.assertEqual(self.context()['revision'], context['revision'] + 1)
        self.assertEqual(self.submit(context=context).status_code, 409)
        init_db(); init_db()
        with db.connection_context():
            self.assertEqual(Availability.select().count(), 0)
            self.assertEqual(FolderAvailability.select().count(), 0)
            self.assertEqual(Folder.select().count(), 1)
            self.assertIsNotNone(SubmissionState.get_or_none(SubmissionState.id == 1))

    def test_retention_failure_early_and_late_rolls_back(self):
        folder_id = self.retention_fixture()
        before = self.context()
        for model in (FolderAvailability, Folder):
            with patch.object(model, 'delete', side_effect=RuntimeError('injected')):
                with self.assertRaises(RuntimeError):
                    self.cleanup(apply=True)
            self.assertEqual(self.context(), before)
            self.assertIn('Old', self.overview(folder_id)['availability'])
        self.assertEqual(self.cleanup(apply=True)['deleted']['folders'], 1)

    def test_retention_bounded_restart_and_two_cleaners(self):
        folder_id = self.retention_fixture()
        with db.connection_context():
            for i in range(5):
                SavedSchedule.create(folder=folder_id, created_at=OLD, snapshot_json='{}')
        first = self.cleanup(apply=True, max_rows=2)
        self.assertEqual(sum(first['deleted'].values()), 2)
        self.assertTrue(first['backlogRemaining'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.cleanup(apply=True), range(2)))
        self.assertEqual(sum(sum(r['deleted'].values()) for r in results), 5)
        self.assertEqual(sum(self.cleanup(apply=True)['deleted'].values()), 0)

    def test_retention_generation_and_weekend_save_reject_removed_inputs(self):
        folder_id = self.retention_fixture()
        with db.connection_context():
            employee_id = Employee.get(Employee.name == 'Old').id
            # Keep the folder alive so input validation, not folder identity, catches the race.
            SavedSchedule.create(folder=folder_id, created_at=NOW, snapshot_json='{}')
        def solve(*args, **kwargs):
            run_retention(now=NOW, apply=True)
            return {'status': 'FEASIBLE', 'schedule': {}, 'work': {}}
        with patch.object(web.solver_module, 'generate_schedule', side_effect=solve):
            response = self.client.post('/api/generate', headers=self.headers, json=dict(folderId=folder_id, employeeIds=[employee_id]))
        self.assertEqual(response.status_code, 409)
        self.submit('Old')
        config = dict(start_date='2026-10-02', end_date='2026-10-04')
        fake = dict(assignments=[], signup_shifts=[], effective_pool=[], rotating_counts={})
        with patch.object(web.weekend_generator, 'generate_weekend_schedule', return_value=fake):
            response = self.client.post('/api/generate_weekend', headers=self.headers, json=dict(folderId=folder_id, config=config))
        self.assertEqual(response.status_code, 200)
        with db.connection_context():
            FolderAvailability.update(submitted_at=OLD).where(FolderAvailability.folder == folder_id).execute()
        self.cleanup(apply=True)
        self.assertEqual(self.client.post('/api/save_weekend', headers=self.headers,
            json=dict(folderId=folder_id, snapshot=response.json)).status_code, 409)
        self.submit('Old')
        with db.connection_context():
            FolderAvailability.update(submitted_at=OLD).where(FolderAvailability.folder == folder_id).execute()
        def weekend_solve(*args, **kwargs):
            run_retention(now=NOW, apply=True)
            return fake
        with patch.object(web.weekend_generator, 'generate_weekend_schedule', side_effect=weekend_solve):
            response = self.client.post('/api/generate_weekend', headers=self.headers, json=dict(folderId=folder_id, config=config))
        self.assertEqual(response.status_code, 409)

    def test_retention_command_requires_production_identity_and_enable(self):
        for changes in [dict(DATABASE_URL=''), dict(DATABASE_URL='sqlite:///unsafe.db'),
                        dict(DATABASE_URL='postgresql://user:secret@wrong/db', RETENTION_DATABASE_HOST='expected', RETENTION_DATABASE_NAME='db')]:
            env = dict(os.environ, **changes)
            run = subprocess.run([sys.executable, 'cleanup_retention.py'], capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 1)
            self.assertNotIn('secret', run.stderr)
        env = dict(os.environ, DATABASE_URL='postgresql://user:secret@expected/db', RETENTION_DATABASE_HOST='expected', RETENTION_DATABASE_NAME='db', RETENTION_ENABLED='false')
        run = subprocess.run([sys.executable, 'cleanup_retention.py', '--apply'], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 1)
        self.assertIn('RETENTION_ENABLED', run.stderr)

    def test_retention_real_command_preview_apply_and_repeat(self):
        self.retention_fixture()
        env = dict(os.environ)
        args = []
        if env.get('DATABASE_URL'):
            from urllib.parse import urlsplit
            parsed = urlsplit(env['DATABASE_URL'])
            env.update(RETENTION_DATABASE_HOST=parsed.hostname, RETENTION_DATABASE_NAME=parsed.path[1:])
        else:
            # SQLite host switches db paths for each test; propagate this path.
            env['DATABASE_PATH'] = str(db.database)
            args = ['--local-test']
        env['RETENTION_ENABLED'] = 'false'
        run = subprocess.run([sys.executable, 'cleanup_retention.py', *args], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        preview = json.loads(run.stdout)
        self.assertEqual(preview['mode'], 'dry-run')
        self.assertEqual(sum(preview['deleted'].values()), 0)
        self.assertGreater(preview['candidates']['availability'], 0)
        env['RETENTION_ENABLED'] = 'true'
        run = subprocess.run([sys.executable, 'cleanup_retention.py', *args, '--apply'], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['deleted']['folders'], 1)
        run = subprocess.run([sys.executable, 'cleanup_retention.py', *args, '--apply'], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sum(json.loads(run.stdout)['deleted'].values()), 0)

    def test_retention_connection_closes_on_success_and_failure(self):
        import cleanup_retention
        env = dict(RETENTION_DATABASE_HOST='local', RETENTION_DATABASE_NAME='test')
        if not os.environ.get('DATABASE_URL'):
            args = ['--local-test']
        else:
            from urllib.parse import urlsplit
            parsed = urlsplit(os.environ['DATABASE_URL'])
            env.update(RETENTION_DATABASE_HOST=parsed.hostname, RETENTION_DATABASE_NAME=parsed.path[1:])
            args = []
        for failure in [False, True]:
            with patch.dict(os.environ, env), patch('retention.run_retention', side_effect=RuntimeError('secret') if failure else None, return_value={}):
                db.connect(reuse_if_open=True)
                self.assertEqual(cleanup_retention.main(args), int(failure))
                self.assertTrue(db.is_closed())

    def test_retention_submission_rechecked_after_shared_lock_and_timeout(self):
        folder_id = self.retention_fixture()
        entered, release = threading.Event(), threading.Event()
        def resubmit():
            with db.connection_context(), write_transaction():
                FolderAvailability.update(submitted_at=NOW).where(FolderAvailability.folder == folder_id).execute()
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('Test lock timed out')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            submission = workers.submit(resubmit)
            try:
                self.assertTrue(entered.wait(10))
                from peewee import OperationalError
                with self.assertRaises(OperationalError):
                    self.cleanup(apply=True, timeout_ms=20)
                cleanup = workers.submit(self.cleanup, apply=True)
            finally:
                release.set()
            submission.result(timeout=10)
            result = cleanup.result(timeout=10)
        self.assertEqual(sum(result['deleted'].values()), 0)
        self.assertIn('Old', self.overview(folder_id)['availability'])

    def test_retention_manual_deletion_and_activation_races(self):
        folder_id = self.retention_fixture()
        preview = self.preview(folder_id)
        def manual():
            with web.app.test_client() as client:
                return client.delete(f'/api/folders/{folder_id}', headers=self.headers,
                    json=dict(confirmationName=preview['folder']['name'], previewVersion=preview['previewVersion'])).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            clean = workers.submit(self.cleanup, apply=True)
            delete = workers.submit(manual)
            self.assertIn(delete.result(timeout=10), (200, 404))
            clean.result(timeout=10)
        other = self.create_folder('Old viewing folder', False)['id']
        with db.connection_context():
            Folder.update(created_at=OLD).where(Folder.id == other).execute()
        def activate():
            with web.app.test_client() as client:
                return client.patch(f'/api/folders/{other}', headers=self.headers, json={'activate': True}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            clean = workers.submit(self.cleanup, apply=True)
            active = workers.submit(activate)
            self.assertIn(active.result(timeout=10), (200, 404))
            clean.result(timeout=10)
        self.assertIsNone(self.context()['folder'])

    def test_retention_invalid_dates_protect_folder_and_schema_missing_fails(self):
        folder_id = self.retention_fixture()
        from peewee import SqliteDatabase
        invalid = 'invalid' if isinstance(db, SqliteDatabase) else 'infinity'
        with db.connection_context():
            db.execute_sql('UPDATE folderavailability SET submitted_at = %s' if not isinstance(db, SqliteDatabase)
                           else 'UPDATE folderavailability SET submitted_at = ?', (invalid,))
        result = self.cleanup(apply=True)
        self.assertEqual(result['invalidTimestamps']['availability'], 1)
        self.assertEqual(sum(result['deleted'].values()), 0)
        with db.connection_context():
            self.assertIsNotNone(Folder.get_or_none(Folder.id == folder_id))
            db.execute_sql('DROP TABLE savedweekendschedule')
        with self.assertRaises(Exception):
            self.cleanup()
        with db.connection_context():
            self.assertNotIn('savedweekendschedule', db.get_tables())
        init_db()  # Restore schema for the host's following tests only.

    def test_retention_legacy_aware_timestamp_precision(self):
        from peewee import SqliteDatabase
        with db.connection_context():
            if not isinstance(db, SqliteDatabase):
                return  # PostgreSQL schema stores naive UTC; checked separately.
            for index, stamp in enumerate([OLD, CUTOFF, CUTOFF + dt.timedelta(microseconds=1)]):
                aware = stamp.replace(tzinfo=dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-6)))
                Availability.create(employee_name='aware' + str(index), submitted_at=aware, data_json='{}')
        self.assertEqual(self.cleanup(apply=True)['deleted']['legacyAvailability'], 1)
