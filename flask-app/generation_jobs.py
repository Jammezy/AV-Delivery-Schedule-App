"""Durable, bounded jobs using the existing database and web service.

Short transactions serialize admission/claims across deploys. A renewable lease
and random fencing token prevent an old process from committing after recovery.
No connection/polling is kept alive when the queue is empty.
"""
import atexit
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from models import db, write_transaction, GenerationJob, SavedSchedule

ACTIVE = ('queued', 'running')
LEASE_SECONDS = 60
MAX_QUEUE = 3
MAX_ATTEMPTS = 3
_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_child = None


def utcnow():
    return dt.datetime.utcnow()


def fingerprint(payload):
    cfg = {k: v for k, v in payload['config'].items() if k not in ('solverTimeLimit', 'solverWorkers')}
    source = dict(version=1, folderId=payload['folder']['id'], folderVersion=payload['folderVersion'], inputVersion=payload['inputVersion'],
                  employees=sorted(payload['employees'], key=lambda e: e['id']),
                  availability=payload['availability'], config=cfg)
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def describe(job):
    checkpoint = json.loads(job.checkpoint_json) if job.checkpoint_json else None
    payload = json.loads(job.payload_json)
    return dict(jobId=job.id, folderId=job.folder_id, status=job.status,
                attempts=job.attempts, createdAt=job.created_at.isoformat() + 'Z',
                startedAt=job.started_at.isoformat() + 'Z' if job.started_at else None,
                timeLimit=payload['config']['solverTimeLimit'], mode=payload.get('mode', 'optimize'),
                bestFairness=checkpoint.get('fairnessFloor') if checkpoint else None,
                fairnessOptimal=bool(checkpoint and checkpoint.get('fairnessOptimal')),
                result=json.loads(job.result_json) if job.result_json else None,
                error=job.error, errorStatus=job.error_status)


def enqueue(payload):
    """Caller holds the shared write transaction; repeated submissions reuse work."""
    from flask import abort
    key = fingerprint(payload)
    request_id = payload.get('requestId')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id)):
        abort(400, 'Invalid generation request identifier.')
    prior_request = GenerationJob.get_or_none(GenerationJob.id == request_id) if request_id else None
    if prior_request:
        prior = json.loads(prior_request.payload_json)
        if (prior_request.fingerprint != key or prior['config']['solverTimeLimit'] != payload['config']['solverTimeLimit'] or
            prior.get('mode', 'optimize') != payload.get('mode', 'optimize')):
            abort(409, 'This request identifier was already used for different inputs.')
        return prior_request
    existing = GenerationJob.get_or_none((GenerationJob.folder == payload['folder']['id']) &
                                         GenerationJob.status.in_(ACTIVE))
    if existing:
        prior = json.loads(existing.payload_json)
        if (existing.fingerprint == key and prior['config']['solverTimeLimit'] == payload['config']['solverTimeLimit'] and
            prior.get('mode', 'optimize') == payload.get('mode', 'optimize')):
            return existing
        abort(409, 'This folder already has a generation in progress. Wait for it or cancel it before changing the inputs.')
    if GenerationJob.select().where(GenerationJob.status.in_(ACTIVE)).count() >= MAX_QUEUE:
        abort(429, 'Three schedules are already in progress. Try again when one finishes.')
    return GenerationJob.create(id=request_id or secrets.token_hex(16), folder=payload['folder']['id'],
                                fingerprint=key, payload_json=json.dumps(payload))


def owned(job_id, owner):
    return GenerationJob.get_or_none((GenerationJob.id == job_id) &
        (GenerationJob.status == 'running') & (GenerationJob.owner == owner) &
        (GenerationJob.lease_until > utcnow()))


def claim():
    with db.connection_context(), write_transaction():
        expired = list(GenerationJob.select().where((GenerationJob.status == 'running') &
                                                    (GenerationJob.lease_until <= utcnow())))
        for job in expired:
            exhausted = job.attempts >= MAX_ATTEMPTS
            GenerationJob.update(status='failed' if exhausted else 'queued', owner=None, lease_until=None,
                error='Generation was interrupted repeatedly. Please try again.' if exhausted else None,
                finished_at=utcnow() if exhausted else None).where(GenerationJob.id == job.id).execute()
        if GenerationJob.select().where(GenerationJob.status == 'running').exists():
            return None
        job = GenerationJob.select().where(GenerationJob.status == 'queued').order_by(GenerationJob.created_at, GenerationJob.id).first()
        if not job:
            return None
        job.status = 'running'
        job.owner = secrets.token_hex(16)
        job.started_at = utcnow()
        job.lease_until = utcnow() + dt.timedelta(seconds=LEASE_SECONDS)
        job.attempts += 1
        job.save()
        return job.id, job.owner, json.loads(job.payload_json)['config']['solverTimeLimit']


def heartbeat(job_id, owner):
    with db.connection_context(), write_transaction():
        if not owned(job_id, owner):
            return False
        GenerationJob.update(lease_until=utcnow() + dt.timedelta(seconds=LEASE_SECONDS)).where(
            (GenerationJob.id == job_id) & (GenerationJob.owner == owner)).execute()
        return True


def fail(job_id, owner, message, status=500):
    with db.connection_context(), write_transaction():
        if owned(job_id, owner):
            GenerationJob.update(status='failed', error=message, error_status=status,
                                 finished_at=utcnow(), owner=None, lease_until=None).where(GenerationJob.id == job_id).execute()


def run_job(job_id, owner):
    """Run in a separate interpreter. Only fenced, current inputs can be saved."""
    import app as web
    from werkzeug.exceptions import HTTPException
    last_checkpoint = 0
    try:
        with db.connection_context():
            job = owned(job_id, owner)
            if not job:
                return
            payload = json.loads(job.payload_json)
            alternative = payload.get('mode') == 'alternative'
            # Alternatives must not inherit assignments they explicitly exclude.
            # Recovery may reuse this job's checkpoint. Optimization compares all
            # runs so a newer, less fair alternative cannot replace the best.
            query = GenerationJob.select().where((GenerationJob.fingerprint == job.fingerprint) &
                                                 GenerationJob.checkpoint_json.is_null(False))
            if alternative:
                query = query.where(GenerationJob.id == job.id)
            candidates = []
            for previous in query:
                candidate = json.loads(previous.checkpoint_json)
                if not {'work','fairnessFloor','fairness','boundarySummary'}.issubset(candidate):
                    continue
                if not alternative and json.loads(previous.payload_json).get('mode') == 'alternative':
                    # Proof over the remaining alternatives is not global proof.
                    candidate.update(status='FEASIBLE', fairnessOptimal=False, optimizationStages=[])
                candidates.append(candidate)
            baseline = max(candidates, key=lambda r: (web.solver_module.schedule_quality(r, payload['employees'], payload['config']),
                                                      r.get('status') == 'OPTIMAL'), default=None)
        cfg = dict(payload['config'])
        cfg['solverWorkers'] = max(1, min(int(cfg.get('solverWorkers', 8)),
                                       int(os.environ.get('GENERATION_SOLVER_WORKERS', '1'))))

        def progress(candidate):
            nonlocal last_checkpoint
            if time.monotonic() - last_checkpoint < 5:
                return
            with db.connection_context(), write_transaction():
                if not owned(job_id, owner):
                    raise RuntimeError('Generation lease lost')
                GenerationJob.update(checkpoint_json=json.dumps(candidate)).where(GenerationJob.id == job_id).execute()
            last_checkpoint = time.monotonic()

        with web.app.app_context():
            kwargs = dict(seed=payload.get('seed'), incumbent=baseline, progress=progress)
            if alternative:
                kwargs['excluded_work'] = payload['excludedWork']
            result = web.solver_module.generate_schedule(payload['employees'], payload['availability'], cfg, **kwargs)
            result['generationMode'] = payload.get('mode', 'optimize')
            with db.connection_context(), write_transaction():
                if not owned(job_id, owner):
                    return
                folder = web.folder_or_404(payload['folder']['id'])
                if folder.created_at.isoformat() + 'Z' != payload['folderVersion']:
                    web.abort(409, 'The original folder was deleted. Generate again in the current folder.')
                web.recheck_generation_inputs(folder, payload['inputVersion'], payload['employeeIds'])
                if result['status'] in ('OPTIMAL', 'FEASIBLE'):
                    result.pop('savedScheduleId', None)
                    result.update(employees=payload['employees'], config=payload['config'], solverWorkersUsed=cfg['solverWorkers'])
                    snapshot = dict(result=result, submissions=payload['submissions'], employeeIds=payload['employeeIds'],
                                    folder=payload['folder'], generationFingerprint=job.fingerprint)
                    saved = SavedSchedule.create(folder=folder, snapshot_json=json.dumps(snapshot))
                    result['savedScheduleId'] = saved.id
                GenerationJob.update(status='completed', result_json=json.dumps(result),
                    checkpoint_json=json.dumps(result) if result['status'] in ('OPTIMAL', 'FEASIBLE') else job.checkpoint_json,
                    owner=None, lease_until=None, finished_at=utcnow()).where(GenerationJob.id == job_id).execute()
    except HTTPException as error:
        fail(job_id, owner, error.description, error.code)
    except Exception as error:
        # Do not expose credentials, SQL, availability or arbitrary exception text.
        web.app.logger.warning('Generation failed (%s)', type(error).__name__)
        fail(job_id, owner, 'Generation could not finish. Your inputs are saved; please try again.')
    finally:
        if not db.is_closed():
            db.close()


def _terminate(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def _consume():
    global _child
    try:
        while not _stop.is_set():
            claimed = claim()
            if not claimed:
                with db.connection_context():
                    pending = GenerationJob.select().where(GenerationJob.status.in_(ACTIVE)).exists()
                if not pending:
                    return
                _stop.wait(5)
                continue
            job_id, owner, limit = claimed
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('generation_worker.py')), job_id, owner],
                                     cwd=str(Path(__file__).parent), env=os.environ.copy())
            _child = child
            deadline = time.monotonic() + limit + 60
            try:
                while child.poll() is None and not _stop.wait(2):
                    if not heartbeat(job_id, owner):
                        break
                    if time.monotonic() > deadline:
                        fail(job_id, owner, 'Generation exceeded its time budget. Please try again.')
                        break
            finally:
                _terminate(child)
                _child = None
            if not _stop.is_set():
                fail(job_id, owner, 'The solver process stopped unexpectedly. Please try again.')
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('Generation runner stopped (%s); resume on next request', type(error).__name__)
    finally:
        if _child:
            _terminate(_child)
            _child = None
        if not db.is_closed():
            db.close()


def wake():
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_consume, name='generation-supervisor', daemon=True)
            _thread.start()


def shutdown():
    _stop.set()
    if _thread and _thread is not threading.current_thread():
        _thread.join(timeout=8)


atexit.register(shutdown)
