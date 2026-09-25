# ============================================================
# app.py - Flask backend
# ============================================================

import os
import re
import json
import secrets
import datetime
import hashlib
from functools import wraps
from collections import defaultdict

from flask import Flask, request, jsonify, send_from_directory, abort
from peewee import DatabaseError

from models import (
    db, init_db, Employee, Availability, get_config, save_config,
    normalize_level, Folder, SubmissionState, FolderAvailability,
    SavedSchedule, AdminSession, write_transaction,
)
import solver as solver_module

app = Flask(__name__, static_folder="public", static_url_path="")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
if ADMIN_PASSWORD == "admin123":
    print("\n*** Using the default admin password 'admin123'. Set ADMIN_PASSWORD "
          "before anyone else can reach this site. ***\n")

TOKEN_TTL = datetime.timedelta(hours=8)
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = datetime.timedelta(minutes=10)
KEY_PATTERN = re.compile(r"^[A-Za-z]{2,4}_\d{2}$")

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
_login_attempts = defaultdict(list)


def now():
    return datetime.datetime.utcnow()


def clean_name(raw):
    return re.sub(r"\s+", " ", (raw or "")).strip()


def resolve_name(raw):
    """Match what was typed against the roster ignoring case, and return the
    roster's spelling. Without this, 'avery', 'Avery' and 'AVERY' each become a
    separate person, and all three land in the solver."""
    name = clean_name(raw)
    if not name:
        return ""
    exact = Employee.get_or_none(Employee.name == name)
    if exact:
        return exact.name
    folded = name.casefold()
    for e in Employee.select():
        if e.name.casefold() == folded:
            return e.name
    return name


# --- database connection per request -------------------------------------
@app.before_request
def _open_db():
    if db.is_closed():
        db.connect(reuse_if_open=True)


@app.teardown_request
def _close_db(_exc):
    if not db.is_closed():
        db.close()


def token_hash():
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    return hashlib.sha256(token.encode()).hexdigest()


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        session = AdminSession.get_or_none(AdminSession.token_hash == token_hash())
        if not session or session.expires_at <= datetime.datetime.utcnow():
            return jsonify(error="Not authenticated. Please log in again."), 401
        return fn(*args, **kwargs)
    return wrapper


@app.post("/api/admin/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    cutoff = now() - LOGIN_WINDOW
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    if len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS:
        return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429

    data = request.get_json(silent=True) or {}
    if not secrets.compare_digest(str(data.get("password") or ""), ADMIN_PASSWORD):
        _login_attempts[ip].append(now())
        return jsonify({"error": "Incorrect password"}), 401

    AdminSession.delete().where(AdminSession.expires_at <= now()).execute()
    token = secrets.token_hex(24)
    AdminSession.create(token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=now() + TOKEN_TTL)
    return jsonify({"token": token})


@app.post("/api/admin/logout")
def logout():
    AdminSession.delete().where(AdminSession.token_hash == token_hash()).execute()
    return jsonify(ok=True)


@app.after_request
def no_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(DatabaseError)
def database_error(error):
    app.logger.exception("Database operation failed")
    return jsonify(error="Unable to save or load data. Your entries have not been cleared; please retry."), 503


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
def request_error(error):
    return jsonify(error=error.description), error.code


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "A JSON object is required.")
    return data


def folder_or_404(folder_id):
    folder = Folder.get_or_none(Folder.id == folder_id)
    if not folder:
        abort(404, "Folder not found.")
    return folder


def folder_json(f):
    return {"id": f.id, "name": f.name, "archived": f.archived}


def submission_json(row):
    return {"employeeId": row.employee_id, "availability": row.get_data(),
            "comment": row.comment, "submittedAt": row.submitted_at.isoformat() + "Z"}



# ---------------- config ----------------
@app.get("/api/config")
def get_config_route():
    return jsonify(get_config())


@app.put("/api/config")
@require_admin
def put_config():
    data = body()
    data["days"] = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    data.pop("availabilityDays", None)
    updated, errors = save_config(data)
    if errors:
        return jsonify({"errors": errors, "config": updated}), 400
    return jsonify(updated)


# ---------------- employees ----------------
def serialize(e):
    return {"id": e.id, "name": e.name, "isLead": e.is_lead,
            "minHours": e.min_hours, "maxHours": e.max_hours}


@app.get("/api/roster")
def public_roster():
    """Names only, so the availability form can offer autocomplete
    instead of trusting free text and creating duplicate people."""
    cfg = get_config()
    return jsonify({
        "names": sorted(e.name for e in Employee.select()),
        "allowSelfRegister": bool(cfg.get("allowSelfRegister", True)),
    })


@app.get("/api/employees")
@require_admin
def list_employees():
    return jsonify([serialize(e) for e in Employee.select().order_by(Employee.name)])


@app.put("/api/employees/<path:name>")
@require_admin
def upsert_employee(name):
    name = resolve_name(name)
    if not name:
        return jsonify({"error": "A name is required."}), 400
    data = request.get_json(silent=True) or {}
    emp, _ = Employee.get_or_create(name=name)
    emp.is_lead = bool(data.get("isLead"))
    emp.min_hours = max(0, int(data.get("minHours") or 0))
    emp.max_hours = max(0, int(data.get("maxHours") or 40))
    if emp.min_hours > emp.max_hours:
        return jsonify({"error": "Minimum hours can't exceed maximum hours."}), 400
    emp.save()
    return jsonify(serialize(emp))


@app.delete("/api/employees/<path:name>")
@require_admin
def delete_employee(name):
    name = resolve_name(name)
    employee = Employee.get_or_none(Employee.name == name)
    if employee and FolderAvailability.select().where(FolderAvailability.employee == employee).exists():
        abort(409, "This employee has saved submissions. Exclude them from generation to keep their history.")
    with write_transaction():
        Employee.delete().where(Employee.name == name).execute()
        Availability.delete().where(Availability.employee_name == name).execute()
    return jsonify({"ok": True})


@app.get("/api/submission-context")
def submission_context():
    state = SubmissionState.get_by_id(1)
    return jsonify(folder=folder_json(state.active_folder) if state.active_folder_id else None,
                   revision=state.revision, config=get_config())


@app.get("/api/folders")
@require_admin
def folders():
    state = SubmissionState.get_by_id(1)
    return jsonify(folders=[folder_json(f) for f in Folder.select().order_by(Folder.id.desc())],
                   activeFolderId=state.active_folder_id)


@app.post("/api/folders")
@require_admin
def create_folder():
    data = body()
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        abort(400, "Enter a folder name of 1–100 characters.")
    with write_transaction():
        folder = Folder.create(name=name.strip())
        if data.get("activate") is True:
            SubmissionState.update(active_folder=folder, revision=SubmissionState.revision + 1).where(SubmissionState.id == 1).execute()
    return jsonify(folder_json(folder)), 201


@app.patch("/api/folders/<int:folder_id>")
@require_admin
def edit_folder(folder_id):
    data = body()
    with write_transaction():
        folder = folder_or_404(folder_id)
        state = SubmissionState.get_by_id(1)
        if "name" in data:
            if not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 100:
                abort(400, "Enter a folder name of 1–100 characters.")
            folder.name = data["name"].strip()
        if "archived" in data:
            if type(data["archived"]) is not bool:
                abort(400, "Invalid archive value.")
            folder.archived = data["archived"]
        if data.get("activate") is True:
            if folder.archived:
                abort(409, "Restore this folder before accepting submissions.")
            state.active_folder = folder
            state.revision += 1
        elif state.active_folder_id == folder.id and (folder.archived or data.get("activate") is False):
            state.active_folder = None
            state.revision += 1
        elif state.active_folder_id == folder.id and "name" in data:
            state.revision += 1
        folder.save()
        state.save()
    return jsonify(folder_json(folder))


@app.post("/api/availability")
def submit_availability():
    data = body()
    name, comment, availability = data.get("name"), data.get("comment", ""), data.get("availability")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        abort(400, "Enter a name of 1–100 characters.")
    if not isinstance(comment, str) or len(comment) > 99:
        abort(400, "Comments must be shorter than 100 characters.")
    if not isinstance(availability, dict):
        abort(400, "Availability must be an object.")
    with write_transaction():
        state = SubmissionState.get_by_id(1)
        if not state.active_folder_id:
            abort(409, "No folder is accepting submissions.")
        if data.get("folderId") != state.active_folder_id or data.get("revision") != state.revision:
            abort(409, "The submission folder changed. Refresh the form and confirm the current folder before submitting.")
        cfg = get_config()
        valid = {f"{d}_{h:02d}" for d in cfg['availabilityDays'] for h in range(cfg["hourStart"], cfg["hourEnd"] + 1)}
        if any(k not in valid or (type(v) not in (int, bool) or v not in (0, 1, 2)) for k, v in availability.items()):
            abort(400, "Invalid availability time slot. Refresh the form and try again.")
        name = resolve_name(name)
        employee = Employee.get_or_none(Employee.name == name)
        if employee is None:
            if not cfg.get("allowSelfRegister", True):
                abort(400, "That name is not on the roster. Ask your supervisor to add you.")
            employee = Employee.create(name=name)
        row, _ = FolderAvailability.get_or_create(employee=employee, folder=state.active_folder_id)
        row.data_json = json.dumps(availability)
        row.comment = comment
        row.submitted_at = datetime.datetime.utcnow()
        row.save()
    available = sum(normalize_level(v) > 0 for v in availability.values())
    weekday_hours = sum(normalize_level(v) > 0 for k, v in availability.items() if k.split("_")[0] in cfg["days"])
    return jsonify(ok=True, name=name, submittedAt=row.submitted_at.isoformat() + "Z",
        availableHours=available, preferredHours=sum(normalize_level(v) == 2 for v in availability.values()),
        minHours=employee.min_hours, maxHours=employee.max_hours,
        shortOfMinimum=max(0, employee.min_hours - weekday_hours))


@app.get("/api/availability/<path:name>")
def get_one_availability(name):
    # Name-based access is legacy behavior, NOT employee authentication.
    state = SubmissionState.get_by_id(1)
    if not state.active_folder_id or request.args.get("folderId", type=int) != state.active_folder_id:
        abort(409, "The submission folder changed. Refresh and confirm the current folder.")
    employee = Employee.get_or_none(Employee.name == resolve_name(name))
    row = FolderAvailability.get_or_none((FolderAvailability.employee == employee.id) &
          (FolderAvailability.folder == state.active_folder_id)) if employee else None
    result = submission_json(row) if row else {}
    result.update(found=row is not None, employee=serialize(employee) if employee else None)
    return jsonify(result)



@app.get("/api/availability")
@require_admin
def get_all_availability():
    folder = folder_or_404(request.args.get("folderId", type=int))
    employees = [serialize(e) for e in Employee.select().order_by(Employee.name)]
    rows = list(FolderAvailability.select().where(FolderAvailability.folder == folder))
    availability = {r.employee.name: r.get_data() for r in rows}
    return jsonify(folder=folder_json(folder), employees=employees, availability=availability,
        submittedAt={r.employee.name: r.submitted_at.isoformat() + "Z" for r in rows},
        comments={r.employee.name: r.comment for r in rows},
        submissions=[submission_json(r) for r in rows],
        missing=[e["name"] for e in employees if e["name"] not in availability])

# ---------------- diagnostics ----------------
def _load_inputs(folder_id, ids=None):
    folder_or_404(folder_id)
    cfg = get_config()
    rows = FolderAvailability.select().where(FolderAvailability.folder == folder_id)
    if ids is not None:
        rows = rows.where(FolderAvailability.employee.in_(ids))
    rows = list(rows)
    if ids is not None and {r.employee_id for r in rows} != set(ids):
        abort(400, "Every selected employee must have a submission in this folder.")
    return cfg, [serialize(r.employee) for r in rows], {r.employee.name: r.get_data() for r in rows}, rows

@app.get("/api/diagnostics")
@require_admin
def diagnostics():
    """Everything blocking or squeezing this week, without running the solver."""
    ids = request.args.getlist("employeeId", type=int) if "selection" in request.args else None
    cfg, employees, availability, _ = _load_inputs(request.args.get("folderId", type=int), ids)
    return jsonify({
        "config": cfg,
        "diagnostics": solver_module.analyze(employees, availability, cfg),
        "burdenMap": {
            d: {h: w for (dd, h), w in
                solver_module.burden_weights(employees, availability, cfg).items()
                if dd == d}
            for d in cfg["days"]
        },
    })


# ---------------- schedule generation ----------------
@app.post("/api/generate")
@require_admin
def generate():
    data = body()
    ids = data.get("employeeIds")
    if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)):
        abort(400, "Select at least one employee with submitted availability.")
    with write_transaction():
        folder = folder_or_404(data.get("folderId"))
        rows = list(FolderAvailability.select().where((FolderAvailability.folder == folder) & (FolderAvailability.employee.in_(ids))))
        if {r.employee_id for r in rows} != set(ids):
            abort(400, "Every selected employee must have a submission in this folder.")
        roster = [serialize(r.employee) for r in rows]
        availability = {r.employee.name: r.get_data() for r in rows}
        cfg = get_config()
        submissions = [submission_json(r) for r in rows]
    seed = data.get("seed")
    if seed is not None and (type(seed) is not int or not 0 <= seed < 2**31):
        abort(400, "Invalid generation seed.")
    result = solver_module.generate_schedule(roster, availability, cfg, seed=seed)
    if result["status"] not in ("OPTIMAL", "FEASIBLE"):
        return jsonify(result)
    result.update(employees=roster, config=cfg)
    snapshot = {"result": result, "submissions": submissions, "employeeIds": ids,
                "folder": folder_json(folder)}
    saved = SavedSchedule.create(folder=folder, snapshot_json=json.dumps(snapshot))
    result["savedScheduleId"] = saved.id
    return jsonify(result)


@app.get("/api/folders/<int:folder_id>/schedules")
@require_admin
def schedules(folder_id):
    folder_or_404(folder_id)
    return jsonify([{"id": s.id, "createdAt": s.created_at.isoformat() + "Z"} for s in
                    SavedSchedule.select().where(SavedSchedule.folder == folder_id).order_by(SavedSchedule.id.desc())])


@app.route("/api/folders/<int:folder_id>/schedules/<int:schedule_id>", methods=["GET", "DELETE"])
@require_admin
def saved_schedule(folder_id, schedule_id):
    saved = SavedSchedule.get_or_none((SavedSchedule.id == schedule_id) & (SavedSchedule.folder == folder_id))
    if not saved:
        abort(404, "Saved schedule not found.")
    if request.method == "DELETE":
        if body().get("confirm") is not True:
            abort(400, "Confirm deletion of this saved schedule.")
        saved.delete_instance()
        return jsonify(ok=True)
    return jsonify(json.loads(saved.snapshot_json))


# ---------------- static frontend ----------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# Runs on import so gunicorn / PythonAnywhere / any WSGI server gets a
# working database. The old version only did this under __main__, which
# meant the first request on a real deployment died with "no such table".
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
