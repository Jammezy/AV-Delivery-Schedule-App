"""Regression tests use a disposable database, never the deployed database."""
import datetime
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

TEST_DIR = Path(os.environ.get("TEST_DATA_DIR", tempfile.gettempdir())) / ("schedule-tests-" + uuid.uuid4().hex)
TEST_DIR.mkdir(parents=True)
os.environ.pop("DATABASE_URL", None)
os.environ["DATABASE_PATH"] = str(TEST_DIR / "initial.db")
os.environ["ADMIN_PASSWORD"] = "test-admin"
import app as web
from models import (db, init_db, Employee, Folder, FolderAvailability,
                    SubmissionState, SavedSchedule, Config)
from backup_db import backup


class AppTests(unittest.TestCase):
    def test_staffing_plan_scope_and_saved_changes(self):
        self.assertEqual(self.client.get('/api/staffing-plan').status_code, 401)
        def plan(query=''):
            return self.client.get('/api/staffing-plan' + query, headers=self.headers).json
        empty = plan()
        # Defaults: Mon–Thu 12*4 + 3*2; Fri 12*4 = 264 staff-hours.
        self.assertEqual((empty['requiredHours'], empty['allottedHours'], empty['remainingHours']), (264, 0, 264))
        for minimum, remaining in [(150, 114), (264, 0), (279, -15), (0, 264)]:
            response = self.client.put('/api/employees/Alex', headers=self.headers,
                json={'minHours': minimum, 'maxHours': 300})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(plan()['remainingHours'], remaining)
            self.assertEqual(plan('?folderId=999&selection=1&employeeId=999'), plan())
        self.client.delete('/api/employees/Alex', headers=self.headers)
        self.assertEqual(plan(), empty)

    def test_staffing_plan_closing_rules_and_validation(self):
        from models import save_config
        with db.connection_context():
            _, errors = save_config({'hourStart': 7, 'hourEnd': 10,
                'lateHourStart': 9, 'reqStaffOpen': 4, 'reqStaffLate': 2,
                'fridayCloseHour': 9, 'dayCloseHours': {'Mon': 8},
                'minShiftLength': 1, 'maxShiftLength': 4})
            self.assertFalse(errors)
        result = self.client.get('/api/staffing-plan', headers=self.headers).json
        # Monday 4, Tue–Thu 12 each, Friday 8; hour 10 is included.
        self.assertEqual(result['requiredHours'], 48)
        for value in [-1, 1.5, '1.5', '', None, True, 'bad']:
            response = self.client.put('/api/employees/Invalid', headers=self.headers,
                json={'minHours': value, 'maxHours': 40})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/employees', headers=self.headers).json, [])
        response = self.client.put('/api/employees/Zero', headers=self.headers,
            json={'minHours': '0', 'maxHours': '0'})
        self.assertEqual(response.json['maxHours'], 0)

    def setUp(self):
        db.close()
        self.path = TEST_DIR / (self._testMethodName + ".db")
        db.init(str(self.path))
        init_db()
        self.client = web.app.test_client()
        token = self.client.post("/api/admin/login", json={"password": "test-admin"}).json["token"]
        self.headers = {"Authorization": "Bearer " + token}

    def tearDown(self):
        if not db.is_closed(): db.close()

    def context(self):
        return self.client.get("/api/submission-context").json

    def submit(self, name="Alex", availability=None, comment="", context=None):
        ctx = context or self.context()
        return self.client.post("/api/availability", json={"name": name,
            "availability": {"Mon_07": True, "Sat_09": True} if availability is None else availability,
            "comment": comment, "folderId": ctx["folder"]["id"], "revision": ctx["revision"]})

    def create_folder(self, name, activate=True):
        result = self.client.post("/api/folders", headers=self.headers, json={"name": name, "activate": activate})
        self.assertEqual(result.status_code, 201)
        return result.json

    def overview(self, folder_id):
        return self.client.get(f"/api/availability?folderId={folder_id}", headers=self.headers).json

    def test_logout_revokes_server_token_and_protected_routes(self):
        self.assertEqual(self.client.get("/api/folders").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/logout", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/employees", headers=self.headers).status_code, 401)
        self.assertEqual(self.client.post("/api/generate", headers=self.headers, json={}).status_code, 401)

    def test_folders_isolate_updates_and_reject_stale_forms(self):
        spring = self.context()
        self.assertEqual(self.submit(context=spring, comment="Spring").status_code, 200)
        summer = self.create_folder("Summer")
        self.assertEqual(self.submit(context=spring).status_code, 409)
        self.assertEqual(self.submit(availability={"Sun_10": True}, comment="Summer").status_code, 200)
        a = self.overview(spring["folder"]["id"])["submissions"][0]
        b = self.overview(summer["id"])["submissions"][0]
        self.assertEqual(a["employeeId"], b["employeeId"])
        self.assertEqual(a["comment"], "Spring")
        self.assertEqual(b["availability"], {"Sun_10": True})
        self.client.patch(f'/api/folders/{spring["folder"]["id"]}', headers=self.headers, json={"activate": True})
        self.assertEqual(self.submit(context=spring).status_code, 409)  # A→B→A also stale

    def test_viewing_and_inactive_creation_do_not_change_active_folder(self):
        original = self.context()["folder"]["id"]
        other = self.create_folder("Review only", False)
        self.overview(other["id"])
        self.assertEqual(self.context()["folder"]["id"], original)

    def test_archive_closes_submission_and_keeps_data(self):
        self.submit()
        ctx = self.context()
        folder_id = ctx["folder"]["id"]
        self.client.patch(f"/api/folders/{folder_id}", headers=self.headers, json={"archived": True})
        self.assertIsNone(self.context()["folder"])
        self.assertEqual(self.submit(context=ctx).status_code, 409)
        self.assertEqual(len(self.overview(folder_id)["submissions"]), 1)
        self.assertEqual(self.client.patch(f"/api/folders/{folder_id}", headers=self.headers, json={"activate": True}).status_code, 409)

    def test_comments_weekends_and_empty_availability(self):
        self.assertEqual(self.submit(comment="🙂" * 99).status_code, 200)
        self.assertEqual(self.submit(comment="a" * 100).status_code, 400)
        ctx = self.context()
        row = self.client.get(f'/api/availability/Alex?folderId={ctx["folder"]["id"]}').json
        self.assertEqual(row["comment"], "🙂" * 99)
        self.assertTrue(row["availability"]["Sat_09"])
        self.assertEqual(self.submit(availability={}).status_code, 200)
        data = self.overview(ctx["folder"]["id"])
        self.assertEqual(data["missing"], [])
        self.assertEqual(data["submissions"][0]["availability"], {})
        self.assertEqual(self.submit(availability={"NotADay_99": True}).status_code, 400)

    def test_database_failure_is_not_reported_as_success(self):
        from peewee import OperationalError
        with patch.object(FolderAvailability, "save", side_effect=OperationalError("disk full")):
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(len(self.overview(self.context()["folder"]["id"])["submissions"]), 0)

    def test_generation_selection_and_immutable_saved_snapshot(self):
        self.submit("Alex", comment="Original")
        self.submit("Blair")
        folder_id = self.context()["folder"]["id"]
        data = self.overview(folder_id)
        chosen = data["submissions"][0]["employeeId"]
        fake_result = {"status":"FEASIBLE", "work":{"Mon":{"7":["Alex"]}}, "schedule":{"Mon":{"7":{"DLA":"Alex"}}}, "weeklyHours":{"Alex":1}}
        with patch.object(web.solver_module, "generate_schedule", return_value=fake_result) as solver:
            res = self.client.post("/api/generate", headers=self.headers, json={"folderId":folder_id, "employeeIds":[chosen]})
            self.assertEqual(res.status_code, 200)
            roster, availability, cfg = solver.call_args.args
            self.assertEqual(len(roster), 1)
            self.assertEqual(cfg["days"], ["Mon", "Tue", "Wed", "Thu", "Fri"])
            schedule_id = res.json["savedScheduleId"]
        self.assertEqual(len(self.overview(folder_id)["submissions"]), 2)
        self.submit("Alex", availability={}, comment="Changed")
        self.client.put("/api/config", headers=self.headers, json={"maxShiftLength": 7})
        url = f"/api/folders/{folder_id}/schedules/{schedule_id}"
        snapshot = self.client.get(url, headers=self.headers).json
        self.assertEqual(snapshot["submissions"][0]["comment"], "Original")
        self.assertEqual(snapshot["result"]["config"]["maxShiftLength"], 6)
        self.assertEqual(self.client.delete(url, headers=self.headers, json={}).status_code, 400)
        self.assertEqual(self.client.delete(url, headers=self.headers, json={"confirm":True}).status_code, 200)
        self.assertEqual(len(self.overview(folder_id)["submissions"]), 2)
        self.assertEqual(self.client.post("/api/generate", headers=self.headers, json={"folderId":folder_id,"employeeIds":[]}).status_code, 400)

    def test_real_solver_weekends_do_not_change_weekday_result(self):
        cfg = web.get_config()
        cfg.update(hourStart=7, hourEnd=7, lateHourStart=7, reqStaffOpen=1,
                   reqStaffLate=1, minShiftLength=1, maxShiftLength=1,
                   maxMorningShifts=5, maxEveningShifts=5, maxMorningPlusEvening=10,
                   blockClopening=False, requireLeadDuringOpen=False)
        roster = [{"name":"Alex", "isLead":False,"minHours":5,"maxHours":5}]
        weekday = {f"{d}_07":True for d in ["Mon", "Tue", "Wed", "Thu", "Fri"]}
        one = web.solver_module.generate_schedule(roster, {"Alex":weekday}, cfg, seed=1)
        two = web.solver_module.generate_schedule(roster, {"Alex":{**weekday,"Sat_07":True,"Sun_07":True}}, cfg, seed=1)
        self.assertEqual(one["status"], "OPTIMAL")
        self.assertEqual(one["schedule"], two["schedule"])
        self.assertEqual(two["weeklyHours"]["Alex"], 5)
        self.assertEqual(set(two["work"]), set(["Mon", "Tue", "Wed", "Thu", "Fri"]))

    def test_restart_old_submissions_and_backup_restore(self):
        self.submit(comment="Keep me")
        db.connect(reuse_if_open=True)
        FolderAvailability.update(submitted_at=datetime.datetime.utcnow()-datetime.timedelta(days=30)).execute()
        db.close()
        env = dict(os.environ, DATABASE_PATH=str(self.path))
        code = "from app import app; c=app.test_client(); x=c.get('/api/submission-context').json; r=c.get('/api/availability/Alex?folderId='+str(x['folder']['id'])); assert r.json['comment']=='Keep me'; print('restart persisted')"
        result = subprocess.run([sys.executable,"-c",code], env=env, cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        backup_path = TEST_DIR / "backup.db"
        backup(self.path, backup_path)
        with sqlite3.connect(backup_path) as restored:
            self.assertEqual(restored.execute("select comment from folderavailability").fetchone()[0], "Keep me")



    def test_weekend_generation_and_save(self):
        ctx = self.context()
        folder_id = ctx["folder"]["id"]

        # Submit some people
        self.submit("W1", {"Sat_07": 1, "Sat_08": 1, "Sat_09": 1, "Sat_10": 1, "Sat_11": 1})
        self.submit("W2", {"Sat_12": 1, "Sat_13": 1, "Sat_14": 1, "Sat_15": 1, "Sat_16": 1})
        self.submit("W3", {}) # empty availability

        # Generate preview
        config = {
            "start_date": "2024-09-02",
            "end_date": "2024-09-08",
            "excluded_dates": [],
            "fixed_assignments": {},
            "rotating_employees": [1, 2, 3],
            "shift_starting_person": True
        }

        roster_res = self.client.get("/api/employees", headers=self.headers).json
        w1_id = next(e["id"] for e in roster_res if e["name"] == "W1")
        w2_id = next(e["id"] for e in roster_res if e["name"] == "W2")
        config["rotating_employees"] = [w1_id, w2_id]

        res = self.client.post("/api/generate_weekend", json={"folderId": folder_id, "config": config}, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json
        self.assertEqual(data["status"], "SUCCESS")

        assignments = data["assignments"]
        self.assertTrue(len(assignments) > 0)

        saturday_morning = next(a for a in assignments if a["shift"]["key"] == "saturday_morning")
        self.assertEqual(saturday_morning["assigned"], w1_id)

        res2 = self.client.post("/api/save_weekend", json={"folderId": folder_id, "snapshot": data}, headers=self.headers)
        self.assertEqual(res2.status_code, 200)
        saved_id = res2.json["savedScheduleId"]

        res3 = self.client.get(f"/api/folders/{folder_id}/weekend_schedules", headers=self.headers)
        self.assertEqual(res3.status_code, 200)
        self.assertEqual(len(res3.json), 1)
        self.assertEqual(res3.json[0]["id"], saved_id)

        res4 = self.client.get(f"/api/folders/{folder_id}/weekend_schedules/{saved_id}", headers=self.headers)
        self.assertEqual(res4.status_code, 200)
        self.assertEqual(res4.json["status"], "SUCCESS")

        res5 = self.client.delete(f"/api/folders/{folder_id}/weekend_schedules/{saved_id}", headers=self.headers)
        self.assertEqual(res5.status_code, 400)
        res6 = self.client.delete(f"/api/folders/{folder_id}/weekend_schedules/{saved_id}", json={"confirm": True}, headers=self.headers)
        self.assertEqual(res6.status_code, 200)

class MigrationTests(unittest.TestCase):
    def test_legacy_import_preserves_settings_and_is_idempotent(self):
        path = TEST_DIR / "legacy.db"
        with sqlite3.connect(path) as legacy:
            legacy.executescript('''CREATE TABLE employee (id INTEGER PRIMARY KEY, name TEXT UNIQUE, is_lead INTEGER, min_hours INTEGER, max_hours INTEGER);
            CREATE TABLE availability (id INTEGER PRIMARY KEY, employee_name TEXT UNIQUE, data_json TEXT, submitted_at DATETIME);
            CREATE TABLE config (id INTEGER PRIMARY KEY, data_json TEXT);
            INSERT INTO employee VALUES (12, 'Legacy Alex', 1, 3, 20);
            INSERT INTO availability VALUES (1, 'Legacy Alex', '{"Mon_07": true}', '2026-01-01 00:00:00');
            INSERT INTO config VALUES (1, '{"maxShiftLength": 8}');''')
        db.close(); db.init(str(path)); init_db(); init_db()
        self.assertEqual(Folder.select().count(), 1)
        self.assertEqual(FolderAvailability.select().count(), 1)
        row = FolderAvailability.get()
        self.assertEqual(row.employee_id, 12)
        self.assertEqual(row.folder.name, "Imported availability")
        self.assertEqual(row.submitted_at.year, 2026)
        self.assertEqual(json.loads(Config.get_by_id(1).data_json)["maxShiftLength"], 8)
        self.assertEqual(row.get_data(), {"Mon_07":True})
        db.close()



if __name__ == "__main__":
    unittest.main()
