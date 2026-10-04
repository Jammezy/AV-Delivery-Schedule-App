"""Explicit fixture adapter for pre-code scheduling/consent regressions.

It performs the public code flow and automatic saving for each fixture
sheet. Security tests use FlaskClient directly, never this adapter.
"""
import os,secrets,uuid
from cryptography.fernet import Fernet
from flask.testing import FlaskClient
os.environ.setdefault('COLLECTION_ENCRYPTION_KEY',Fernet.generate_key().decode())
os.environ.setdefault('COLLECTION_VERIFIER_KEY',secrets.token_hex(32))

class LegacyFixtureClient(FlaskClient):
    def fixture_admin(self):
        import app as web
        login=super().post('/api/admin/login',json={'password':web.ADMIN_PASSWORD})
        return {'Authorization':'Bearer '+login.json['token']}
    def get(self,path,*args,**kwargs):
        if path=='/api/config' or path.startswith('/api/availability/'):
            kwargs.setdefault('headers',self.fixture_admin())
        return super().get(path,*args,**kwargs)
    def post(self,path,*args,**kwargs):
        if path!='/api/availability':return super().post(path,*args,**kwargs)
        data=dict(kwargs.get('json') or {});admin=self.fixture_admin()
        ctx=super().get('/api/submission-context').json
        if not ctx['folder'] or data.get('folderId')!=ctx['folder']['id'] or data.get('revision')!=ctx['revision']:
            # No fake fixture submission for stale destinations.
            from flask import jsonify
            with self.application.app_context():return self.application.response_class(jsonify(error='The collection changed.').get_data(),status=409,mimetype='application/json')
        made=super().post('/api/admin/codes',headers=admin,json={'folderId':ctx['folder']['id']})
        if made.status_code!=201:return made
        unlocked=super().post('/api/collection/unlock',json={'code':made.json['code']})
        if unlocked.status_code!=200:return unlocked
        data['requestId']=uuid.uuid4().hex
        response=super().post(path,json=data,headers={'X-Submission-CSRF':unlocked.json['csrf']})
        if response.status_code!=200:return response
        return response
