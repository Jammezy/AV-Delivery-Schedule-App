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
    SavedSchedule, SavedWeekendSchedule, GenerationJob, AdminSession, write_transaction,
    IntakeSubmission, CollectionCode, CollectionSettings, EditGrant, FolderEmployee, FolderConfig,
    folder_members,
)
import solver as solver_module
import weekend_generator
import generation_jobs
from boundary import boundary_context, consent_status
from request_retention import RequestRetention

app = Flask(__name__, static_folder="public", static_url_path="")
app.logger.setLevel('INFO')
app.config['REQUEST_RETENTION_ENABLED'] = os.environ.get('REQUEST_RETENTION_ENABLED', 'true') == 'true'
app.config['GENERATION_RUNNER_ENABLED'] = True
request_retention = RequestRetention()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD or ADMIN_PASSWORD == "admin123":
    raise RuntimeError("Set a unique ADMIN_PASSWORD before starting the app.")

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
    if request.path == '/healthz' or not request.path.startswith('/api/'):
        return
    if db.is_closed():
        db.connect(reuse_if_open=True)
    if request.path.startswith('/api/') and app.config['REQUEST_RETENTION_ENABLED']:
        request_retention.maybe_run(app.logger)


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
    ip = request.remote_addr or "?"
    cutoff = now() - LOGIN_WINDOW
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    if len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS:
        return jsonify({"error": "Too many attempts. Wait 15 minutes."}), 429

    data = body()
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
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.errorhandler(DatabaseError)
def database_error(error):
    app.logger.error("Database operation failed (%s)", type(error).__name__)
    return jsonify(error="Unable to save or load data. Your entries have not been cleared; please retry."), 503


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(429)
@app.errorhandler(503)
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


def recorded_consent(row):
    return {'allowExtraOpenings': row.allow_extra_openings,
            'allowExtraClosings': row.allow_extra_closings, 'consentContext': row.consent_context}


def freeze_consent(cfg, rows, folder_id):
    cfg['boundaryConsentFolderId'] = folder_id
    cfg['boundaryConsents'] = {str(r.employee_id): recorded_consent(r) for r in rows}


def submission_json(row, cfg=None):
    return {"employeeId": row.employee_id, "availability": row.get_data(),
            'permissionVersion': permission_version(row),
            'consent': consent_status(json.loads(row.data_json), recorded_consent(row), cfg or get_config(row.folder_id)),
            "comment": row.comment, "submittedAt": row.submitted_at.isoformat() + "Z"}


def permission_version(row):
    # Include the availability and agreement as well as stable record identities:
    # concurrent employee/admin edits and reused folder IDs cannot overwrite a draft.
    value = [row.employee_id, row.folder_id, row.folder.created_at.isoformat(), row.save_version,
             row.data_json, row.comment, row.submitted_at.isoformat(), recorded_consent(row)]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@app.put('/api/admin/boundary-permissions')
@require_admin
def update_boundary_permissions():
    data = body()
    if any(k in data for k in ('availability', 'comment')):
        abort(400, 'Save availability and comments through the availability editor.')
    if any(type(data.get(k)) is not int for k in ('employeeId', 'folderId')):
        abort(400, 'Employee and folder IDs are required.')
    if any(type(data.get(k)) is not bool for k in ('allowExtraOpenings', 'allowExtraClosings')):
        abort(400, 'Both permission choices must be explicit booleans.')
    with write_transaction():
        folder = folder_or_404(data['folderId'])
        require_member(folder.id, data['employeeId'])
        row = FolderAvailability.get_or_none((FolderAvailability.folder == folder) &
            (FolderAvailability.employee == data['employeeId']))
        if row is None:
            abort(404, 'No submission for this employee in this folder.')
        cfg = get_config(folder.id)
        if data.get('permissionVersion') != permission_version(row):
            abort(409, 'This employee submission changed. Reload permissions before saving.')
        if data.get('consentContext') != boundary_context(cfg)['token']:
            abort(409, 'Caps or opening/closing boundaries changed. Reload permissions before saving.')
        row.allow_extra_openings = data['allowExtraOpenings']
        row.allow_extra_closings = data['allowExtraClosings']
        row.consent_context = boundary_context(cfg)['token']
        row.save_version += 1
        row.save(only=[FolderAvailability.allow_extra_openings, FolderAvailability.allow_extra_closings,
                       FolderAvailability.consent_context, FolderAvailability.save_version])
    return jsonify(submission=submission_json(row, cfg))



# ---------------- folder settings and roster ----------------
def requested_folder(folder_id=None):
    value = folder_id if folder_id is not None else request.args.get('folderId', type=int)
    if type(value) is not int:
        abort(400, 'Choose a folder.')
    return folder_or_404(value)


def require_member(folder_id, employee_id):
    member = FolderEmployee.get_or_none((FolderEmployee.folder == folder_id) &
        (FolderEmployee.employee == employee_id) & FolderEmployee.active)
    if member is None:
        abort(404, 'Employee does not belong to this folder.')
    return member


def serialize(e, folder_id=None):
    member = e if isinstance(e, FolderEmployee) else require_member(folder_id, e.id)
    employee = member.employee
    return dict(id=employee.id, name=employee.name, isLead=member.is_lead,
                minHours=member.min_hours, maxHours=member.max_hours)


@app.get('/api/config')
@app.get('/api/folders/<int:folder_id>/config')
@require_admin
def get_config_route(folder_id=None):
    folder = requested_folder(folder_id)
    return jsonify(get_config(folder.id))


@app.put('/api/config')
@app.put('/api/folders/<int:folder_id>/config')
@require_admin
def put_config(folder_id=None):
    folder = requested_folder(folder_id)
    data = body()
    data['days'] = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    for key in ('availabilityDays', 'boundaryConsents', 'boundaryConsentFolderId', 'weekendChoices'):
        data.pop(key, None)
    with write_transaction():
        updated, errors = save_config(data, folder.id)
    if errors:
        return jsonify(errors=errors, config=updated), 400
    return jsonify(updated)


@app.put('/api/folders/<int:folder_id>/weekend-choices')
@require_admin
def save_weekend_choices(folder_id):
    data = body()
    def date_value(value, optional=False):
        if optional and value == '':
            return value
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            abort(400, 'Choose valid weekend dates.')
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            abort(400, 'Choose valid weekend dates.')
        return value

    start = date_value(data.get('start_date', ''), optional=True)
    end = date_value(data.get('end_date', ''), optional=True)
    if start and end and start > end:
        abort(400, 'End date must be on or after start date.')
    exclusions = data.get('excluded_dates', [])
    fixed = data.get('fixed_assignments', {})
    order = data.get('rotating_employees', [])
    shifting = data.get('shift_starting_person', True)
    if not isinstance(exclusions, list) or not isinstance(fixed, dict) or not isinstance(order, list) or type(shifting) is not bool:
        abort(400, 'Invalid weekend choices.')
    cleaned_exclusions = []
    for item in exclusions:
        if not isinstance(item, dict) or not isinstance(item.get('label', ''), str) or len(item.get('label', '')) > 200:
            abort(400, 'Use an excluded date with a note of up to 200 characters.')
        cleaned_exclusions.append({'date': date_value(item.get('date')), 'label': item.get('label', '')})
    if any(key not in {s['key'] for s in weekend_generator.SHIFTS} for key in fixed):
        abort(400, 'Choose a valid fixed weekend shift.')
    selected = list(fixed.values()) + order
    if any(type(eid) is not int for eid in selected) or len(set(order)) != len(order):
        abort(400, 'Use employee IDs and a rotation order without duplicates.')
    choices = dict(start_date=start, end_date=end, excluded_dates=cleaned_exclusions,
                   fixed_assignments=fixed, rotating_employees=order, shift_starting_person=shifting)
    with write_transaction():
        folder_or_404(folder_id)
        ids = {m.employee_id for m in folder_members(folder_id)}
        if any(eid not in ids for eid in selected):
            abort(409, 'The roster changed. Refresh this folder before saving weekend choices.')
        cfg = get_config(folder_id)
        cfg['weekendChoices'] = choices
        row, _ = FolderConfig.get_or_create(folder=folder_id, defaults={'data_json': '{}'})
        row.data_json = json.dumps(cfg)
        row.save()
    return jsonify(choices)


@app.get('/api/roster')
@require_admin
def supervisor_roster():
    folder = requested_folder()
    return jsonify(names=[m.employee.name for m in folder_members(folder.id)],
                   allowSelfRegister=bool(get_config(folder.id).get('allowSelfRegister', True)))


@app.get('/api/employees')
@app.get('/api/folders/<int:folder_id>/employees')
@require_admin
def list_employees(folder_id=None):
    folder = requested_folder(folder_id)
    return jsonify([serialize(m) for m in folder_members(folder.id)])


@app.get('/api/staffing-plan')
@app.get('/api/folders/<int:folder_id>/staffing-plan')
@require_admin
def staffing_plan(folder_id=None):
    folder = requested_folder(folder_id)
    cfg = get_config(folder.id)
    employees = [serialize(m) for m in folder_members(folder.id)]
    required = sum(solver_module.required_staff(day, hour, cfg)
                   for day in cfg['days'] for hour in solver_module.hours_of(cfg))
    allotted = sum(e['minHours'] for e in employees)
    return jsonify(folderId=folder.id, requiredHours=required, allottedHours=allotted,
                   remainingHours=required - allotted, employees=employees)


def employee_values(data):
    values = []
    for key, default in (('minHours', 0), ('maxHours', 40)):
        raw = data.get(key, default)
        if type(raw) is int and raw >= 0:
            values.append(raw)
        elif isinstance(raw, str) and re.fullmatch(r'[0-9]+', raw):
            values.append(int(raw))
        else:
            abort(400, 'Hours must be nonnegative whole numbers.')
    if values[0] > values[1]:
        abort(400, "Minimum hours can't exceed maximum hours.")
    return values


@app.get('/api/employees/unassigned')
@require_admin
def unassigned_employees():
    abort(410, 'Employee imports are no longer available. Employees join a folder by submitting availability.')


@app.post('/api/folders/<int:folder_id>/employees')
@require_admin
def add_folder_employee(folder_id):
    requested_folder(folder_id)
    abort(410, 'Adding employees manually is no longer available. Employees join this folder by submitting availability.')


@app.put('/api/folders/<int:folder_id>/employees')
@require_admin
def update_all_folder_employees(folder_id):
    entries = body().get('employees')
    if not isinstance(entries, list):
        abort(400, 'Provide the employee roster.')
    validated = {}
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get('id')) is not int or entry['id'] in validated:
            abort(400, 'Provide each employee once with a valid ID.')
        if type(entry.get('isLead')) is not bool:
            abort(400, 'Lead status must be a boolean.')
        validated[entry['id']] = (entry['isLead'], *employee_values(entry))
    with write_transaction():
        folder_or_404(folder_id)
        members = list(folder_members(folder_id))
        if set(validated) != {m.employee_id for m in members}:
            abort(409, 'The roster changed. Refresh this folder before saving all employees.')
        for member in members:
            member.is_lead, member.min_hours, member.max_hours = validated[member.employee_id]
            member.save(only=[FolderEmployee.is_lead, FolderEmployee.min_hours, FolderEmployee.max_hours])
    return jsonify([serialize(m) for m in members])


@app.put('/api/folders/<int:folder_id>/employees/<int:employee_id>')
@require_admin
def update_folder_employee(folder_id, employee_id):
    requested_folder(folder_id)
    data = body()
    values = employee_values(data)
    with write_transaction():
        member = require_member(folder_id, employee_id)
        member.is_lead = bool(data.get('isLead'))
        member.min_hours, member.max_hours = values
        member.save()
    return jsonify(serialize(member))


@app.delete('/api/folders/<int:folder_id>/employees/<int:employee_id>')
@require_admin
def remove_folder_employee(folder_id, employee_id):
    requested_folder(folder_id)
    with write_transaction():
        member = require_member(folder_id, employee_id)
        member.active = False
        member.save(only=[FolderEmployee.active])
        EditGrant.update(revoked=True).where((EditGrant.employee == employee_id) & (EditGrant.folder == folder_id)).execute()
    return jsonify(ok=True)


# Explicitly scoped aliases for older administrator integrations.
@app.put('/api/employees/<path:name>')
@require_admin
def upsert_employee(name):
    folder = requested_folder()
    name = resolve_name(name)
    if not name:
        abort(400, 'A name is required.')
    employee_values(body())
    member = folder_members(folder.id).where(Employee.name == name).first()
    if member is None:
        abort(409, 'Employees join this folder by submitting availability. Only existing roster entries can be updated.')
    return update_folder_employee.__wrapped__(folder.id, member.employee_id)


@app.delete('/api/employees/<path:name>')
@require_admin
def delete_employee(name):
    folder = requested_folder()
    member = folder_members(folder.id).where(Employee.name == resolve_name(name)).first()
    if member is None:
        abort(404, 'Employee does not belong to this folder.')
    return remove_folder_employee.__wrapped__(folder.id, member.employee_id)


@app.get("/api/submission-context")
def submission_context():
    from collection_codes import public_context
    with write_transaction():
        return jsonify(public_context())


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
        FolderConfig.create(folder=folder, data_json=json.dumps(get_config(folder.id)))
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


def deletion_scope(folder):
    """Call under write_transaction so the counts and version share one scope.

    Hash contents, not just counts: edits and same-count replacements invalidate
    confirmation too. No schema migration or process-local version is needed.
    """
    state = SubmissionState.get_by_id(1)
    active = state.active_folder_id == folder.id
    digest = hashlib.sha256()
    def include(value):
        digest.update(json.dumps(value, sort_keys=True, default=str).encode())
        digest.update(b"\n")
    include([folder.id, folder.name, folder.archived, folder.created_at, active,
             state.revision if active else None])
    counts = {}
    for key, model in (("availabilitySubmissions", FolderAvailability),
                       ("weekdaySchedules", SavedSchedule),
                       ("generationJobs", GenerationJob),
                       ("weekendSchedules", SavedWeekendSchedule),
                       ("unverifiedResponses", IntakeSubmission),
                       ("collectionCodes", CollectionCode),
                       ("folderEmployees", FolderEmployee), ("folderSettings", FolderConfig)):
        include(key)
        counts[key] = 0
        query = model.select().where(model.folder == folder.id).order_by(model.id)
        if model is GenerationJob:
            # Progress/heartbeat updates must not invalidate a deletion preview.
            query = query.select(GenerationJob.id, GenerationJob.fingerprint)
        for row in query.dicts():
            include(row)
            counts[key] += 1
    return {"folder": folder_json(folder), "counts": counts,
            "acceptsSubmissions": active, "previewVersion": digest.hexdigest()}


@app.get("/api/folders/<int:folder_id>/deletion-preview")
@require_admin
def folder_deletion_preview(folder_id):
    with write_transaction():
        preview = deletion_scope(folder_or_404(folder_id))
    return jsonify(preview)


@app.delete("/api/folders/<int:folder_id>")
@require_admin
def delete_folder(folder_id):
    data = body()
    with write_transaction():
        folder = folder_or_404(folder_id)
        if not isinstance(data.get("confirmationName"), str) or data["confirmationName"] != folder.name:
            abort(400, "Type the exact folder name to confirm permanent deletion.")
        version = data.get("previewVersion")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            abort(400, "A deletion preview is required before confirmation.")
        preview = deletion_scope(folder)
        if not secrets.compare_digest(version, preview["previewVersion"]):
            abort(409, "This folder changed since the deletion preview. Review a fresh preview and type the folder name again.")
        if preview["acceptsSubmissions"]:
            SubmissionState.update(active_folder=None, revision=SubmissionState.revision + 1).where(SubmissionState.id == 1).execute()
        SavedSchedule.delete().where(SavedSchedule.folder == folder.id).execute()
        SavedWeekendSchedule.delete().where(SavedWeekendSchedule.folder == folder.id).execute()
        FolderAvailability.delete().where(FolderAvailability.folder == folder.id).execute()
        IntakeSubmission.delete().where(IntakeSubmission.folder == folder.id).execute()
        CollectionCode.delete().where(CollectionCode.folder == folder.id).execute()
        CollectionSettings.delete().where(CollectionSettings.folder == folder.id).execute()
        FolderEmployee.delete().where(FolderEmployee.folder == folder.id).execute()
        FolderConfig.delete().where(FolderConfig.folder == folder.id).execute()
        folder.delete_instance()
    return jsonify(ok=True, deletedFolderId=folder_id, deletedCounts=preview["counts"])


def recheck_generation_folder(original):
    current = folder_or_404(original.id)
    # SQLite can reuse IDs after deleting the final folder. A replacement folder
    # is not the folder whose inputs were read before the solver ran.
    if current.created_at != original.created_at:
        abort(409, "The original folder was deleted. Refresh before generating again.")
    return current


def generation_input_version(rows):
    """Opaque fingerprint; removed/edited source rows invalidate in-flight work."""
    # Consent-only changes keep the pre-solve frozen agreement, as before.
    # Resubmission resets submitted_at; cleanup removes the row entirely.
    versions = sorted((r.id, r.employee_id, r.folder_id, r.submitted_at.isoformat(),
                       r.data_json, r.comment, serialize(r.employee, r.folder_id)) for r in rows)
    # Fingerprint each selected submission and its folder-specific employee attributes.
    return hashlib.sha256(json.dumps(versions).encode()).hexdigest()


def recheck_generation_inputs(folder, version, employee_ids=None):
    rows = FolderAvailability.select().where((FolderAvailability.folder == folder.id) &
        FolderAvailability.employee.in_(folder_members(folder.id).select(FolderEmployee.employee)))
    if employee_ids is not None:
        rows = rows.where(FolderAvailability.employee.in_(employee_ids))
    if generation_input_version(list(rows)) != version:
        abort(409, "Availability changed or expired. Refresh and generate a fresh preview.")


@app.get("/api/availability/<path:name>")
@require_admin
def get_one_availability(name):
    # Supervisor-only legacy lookup; shared codes never grant read access.
    state = SubmissionState.get_by_id(1)
    if not state.active_folder_id or request.args.get("folderId", type=int) != state.active_folder_id:
        abort(409, "The submission folder changed. Refresh and confirm the current folder.")
    employee = Employee.get_or_none(Employee.name == resolve_name(name))
    row = FolderAvailability.get_or_none((FolderAvailability.employee == employee.id) &
          (FolderAvailability.folder == state.active_folder_id)) if employee else None
    cfg = get_config(state.active_folder_id)
    result = submission_json(row, cfg) if row else {}
    result.update(found=row is not None, employee=serialize(employee, state.active_folder_id) if row else None)
    result.update(boundaryContext=boundary_context(cfg), config=cfg)
    return jsonify(result)



@app.get("/api/availability")
@require_admin
def get_all_availability():
    folder = folder_or_404(request.args.get("folderId", type=int))
    employees = [serialize(m) for m in folder_members(folder.id)]
    rows = list(FolderAvailability.select().where((FolderAvailability.folder == folder) &
        FolderAvailability.employee.in_([e["id"] for e in employees])))
    cfg = get_config(folder.id)
    availability = {r.employee.name: r.get_data() for r in rows}
    return jsonify(folder=folder_json(folder), employees=employees, availability=availability,
        submittedAt={r.employee.name: r.submitted_at.isoformat() + "Z" for r in rows},
        comments={r.employee.name: r.comment for r in rows},
        submissions=[submission_json(r, cfg) for r in rows], boundaryContext=boundary_context(cfg),
        missing=[e["name"] for e in employees if e["name"] not in availability])

@app.put("/api/admin/availability")
@require_admin
def admin_update_availability():
    data = body()
    if any(k in data for k in ('allowExtraOpenings', 'allowExtraClosings', 'consentContext', 'consent')):
        abort(400, 'Save additional-shift permissions through the separate permission controls.')
    employee_id = data.get("employeeId")
    folder_id = data.get("folderId")
    availability = data.get("availability")
    comment = data.get("comment", "")

    if type(employee_id) is not int or type(folder_id) is not int:
        abort(400, "Employee ID and Folder ID are required.")
    if not isinstance(availability, dict):
        abort(400, "Availability must be an object.")
    if not isinstance(comment, str) or len(comment) > 99:
        abort(400, "Comments must be shorter than 100 characters.")

    with write_transaction():
        folder = folder_or_404(folder_id)
        require_member(folder.id, employee_id)
        employee = Employee.get_or_none(Employee.id == employee_id)
        if not employee:
            abort(404, "Employee not found.")

        # Supervisor collection is wider than weekday scheduling. Closing/staffing
        # settings still apply in the solvers, not when editing availability.
        valid = {f"{d}_{h:02d}" for d in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
                 for h in range(7, 17 if d == 'Sun' else 22)}
        if any(k not in valid or (type(v) not in (int, bool) or v not in (0, 1, 2)) for k, v in availability.items()):
            abort(400, "Invalid availability time slot.")

        row = FolderAvailability.get_or_none((FolderAvailability.employee == employee) & (FolderAvailability.folder == folder))
        if row is None:
            abort(404, 'Current availability no longer exists. Reload current availability.')
        if data.get('permissionVersion') != permission_version(row):
            abort(409, 'Current availability changed after this form was loaded. Reload current availability before saving.')
        row.data_json = json.dumps(availability)
        row.comment = comment
        row.submitted_at = datetime.datetime.utcnow()
        row.save_version += 1
        row.save()

    return jsonify(ok=True)

# ---------------- diagnostics ----------------
def _load_inputs(folder_id, ids=None):
    folder_or_404(folder_id)
    cfg = get_config(folder_id)
    rows = FolderAvailability.select().where((FolderAvailability.folder == folder_id) &
        FolderAvailability.employee.in_(folder_members(folder_id).select(FolderEmployee.employee)))
    if ids is not None:
        rows = rows.where(FolderAvailability.employee.in_(ids))
    rows = list(rows)
    if ids is not None and {r.employee_id for r in rows} != set(ids):
        abort(400, "Every selected employee must have a submission in this folder.")
    freeze_consent(cfg, rows, folder_id)
    return cfg, [serialize(r.employee, folder_id) for r in rows], {r.employee.name: r.get_data() for r in rows}, rows

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
@app.post("/api/generate_weekend")
@require_admin
def generate_weekend():
    data = body()
    config = data.get("config")

    if not config or "start_date" not in config or "end_date" not in config:
        abort(400, "Missing required weekend configuration.")

    with write_transaction():
        folder = folder_or_404(data.get("folderId"))

        # We fetch all employees for this folder.
        rows = list(FolderAvailability.select().where((FolderAvailability.folder == folder) &
            FolderAvailability.employee.in_(folder_members(folder.id).select(FolderEmployee.employee))))

        roster = [serialize(m) for m in folder_members(folder.id)]
        availability = {r.employee.name: r.get_data() for r in rows}

        ids = {e['id'] for e in roster}
        selected = list(config.get('fixed_assignments', {}).values()) + list(config.get('rotating_employees', []))
        if any(type(eid) is not int or eid not in ids for eid in selected):
            abort(400, 'Every weekend employee must belong to this folder.')

        input_version = generation_input_version(rows)
    result = weekend_generator.generate_weekend_schedule(config, availability, roster)

    with write_transaction():
        recheck_generation_folder(folder)
        recheck_generation_inputs(folder, input_version)

    # Send it back
    return jsonify({
        "status": "SUCCESS",
        "assignments": result["assignments"],
        "signup_shifts": result["signup_shifts"],
        "effective_pool": result["effective_pool"],
        "rotating_counts": result["rotating_counts"],
        "employees": roster,
        "config": config,
        "folderId": folder.id,
        "folderVersion": folder.created_at.isoformat(),
        "inputVersion": input_version
    })


@app.post("/api/save_weekend")
@require_admin
def save_weekend():
    data = body()
    folder_id = data.get("folderId")
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot or not folder_id:
        abort(400, "Missing snapshot or folder.")
    with write_transaction():
        folder = folder_or_404(folder_id)
        if snapshot.get("folderId") != folder.id or snapshot.get("folderVersion") != folder.created_at.isoformat():
            abort(409, "The weekend preview does not belong to this folder. Generate a fresh preview before saving.")
        recheck_generation_inputs(folder, snapshot.get("inputVersion"))
        saved = SavedWeekendSchedule.create(folder=folder, snapshot_json=json.dumps(snapshot))
    return jsonify({"savedScheduleId": saved.id})


@app.get("/api/folders/<int:folder_id>/weekend_schedules")
@require_admin
def weekend_schedules(folder_id):
    folder_or_404(folder_id)
    return jsonify([{"id": s.id, "createdAt": s.created_at.isoformat() + "Z"} for s in
                    SavedWeekendSchedule.select().where(SavedWeekendSchedule.folder == folder_id).order_by(SavedWeekendSchedule.id.desc())])


@app.route("/api/folders/<int:folder_id>/weekend_schedules/<int:schedule_id>", methods=["GET", "DELETE"])
@require_admin
def saved_weekend_schedule(folder_id, schedule_id):
    with write_transaction():
        saved = SavedWeekendSchedule.get_or_none((SavedWeekendSchedule.id == schedule_id) & (SavedWeekendSchedule.folder == folder_id))
        if not saved:
            abort(404, "Saved schedule not found.")
        if request.method == "DELETE":
            if body().get("confirm") is not True:
                abort(400, "Confirm deletion of this saved schedule.")
            saved.delete_instance()
            return jsonify(ok=True)
        return jsonify(json.loads(saved.snapshot_json))


@app.post("/api/generate")
@require_admin
def generate():
    data = body()
    ids = data.get("employeeIds")
    if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids) or len(ids) != len(set(ids)):
        abort(400, "Select at least one employee with submitted availability.")
    seed = data.get("seed")
    if seed is not None and (type(seed) is not int or not 0 <= seed < 2**31):
        abort(400, "Invalid generation seed.")
    with write_transaction():
        folder = folder_or_404(data.get("folderId"))
        for employee_id in ids:
            require_member(folder.id, employee_id)
        rows = list(FolderAvailability.select().where((FolderAvailability.folder == folder) & (FolderAvailability.employee.in_(ids))))
        if {r.employee_id for r in rows} != set(ids):
            abort(400, "Every selected employee must have a submission in this folder.")
        roster = [serialize(r.employee, folder.id) for r in rows]
        availability = {r.employee.name: r.get_data() for r in rows}
        cfg = get_config(folder.id)
        freeze_consent(cfg, rows, folder.id)
        submissions = [submission_json(r, cfg) for r in rows]
        input_version = generation_input_version(rows)
        if not 1 <= int(cfg['solverTimeLimit']) <= 1800:
            abort(400, 'Set the solver time limit between 1 and 1800 seconds.')
        payload = dict(employees=roster, availability=availability, config=cfg, seed=seed,
                       submissions=submissions, employeeIds=ids, folder=folder_json(folder),
                       folderVersion=folder.created_at.isoformat() + 'Z',
                       inputVersion=input_version, requestId=data.get('requestId'))
        job = generation_jobs.enqueue(payload)
        response = generation_jobs.describe(job)
    if app.config['GENERATION_RUNNER_ENABLED']:
        generation_jobs.wake()
    return jsonify(response), 202


@app.get('/api/folders/<int:folder_id>/generation-jobs/latest')
@app.get('/api/folders/<int:folder_id>/generation-jobs/<job_id>')
@require_admin
def generation_status(folder_id, job_id=None):
    folder_or_404(folder_id)
    query = GenerationJob.select().where(GenerationJob.folder == folder_id)
    job = query.where(GenerationJob.id == job_id).first() if job_id else query.order_by(GenerationJob.created_at.desc()).first()
    if not job and job_id:
        abort(404, 'Generation job not found in this folder.')
    response = generation_jobs.describe(job) if job else None
    if job and job.status in generation_jobs.ACTIVE and app.config['GENERATION_RUNNER_ENABLED']:
        generation_jobs.wake()
    return jsonify(response)


@app.post('/api/folders/<int:folder_id>/generation-jobs/<job_id>/cancel')
@require_admin
def cancel_generation(folder_id, job_id):
    with write_transaction():
        folder_or_404(folder_id)
        job = GenerationJob.get_or_none((GenerationJob.id == job_id) & (GenerationJob.folder == folder_id))
        if not job:
            abort(404, 'Generation job not found in this folder.')
        if job.status in generation_jobs.ACTIVE:
            job.status = 'cancelled'
            job.owner = job.lease_until = None
            job.finished_at = now()
            job.save()
        response = generation_jobs.describe(job)
    return jsonify(response)


@app.get("/api/folders/<int:folder_id>/schedules")
@require_admin
def schedules(folder_id):
    folder_or_404(folder_id)
    return jsonify([{"id": s.id, "createdAt": s.created_at.isoformat() + "Z"} for s in
                    SavedSchedule.select().where(SavedSchedule.folder == folder_id).order_by(SavedSchedule.id.desc())])


@app.route("/api/folders/<int:folder_id>/schedules/<int:schedule_id>", methods=["GET", "DELETE"])
@require_admin
def saved_schedule(folder_id, schedule_id):
    with write_transaction():
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
from collection_codes import register as register_collection_codes
register_collection_codes(app, require_admin, permission_version, body, folder_or_404)

if __name__ == "__main__":
    if not db.is_closed():
        db.close()
    generation_jobs.wake()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
