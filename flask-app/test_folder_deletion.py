"""Deletion regressions shared by the disposable SQLite and PostgreSQL suites.

This mixin never configures a database or reads DATABASE_URL. Its host suite must
supply an isolated database, authenticated client, context/submit/create helpers.
"""
import concurrent.futures
import json
import threading
from unittest.mock import patch

import app as web
from models import (db, Employee, Folder, FolderAvailability, SubmissionState,
                    SavedSchedule, SavedWeekendSchedule, Config)


class FolderDeletionTests:
    def preview(self, folder_id):
        response = self.client.get(f'/api/folders/{folder_id}/deletion-preview', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def delete_folder(self, folder_id, preview=None, **overrides):
        preview = preview or self.preview(folder_id)
        payload = {'confirmationName': preview['folder']['name'], 'previewVersion': preview['previewVersion']}
        payload.update(overrides)
        return self.client.delete(f'/api/folders/{folder_id}', headers=self.headers, json=payload)

    def seed_snapshots(self, folder_id):
        with db.connection_context():
            SavedSchedule.create(folder=folder_id, snapshot_json=json.dumps({'result': {'schedule': {'Mon': 'Alex'}}}))
            SavedWeekendSchedule.create(folder=folder_id, snapshot_json=json.dumps({'assignments': [
                {'shift': 'friday_evening', 'assigned': 1}, {'shift': 'sunday_afternoon', 'assigned': 2}]}))

    def test_delete_scope_preserves_shared_and_other_folder_records(self):
        first = self.context()['folder']['id']
        self.submit('Alex', comment='First comment')
        self.submit('Blair', comment='Second comment')
        self.seed_snapshots(first)
        other = self.create_folder(self.context()['folder']['name'])['id']  # same name, separate ID
        self.submit('Alex', comment='Keep this comment')
        self.seed_snapshots(other)
        self.client.put('/api/employees/Alex', headers=self.headers,
                        json={'isLead': True, 'minHours': 10, 'maxHours': 25})
        self.client.put('/api/config', headers=self.headers, json={'wFairness': 123})
        with db.connection_context():
            preserved = [(model, list(model.select().dicts())) for model in (Employee, Config, SubmissionState)]
            others = [(model, list(model.select().where(model.folder == other).dicts()))
                      for model in (FolderAvailability, SavedSchedule, SavedWeekendSchedule)]
        preview = self.preview(first)
        self.assertEqual(preview['counts'], {'availabilitySubmissions': 2, 'weekdaySchedules': 1, 'weekendSchedules': 1})
        self.assertFalse(preview['acceptsSubmissions'])
        self.assertEqual(self.delete_folder(first, preview).status_code, 200)
        with db.connection_context():
            self.assertIsNone(Folder.get_or_none(Folder.id == first))
            for model, records in preserved:
                self.assertEqual(list(model.select().dicts()), records)
            for model, records in others:
                self.assertEqual(list(model.select().where(model.folder == other).dicts()), records)
                self.assertEqual(model.select().where(model.folder == first).count(), 0)

    def test_delete_active_folder_invalidates_forms_without_activating_another(self):
        old = self.context()
        self.submit(comment='Delete me')
        self.seed_snapshots(old['folder']['id'])
        self.create_folder('Viewing only', False)
        self.assertTrue(self.preview(old['folder']['id'])['acceptsSubmissions'])
        self.assertEqual(self.delete_folder(old['folder']['id']).status_code, 200)
        current = self.context()
        self.assertIsNone(current['folder'])
        self.assertEqual(current['revision'], old['revision'] + 1)
        self.assertEqual(self.submit(context=old).status_code, 409)
        with db.connection_context():
            self.assertEqual(FolderAvailability.select().count(), 0)
            self.assertEqual(Employee.select().count(), 1)

    def test_delete_archived_empty_and_final_folder_survives_restart(self):
        original = self.context()['folder']['id']
        archived = self.create_folder('Archived', False)['id']
        self.client.patch(f'/api/folders/{archived}', headers=self.headers, json={'archived': True})
        self.assertEqual(self.preview(archived)['counts'], {'availabilitySubmissions': 0, 'weekdaySchedules': 0, 'weekendSchedules': 0})
        self.assertEqual(self.delete_folder(archived).status_code, 200)
        self.assertEqual(self.delete_folder(original).status_code, 200)
        self.assertEqual(self.client.get('/api/folders', headers=self.headers).json['folders'], [])
        web.init_db()  # A restart must not resurrect Imported availability.
        self.assertEqual(self.client.get('/api/folders', headers=self.headers).json['folders'], [])
        self.assertEqual(self.client.get('/api/staffing-plan', headers=self.headers).status_code, 200)
        self.assertEqual(self.create_folder('New semester', False)['name'], 'New semester')
        self.assertIsNone(self.context()['folder'])

    def test_delete_auth_confirmation_missing_and_repeated_requests(self):
        folder = self.context()['folder']['id']
        preview = self.preview(folder)
        url = f'/api/folders/{folder}'
        self.assertEqual(self.client.get(url + '/deletion-preview').status_code, 401)
        self.assertEqual(self.client.delete(url, json={}).status_code, 401)
        for payload in ({}, {'confirmationName': preview['folder']['name']},
                        {'confirmationName': True, 'previewVersion': preview['previewVersion']},
                        {'confirmationName': preview['folder']['name'].lower(), 'previewVersion': preview['previewVersion']},
                        {'confirmationName': preview['folder']['name'], 'previewVersion': '🙂'}):
            self.assertEqual(self.client.delete(url, headers=self.headers, json=payload).status_code, 400)
        self.assertEqual(self.delete_folder(folder, preview).status_code, 200)
        self.assertEqual(self.delete_folder(folder, preview).status_code, 404)
        self.assertEqual(self.client.get(url + '/deletion-preview', headers=self.headers).status_code, 404)
        self.assertEqual(self.client.delete('/api/folders/99999', headers=self.headers, json={}).status_code, 404)

    def test_delete_preview_rejects_changed_counts_content_and_folder_state(self):
        folder = self.context()['folder']['id']
        actions = [lambda: self.submit(comment='New'), lambda: self.submit(comment='Edited, same count'),
                   lambda: self.seed_snapshots(folder),
                   lambda: self.client.patch(f'/api/folders/{folder}', headers=self.headers, json={'archived': True})]
        for action in actions:
            preview = self.preview(folder)
            action()
            self.assertEqual(self.delete_folder(folder, preview).status_code, 409)
        self.assertEqual(self.delete_folder(folder).status_code, 200)

    def test_delete_rollback_restores_all_records_and_active_state(self):
        from peewee import OperationalError
        folder = self.context()['folder']['id']
        self.submit(comment='Must survive')
        self.seed_snapshots(folder)
        preview = self.preview(folder)
        with db.connection_context():
            before = [(model, list(model.select().dicts())) for model in
                      (SubmissionState, Folder, FolderAvailability, SavedSchedule, SavedWeekendSchedule)]
        # Fail early and late: both earlier dependent deletes and revision change roll back.
        for model, method in [(SavedWeekendSchedule, 'delete'), (FolderAvailability, 'delete'), (Folder, 'delete_instance')]:
            with patch.object(model, method, side_effect=OperationalError('Injected deletion failure')):
                self.assertEqual(self.delete_folder(folder, preview).status_code, 503)
            with db.connection_context():
                for table, records in before:
                    self.assertEqual(list(table.select().dicts()), records)
        self.assertEqual(self.delete_folder(folder, preview).status_code, 200)

    def delayed_generation(self, weekend=False, replacement=False):
        folder = self.context()['folder']['id']
        self.submit()
        employee = self.overview(folder)['employees'][0]['id']
        started, release = threading.Event(), threading.Event()
        def solver(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise AssertionError('Solver blocked too long')
            return {'assignments': [], 'effective_pool': [], 'rotating_counts': {}} if weekend else {'status': 'FEASIBLE', 'schedule': {}, 'work': {}}
        module, method = (web.weekend_generator, 'generate_weekend_schedule') if weekend else (web.solver_module, 'generate_schedule')
        payload = {'folderId': folder, 'config': {'start_date': '2026-09-01', 'end_date': '2026-09-07'}} if weekend else {'folderId': folder, 'employeeIds': [employee]}
        def generate():
            with web.app.test_client() as client:
                return client.post('/api/generate_weekend' if weekend else '/api/generate', headers=self.headers, json=payload)
        with patch.object(module, method, side_effect=solver), concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(generate)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.delete_folder(folder).status_code, 200)
                if replacement:
                    self.create_folder('Replacement', False)
            finally:
                release.set()
            self.assertIn(future.result(timeout=10).status_code, (404, 409))
        with db.connection_context():
            self.assertEqual(SavedSchedule.select().count(), 0)
            self.assertEqual(SavedWeekendSchedule.select().count(), 0)

    def test_weekday_generation_finishing_after_deletion(self):
        self.delayed_generation()

    def test_weekend_generation_finishing_after_deletion(self):
        self.delayed_generation(weekend=True)

    def test_generation_rejects_replacement_folder_identity(self):
        self.delayed_generation(replacement=True)

    def test_weekend_save_and_admin_submission_reject_deleted_folder(self):
        folder = self.context()['folder']['id']
        self.submit()
        employee = self.overview(folder)['employees'][0]['id']
        preview = self.client.post('/api/generate_weekend', headers=self.headers, json={'folderId': folder,
            'config': {'start_date': '2026-09-01', 'end_date': '2026-09-07', 'rotating_employees': []}}).json
        self.assertEqual(self.delete_folder(folder).status_code, 200)
        self.assertEqual(self.client.post('/api/save_weekend', headers=self.headers,
            json={'folderId': folder, 'snapshot': preview}).status_code, 404)
        self.assertEqual(self.client.put('/api/admin/availability', headers=self.headers,
            json={'folderId': folder, 'employeeId': employee, 'availability': {}}).status_code, 404)
        replacement = self.create_folder('Replacement', False)['id']
        self.assertEqual(self.client.post('/api/save_weekend', headers=self.headers,
            json={'folderId': replacement, 'snapshot': preview}).status_code, 409)
