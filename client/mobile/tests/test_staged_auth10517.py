"""Legacy facade registration on an explicitly staged server; isolated HTTP/disk only."""
from pathlib import Path
import json,sys
import httpx
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'android/app/src/main/python'),str(ROOT/'src'),str(ROOT/'mobile/tests')]
from mobile_api import MobileApplication
from mobile_providers import ScopedCloudClient
from test_phone_auth10516 import AuthServer,Vault,close,login

class StageServer(AuthServer):
    def __init__(self,*,legacy=True):
        super().__init__();self.legacy=legacy;self.fail_login=False
    def __call__(self,request):
        if request.url.path=='/v1/auth/register':
            body=json.loads(request.content);self.calls.append((request.method,request.url.path,body))
            if not self.legacy:return httpx.Response(422,json={'detail':'validation'})
            return httpx.Response(201,json=self.user(2))
        if request.url.path=='/v1/auth/login' and self.fail_login:
            self.calls.append((request.method,request.url.path,json.loads(request.content)))
            raise httpx.ConnectError('isolated login transport failure',request=request)
        return super().__call__(request)

def app_for(root,server):
    return MobileApplication(root,base_url='https://staged.example.org',vault=Vault(),
        client_factory=lambda endpoint:ScopedCloudClient(endpoint,transport=httpx.MockTransport(server)))
def register(app,**extra):
    return app.handle('auth.register',{'username':'synthetic2','password':'Synthetic!10516',**extra})

def test_legacy_registration_establishes_authenticated_native_user_and_remembers_default(tmp_path):
    server=StageServer();app=app_for(tmp_path,server)
    try:
        reply=register(app);assert reply['ok'] and reply['result']['id']==2
        assert [path for _,path,_ in server.calls]==['/v1/auth/register','/v1/auth/login','/v1/member/local-sync/account-state']
        assert app.handle('app.bootstrap',{})['result']['user']['id']==2
        assert app.local.identity().actor_id==2 and app.session.persistent
        assert all('auto_login' not in body for _,_,body in server.calls)
    finally:close(app)
    restarted=app_for(tmp_path,server)
    try:assert restarted.session.user()['id']==2
    finally:close(restarted)

def test_legacy_registration_honors_unchecked_auto_login(tmp_path):
    server=StageServer();app=app_for(tmp_path,server)
    try:
        assert register(app,auto_login=False)['ok']
        assert app.local.identity().actor_id==2 and app.session.user()['id']==2
        assert not app.session.persistent and not app.session._pointer().exists()
    finally:close(app)
    restarted=app_for(tmp_path,server)
    try:assert restarted.handle('app.bootstrap',{})['result']['user'] is None
    finally:close(restarted)

def test_full_server_rejection_never_triggers_client_side_bypass_login(tmp_path):
    server=StageServer(legacy=False);app=app_for(tmp_path,server)
    try:
        assert not register(app)['ok']
        assert [path for _,path,_ in server.calls]==['/v1/auth/register']
        assert app.handle('app.bootstrap',{})['result']['user'] is None
        assert not app.session._pointer().exists()
    finally:close(app)

def test_created_account_login_failure_never_claims_session_or_forgets_last_success(tmp_path):
    server=StageServer();app=app_for(tmp_path,server)
    try:
        assert login(app,1)['ok'];saved={path.name:path.read_bytes() for path in app.session.root.iterdir()}
        server.fail_login=True;reply=register(app,auto_login=False)
        assert not reply['ok']
        assert reply['error']=={'code':'registration_login_required','message':'账号已创建，请使用账号密码登录。'}
        assert app.handle('app.bootstrap',{})['result']['user'] is None
        assert saved=={path.name:path.read_bytes() for path in app.session.root.iterdir()}
    finally:close(app)
