"""Drain durable jobs inline for the older API regression fixtures.

Job/lifecycle tests use the unmodified FlaskClient and real subprocesses.
This adapter preserves mocking and concurrent source-deletion regressions.
"""
import json
from flask.testing import FlaskClient


class GenerationFixtureClient(FlaskClient):
    def __init__(self, application, *args, **kwargs):
        application.config['GENERATION_RUNNER_ENABLED'] = False
        application.test_client_class = GenerationFixtureClient
        super().__init__(application, *args, **kwargs)

    def post(self, path, *args, **kwargs):
        response = super().post(path, *args, **kwargs)
        if path != '/api/generate' or response.status_code != 202:
            return response
        from generation_jobs import claim, run_job
        from models import db, GenerationJob
        job_id = response.json['jobId']
        claimed = claim()
        if claimed:
            run_job(claimed[0], claimed[1])
        with db.connection_context():
            job = GenerationJob.get_or_none(GenerationJob.id == job_id)
            if job is None:
                data, status = {'error':'The original folder was deleted.'}, 404
            elif job.status == 'completed':
                data, status = json.loads(job.result_json), 200
            else:
                data, status = {'error':job.error or job.status}, job.error_status or 500
        return self.application.response_class(json.dumps(data), status=status, mimetype='application/json')
