"""Account-free, quota-bounded intake. Accepted FolderAvailability remains solver input.

All quota/accept/revoke writes use the application's cross-worker transaction lock.
No production identity claims are made for a shared collection code.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken
from flask import abort, jsonify, request
from models import (db, write_transaction, Employee, Folder, FolderAvailability,
                    SubmissionState, CollectionSettings, CollectionCode,
                    IntakeSubmission, EditGrant, SubmissionSession, RateBucket,
                    get_config, normalize_level)
from boundary import boundary_context, consent_status

MAX_CODE_RESPONSES = 10000
COOKIE = 'availability_session'
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
PUBLIC_FIELDS = ('days', 'availabilityDays', 'hourStart', 'hourEnd', 'dayCloseHours',
                 'minShiftLength', 'maxMorningShifts', 'maxEveningShifts',
                 'maxMorningPlusEvening', 'allowPreferredBoundaryExtras')
EDIT_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def now():
    return dt.datetime.utcnow()


def keys():
    try:
        f = Fernet(os.environ['COLLECTION_ENCRYPTION_KEY'].encode())
        k = bytes.fromhex(os.environ['COLLECTION_VERIFIER_KEY'])
        if len(k) != 32:
            raise ValueError()
        return f, k
    except (KeyError, ValueError):
        abort(503, 'Collection codes need one-time server setup. Ask the app maintainer to configure the collection keys.')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def verify_code(value):
    if not isinstance(value, str) or len(value) > 40:
        abort(400, 'Enter the collection code your supervisor sent.')
    normalized = re.sub(r'[\s-]', '', value).upper()
    return hmac.new(keys()[1], ('code:' + normalized).encode(), hashlib.sha256).hexdigest()


def csrf(token):
    return hmac.new(keys()[1], ('csrf:' + token).encode(), hashlib.sha256).hexdigest()


def rate_limit(kind, identity, limit, seconds=600):
    """Fixed keyed hash buckets + global ceiling. Transaction commits even on rejection.

    X-Forwarded-For is deliberately not consumed here. A trusted proxy middleware
    may be configured separately; shared-address limits allow a 35-person burst.
    """
    slot = int(hmac.new(keys()[1], (kind + ':' + identity).encode(), hashlib.sha256).hexdigest()[:8], 16) % 512 + 1
    window = int(time.time()) // seconds
    denied = False
    with write_transaction():
        for bucket_id, ceiling in ((slot, limit), (0, 1000)):
            bucket, _ = RateBucket.get_or_create(slot=bucket_id, defaults={'window': window})
            if bucket.window != window:
                bucket.window, bucket.count = window, 0
            denied |= bucket.count >= ceiling
            bucket.count = min(bucket.count + 1, 1001)
            bucket.save()
    if denied:
        abort(429, 'Too many requests. Please wait a few minutes and try again.')


def code_status(code):
    if code.deleted:
        return 'Deleted'
    if code.revoked:
        return 'Revoked'
    if code.received >= code.response_limit:
        return 'Exhausted'
    if code.expires_at and code.expires_at <= now():
        return 'Expired'
    if code.folder.archived:
        return 'Collection closed'
    settings, _ = CollectionSettings.get_or_create(folder=code.folder_id)
    if settings.received >= settings.response_cap:
        return 'Collection paused'
    return 'Active'


def check_code(code, allow_exhausted=False):
    status = code_status(code)
    if status != 'Active' and not (allow_exhausted and status in ('Exhausted', 'Collection paused')):
        if status == 'Collection paused':
            abort(409, 'This folder has reached its total submission limit. Ask your supervisor to increase the folder limit.')
        abort(409, 'This code is not accepting responses. Ask your supervisor for another code or to reopen collection.')


def session_record(require_csrf=False):
    token = request.cookies.get(COOKIE, '')
    session = SubmissionSession.get_or_none(SubmissionSession.token_hash == digest(token)) if token else None
    if not session or session.expires_at <= now():
        abort(401, 'Enter a valid collection code to continue.')
    if require_csrf and not hmac.compare_digest(request.headers.get('X-Submission-CSRF', ''), csrf(token)):
        abort(403, 'Refresh the form and enter your collection code again.')
    if session.edit_grant_id and (session.edit_grant.revoked or session.edit_grant.expires_at <= now()):
        abort(401, 'This edit link expired or was revoked. Ask your supervisor for another link.')
    check_code(session.code, allow_exhausted=True)
    return session, token


def validate_payload(data, cfg, editing=False):
    if not isinstance(data, dict):
        abort(400, 'A JSON object is required.')
    allowed = {'name', 'availability', 'comment', 'folderId', 'revision', 'allowExtraOpenings',
               'allowExtraClosings', 'consentContext', 'requestId', 'permissionVersion'}
    if set(data) - allowed:
        abort(400, 'Unexpected submission fields.')
    name, comment, av = data.get('name'), data.get('comment', ''), data.get('availability')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        abort(400, 'Enter a name of 1–100 characters.')
    if not isinstance(comment, str) or len(comment) > 99:
        abort(400, 'Comments must be shorter than 100 characters.')
    if not isinstance(av, dict):
        abort(400, 'Availability must be an object.')
    valid = ({f'{d}_{h:02d}' for d in EDIT_DAYS for h in range(7, 17 if d == 'Sun' else 22)}
             if editing else {f'{d}_{h:02d}' for d in cfg['availabilityDays'] for h in range(cfg['hourStart'], cfg['hourEnd'] + 1)})
    if any(k not in valid or type(v) not in (int, bool) or v not in (0, 1, 2) for k, v in av.items()):
        abort(400, 'Invalid availability time slot. Refresh the form and try again.')
    fields = ('allowExtraOpenings', 'allowExtraClosings')
    explicit = any(k in data for k in fields)
    if explicit:
        if any(type(data.get(k)) is not bool for k in fields):
            abort(400, 'Both consent choices must be explicit booleans.')
        if data.get('consentContext') != boundary_context(cfg)['token']:
            abort(409, 'Limits or shift blocks changed. Refresh and reconfirm your consent choices.')
    rid = data.get('requestId')
    if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', rid):
        abort(400, 'A request ID is required. Refresh the form before submitting.')
    return re.sub(r'\s+', ' ', name).strip(), comment, av, explicit


def linked_current(grant):
    employee = Employee.get_or_none(Employee.id == grant.employee_id) if grant.employee_id else None
    current = FolderAvailability.get_or_none((FolderAvailability.employee == grant.employee_id) &
        (FolderAvailability.folder == grant.folder_id)) if employee and grant.folder_id else None
    if current is None or grant.folder_id != grant.submission.folder_id:
        abort(404, 'The linked employee or current availability no longer exists in this folder. Ask your supervisor for help.')
    return current


def public_context(session=None, token=None, permission_version=None):
    state = SubmissionState.get_by_id(1)
    cfg = get_config()
    result = dict(folder={'id': state.active_folder_id, 'name': state.active_folder.name} if state.active_folder_id else None,
                  revision=state.revision, config={k: cfg[k] for k in PUBLIC_FIELDS if k in cfg},
                  boundaryContext=boundary_context(cfg), unlocked=bool(session))
    if session:
        result['folder'] = dict(id=session.code.folder_id, name=session.code.folder.name)
        result['revision'] = session.state_revision
        result['responseLimit'] = session.code.response_limit
        result['csrf'] = csrf(token)
        result['submitted'] = bool(session.submitted_request_key)
        if session.edit_grant_id:
            row = linked_current(session.edit_grant)
            # The administrator edits the full collection window independently of
            # weekday scheduling hours. Expose that same window when correcting it.
            result['config'].update(availabilityDays=list(EDIT_DAYS), hourStart=7, hourEnd=21,
                dayCloseHours={d: 17 if d == 'Sun' else 22 for d in EDIT_DAYS})
            recorded = dict(allowExtraOpenings=row.allow_extra_openings, allowExtraClosings=row.allow_extra_closings,
                            consentContext=row.consent_context)
            result['edit'] = dict(name=session.edit_grant.submission.name, employeeId=row.employee_id,
                                 permissionVersion=permission_version(row), availability=json.loads(row.data_json), comment=row.comment,
                                 consent=consent_status(json.loads(row.data_json), recorded, cfg))
    return result


def receipt(row):
    av = json.loads(row.data_json)
    return dict(ok=True, availableHours=sum(normalize_level(v) > 0 for v in av.values()),
                preferredHours=sum(normalize_level(v) == 2 for v in av.values()), pendingReview=False)


def save_to_folder(row, permission_version):
    """Every submission has its own roster entry; shared codes never establish identity."""
    base = row.name
    name = base
    number = 2
    existing = {employee.name.casefold() for employee in Employee.select(Employee.name)}
    while name.casefold() in existing:
        name = f'{base} ({number})'
        number += 1
    employee = Employee.create(name=name)
    current = FolderAvailability.create(employee=employee, folder=row.folder,
        data_json=row.data_json, comment=row.comment,
        allow_extra_openings=row.allow_extra_openings, allow_extra_closings=row.allow_extra_closings,
        consent_context=row.consent_context, submitted_at=row.submitted_at)
    row.employee = employee
    row.status = 'Accepted'
    row.accepted_fingerprint = permission_version(current)
    row.save(only=[IntakeSubmission.employee, IntakeSubmission.status, IntakeSubmission.accepted_fingerprint])


def import_pending_responses(permission_version):
    """Idempotently import pending sheets separately without changing existing availability."""
    with db.connection_context(), write_transaction():
        for row in IntakeSubmission.select().where(IntakeSubmission.status == 'Pending').order_by(
                IntakeSubmission.submitted_at, IntakeSubmission.id):
            save_to_folder(row, permission_version)


def register(app, require_admin, permission_version, body, folder_or_404):
    import_pending_responses(permission_version)

    @app.post('/api/collection/unlock')
    def unlock():
        rate_limit('unlock', request.remote_addr or '?', 150)
        data = body()
        with write_transaction():
            if 'editToken' in data:
                token = data['editToken']
                if not isinstance(token, str) or len(token) > 200:
                    abort(400, 'Invalid edit link.')
                grant = EditGrant.get_or_none(EditGrant.token_hash == digest(token))
                if not grant or grant.revoked or grant.redeemed or grant.expires_at <= now():
                    abort(401, 'Edit link expired, already used, or revoked. Ask your supervisor for another link.')
                code = grant.submission.code
                linked_current(grant)
                grant.redeemed = True
                grant.save()
            else:
                grant = None
                code = CollectionCode.get_or_none(CollectionCode.verifier == verify_code(data.get('code')))
                if not code:
                    abort(401, 'Invalid code. Check the code your supervisor sent.')
            check_code(code)
            # Bound session rows as well as sheets. Expired rows may be deleted.
            SubmissionSession.delete().where(SubmissionSession.expires_at <= now()).execute()
            if SubmissionSession.select().where(SubmissionSession.code == code).count() >= 200:
                abort(429, 'Too many form sessions for this code. Try later or ask for another code.')
            secret = secrets.token_urlsafe(32)
            state = SubmissionState.get_by_id(1)
            session = SubmissionSession.create(code=code, token_hash=digest(secret), state_revision=state.revision,
                edit_grant=grant, expires_at=now() + dt.timedelta(hours=2))
            result = public_context(session, secret, permission_version)
        response = jsonify(result)
        response.set_cookie(COOKIE, secret, httponly=True, secure=bool(os.environ.get('RENDER')) or request.is_secure,
                            samesite='Strict', max_age=7200, path='/api/')
        return response

    @app.get('/api/collection/context')
    def context():
        with write_transaction():
            session, token = session_record()
            return jsonify(public_context(session, token, permission_version))

    @app.post('/api/availability')
    def submit():
        rate_limit('submit-address', request.remote_addr or '?', 200)
        session, _ = session_record(require_csrf=True)
        rate_limit('submit-session', str(session.id), 100)
        rate_limit('submit-code', str(session.code_id), 200)
        data = body()
        with write_transaction():
            session, _ = session_record(require_csrf=True)
            code = session.code
            cfg = get_config()
            name, comment, av, explicit = validate_payload(data, cfg, editing=bool(session.edit_grant_id))
            if type(data.get('folderId')) is not int or type(data.get('revision')) is not int or data['folderId'] != code.folder_id or data['revision'] != session.state_revision:
                abort(409, 'The collection changed. Refresh and confirm the destination.')
            if session.edit_grant_id:
                original = session.edit_grant.submission
                if name != original.name:
                    abort(400, 'An edit link cannot change the submission name.')
            key = digest(str(session.id) + ':' + data['requestId'])
            payload_hash = digest(json.dumps(data, sort_keys=True))
            previous = IntakeSubmission.get_or_none(IntakeSubmission.request_key == key)
            if previous:
                if previous.payload_hash != payload_hash:
                    abort(409, 'This request ID was already used. Start a new submission.')
                return jsonify(receipt(previous))
            current = linked_current(session.edit_grant) if session.edit_grant_id else None
            if current is not None and data.get('permissionVersion') != permission_version(current):
                abort(409, 'Current availability changed after this form was loaded. Reload current availability before saving.')
            check_code(code)
            if session.submitted_request_key:
                abort(409, 'This form has already been submitted. Enter a code again for another submission.')
            settings, _ = CollectionSettings.get_or_create(folder=code.folder)
            if settings.received >= settings.response_cap:
                abort(409, 'This folder has reached its total submission limit. Ask your supervisor to increase the folder limit.')
            row = IntakeSubmission.create(folder=code.folder, code=code, name=name, data_json=json.dumps(av), comment=comment,
                allow_extra_openings=data.get('allowExtraOpenings', False), allow_extra_closings=data.get('allowExtraClosings', False),
                consent_context=boundary_context(cfg)['token'] if explicit else None,
                parent_id=session.edit_grant.submission_id if session.edit_grant_id else None,
                request_key=key, payload_hash=payload_hash)
            if current is None:
                save_to_folder(row, permission_version)
            else:
                for field in ('data_json', 'comment', 'allow_extra_openings', 'allow_extra_closings', 'consent_context', 'submitted_at'):
                    setattr(current, field, getattr(row, field))
                current.save_version += 1
                current.save()
                IntakeSubmission.update(status='Superseded', review_version=IntakeSubmission.review_version+1).where(
                    (IntakeSubmission.folder == current.folder_id) & (IntakeSubmission.employee == current.employee_id) &
                    (IntakeSubmission.status == 'Accepted')).execute()
                row.employee = current.employee_id
                row.status = 'Accepted'
                row.accepted_fingerprint = permission_version(current)
                row.save(only=[IntakeSubmission.employee, IntakeSubmission.status, IntakeSubmission.accepted_fingerprint])
            code.received += 1
            code.save(only=[CollectionCode.received])
            settings.received += 1
            settings.save(only=[CollectionSettings.received])
            session.submitted_request_key = key
            session.save(only=[SubmissionSession.submitted_request_key])
            return jsonify(receipt(row))

    def code_json(code):
        try:
            plain = keys()[0].decrypt(code.encrypted_code.encode()).decode()
        except InvalidToken:
            abort(503, 'Collection encryption key changed. Ask the maintainer to restore the original key.')
        return dict(id=code.id, code=plain, label=code.label, folderId=code.folder_id,
                    folderName=code.folder.name, createdAt=code.created_at.isoformat()+'Z',
                    expiresAt=code.expires_at.isoformat()+'Z' if code.expires_at else None,
                    received=code.received, responseLimit=code.response_limit,
                    remaining=max(0, code.response_limit-code.received), status=code_status(code))

    @app.get('/api/admin/codes')
    @require_admin
    def list_codes():
        with write_transaction():
            folder = folder_or_404(request.args.get('folderId', type=int))
            settings, _ = CollectionSettings.get_or_create(folder=folder)
            query = CollectionCode.select().where(CollectionCode.folder == folder)
            if request.args.get('showDeleted') != '1':
                query = query.where(CollectionCode.deleted == False)
            return jsonify(codes=[code_json(c) for c in query.order_by(CollectionCode.id.desc())],
                           collection=dict(received=settings.received, responseCap=settings.response_cap))

    @app.post('/api/admin/codes')
    @require_admin
    def create_code():
        data = body()
        label = data.get('label', '')
        if not isinstance(label, str) or len(label) > 100 or type(data.get('folderId')) is not int:
            abort(400, 'Choose a folder and enter a label of up to 100 characters.')
        expiry = data.get('expiresAt')
        if expiry:
            try:
                expiry = dt.datetime.fromisoformat(expiry.replace('Z', '+00:00')).astimezone(dt.timezone.utc).replace(tzinfo=None)
                if expiry <= now():
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                abort(400, 'Choose a future closing date.')
        else:
            expiry = None
        with write_transaction():
            folder = folder_or_404(data['folderId'])
            if folder.archived:
                abort(409, 'Restore the folder before creating a code.')
            if CollectionCode.select().where(CollectionCode.folder == folder).count() >= 100:
                abort(409, 'This folder already has 100 codes. Create a new scheduling folder for more codes.')
            raw = ''.join(secrets.choice(ALPHABET) for _ in range(12))
            display = '-'.join(raw[i:i+4] for i in range(0,12,4))
            code = CollectionCode.create(folder=folder, verifier=verify_code(display),
                encrypted_code=keys()[0].encrypt(display.encode()).decode(), label=label.strip(), expires_at=expiry)
            return jsonify(code_json(code)), 201

    @app.post('/api/admin/codes/<int:code_id>/responses')
    @require_admin
    def add_code_responses(code_id):
        additional = body().get('additionalResponses')
        if type(additional) is not int or not 1 <= additional <= MAX_CODE_RESPONSES - 30:
            abort(400, 'Additional responses must be a whole number from 1 to 9970.')
        with write_transaction():
            code = CollectionCode.get_or_none(CollectionCode.id == code_id)
            if not code:
                abort(404, 'Code not found.')
            if code.revoked or code.deleted:
                abort(409, 'Revoked or deleted codes cannot receive additional responses. Create a new code.')
            if code.response_limit + additional > MAX_CODE_RESPONSES:
                abort(400, 'A code can allow at most 10000 responses total.')
            code.response_limit += additional
            code.save(only=[CollectionCode.response_limit])
            return jsonify(code_json(code))

    @app.route('/api/admin/codes/<int:code_id>', methods=['PATCH','DELETE'])
    @require_admin
    def stop_code(code_id):
        with write_transaction():
            code = CollectionCode.get_or_none(CollectionCode.id == code_id)
            if not code:
                abort(404, 'Code not found.')
            code.revoked = True
            if request.method == 'DELETE':
                code.deleted = True
            code.save(only=[CollectionCode.revoked, CollectionCode.deleted])
            return jsonify(ok=True)

    @app.put('/api/admin/collections/<int:folder_id>')
    @require_admin
    def collection_cap(folder_id):
        cap = body().get('responseCap')
        if type(cap) is not int or not 30 <= cap <= 10000:
            abort(400, 'Response cap must be a whole number from 30 to 10000.')
        with write_transaction():
            folder_or_404(folder_id)
            settings, _ = CollectionSettings.get_or_create(folder=folder_id)
            settings.response_cap = cap
            settings.save()
        return jsonify(ok=True)

    @app.get('/api/admin/intake')
    @require_admin
    def intake():
        with write_transaction():
            fid = request.args.get('folderId', type=int)
            folder_or_404(fid)
            cfg = get_config()
            roster = list(Employee.select().order_by(Employee.name))
            rows = []
            for row in IntakeSubmission.select().where(IntakeSubmission.folder == fid).order_by(IntakeSubmission.id.desc()):
                candidate = next((e for e in roster if e.name.casefold() == row.name.casefold()), None)
                accepted_id = row.employee_id
                accepted = FolderAvailability.get_or_none((FolderAvailability.employee == accepted_id) & (FolderAvailability.folder == fid)) if accepted_id else None
                recorded = dict(allowExtraOpenings=row.allow_extra_openings, allowExtraClosings=row.allow_extra_closings, consentContext=row.consent_context)
                rows.append(dict(id=row.id, name=row.name, availability=json.loads(row.data_json), comment=row.comment,
                    submittedAt=row.submitted_at.isoformat()+'Z', status=row.status, reviewVersion=row.review_version,
                    candidateEmployeeId=candidate.id if candidate else None, employeeId=row.employee_id,
                    acceptedVersion=permission_version(accepted) if accepted else None, parentId=row.parent_id,
                    codeId=row.code_id, codeLabel=row.code.label,
                    canCreateEditLink=bool(accepted and code_status(row.code) == 'Active'),
                    canRevokeEditLinks=bool(accepted),
                    consent=consent_status(json.loads(row.data_json), recorded, cfg)))
            return jsonify(submissions=rows, employees=[dict(id=e.id, name=e.name) for e in roster],
                acceptedVersions={str(r.employee_id): permission_version(r) for r in FolderAvailability.select().where(FolderAvailability.folder == fid)})

    @app.post('/api/admin/intake/<int:entry_id>/review')
    @require_admin
    def review(entry_id):
        data = body()
        with write_transaction():
            row = IntakeSubmission.get_or_none(IntakeSubmission.id == entry_id)
            if not row:
                abort(404, 'Submission not found.')
            if type(data.get('reviewVersion')) is not int or data['reviewVersion'] != row.review_version:
                abort(409, 'This submission was reviewed already. Reload before choosing.')
            if data.get('action') not in ('accept','reject'):
                abort(400, 'Choose accept or reject.')
            if data['action'] == 'reject':
                if row.status == 'Accepted':
                    abort(409, 'Choose a replacement before rejecting an accepted submission.')
                row.status, row.review_version = 'Rejected', row.review_version + 1
                row.save(only=[IntakeSubmission.status, IntakeSubmission.review_version])
                return jsonify(ok=True)
            if row.status == 'Accepted':
                abort(409, 'This sheet is already accepted. Reload before choosing a replacement.')
            eid = data.get('employeeId')
            if type(eid) is not int:
                abort(400, 'Choose an employee from the roster. Add the employee first if needed.')
            employee = Employee.get_or_none(Employee.id == eid)
            if not employee:
                abort(404, 'Employee not found.')
            accepted = FolderAvailability.get_or_none((FolderAvailability.employee == eid) & (FolderAvailability.folder == row.folder_id))
            version = permission_version(accepted) if accepted else None
            if data.get('acceptedVersion') != version:
                abort(409, 'Accepted availability changed. Reload and review the replacement.')
            # No silent stale consent upgrade: copy recorded choices and context.
            IntakeSubmission.update(status='Superseded', review_version=IntakeSubmission.review_version+1).where(
                (IntakeSubmission.folder == row.folder_id) & (IntakeSubmission.employee == eid) & (IntakeSubmission.status == 'Accepted')).execute()
            if accepted is None:
                accepted = FolderAvailability.create(employee=employee, folder=row.folder)
            for field in ('data_json','comment','allow_extra_openings','allow_extra_closings','consent_context','submitted_at'):
                setattr(accepted,field,getattr(row,field))
            accepted.save_version += 1
            accepted.save()
            row.employee, row.status, row.review_version = employee, 'Accepted', row.review_version+1
            row.accepted_fingerprint = permission_version(accepted)
            row.save(only=[IntakeSubmission.employee,IntakeSubmission.status,IntakeSubmission.review_version,IntakeSubmission.accepted_fingerprint])
            return jsonify(ok=True)

    @app.post('/api/admin/intake/<int:entry_id>/edit-links')
    @require_admin
    def edit_link(entry_id):
        with write_transaction():
            row = IntakeSubmission.get_or_none(IntakeSubmission.id == entry_id)
            if not row:
                abort(404, 'Submission not found.')
            check_code(row.code)
            if not row.employee_id or not FolderAvailability.get_or_none(
                    (FolderAvailability.employee == row.employee_id) & (FolderAvailability.folder == row.folder_id)):
                abort(404, 'The linked employee or current availability no longer exists in this folder.')
            EditGrant.update(revoked=True).where((EditGrant.employee == row.employee_id) & (EditGrant.folder == row.folder_id)).execute()
            token = secrets.token_urlsafe(32)
            grant = EditGrant.create(submission=row, employee=row.employee_id, folder=row.folder_id,
                token_hash=digest(token),expires_at=now()+dt.timedelta(hours=24))
            return jsonify(token=token,grantId=grant.id,expiresAt=grant.expires_at.isoformat()+'Z')

    @app.delete('/api/admin/intake/<int:entry_id>/edit-links')
    @require_admin
    def revoke_entry_edits(entry_id):
        with write_transaction():
            row = IntakeSubmission.get_or_none(IntakeSubmission.id == entry_id)
            if not row:
                abort(404, 'Submission not found.')
            EditGrant.update(revoked=True).where((EditGrant.submission == entry_id) |
                ((EditGrant.employee == row.employee_id) & (EditGrant.folder == row.folder_id))).execute()
        return jsonify(ok=True)

    @app.delete('/api/admin/edit-links/<int:grant_id>')
    @require_admin
    def revoke_edit(grant_id):
        with write_transaction():
            EditGrant.update(revoked=True).where(EditGrant.id == grant_id).execute()
        return jsonify(ok=True)
