"""Real encrypted disk and HTTP-contract tests; no phone, SMS or live account."""
from __future__ import annotations
import base64
from datetime import datetime,timedelta,timezone
import hashlib,json,sys
from pathlib import Path
from uuid import uuid4
import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'android/app/src/main/python'),str(ROOT/'src')]
from mobile_api import MobileApplication
from mobile_providers import ScopedCloudClient
from mobile_session149 import LocalSession149

class Vault:
    def __init__(self):self.deleted=[];self.fail_delete_once=False
    def seal(self,actor,purpose,text):
        import os
        nonce=os.urandom(12);key=hashlib.sha256(('isolated/'+str(actor)).encode()).digest()
        return base64.b64encode(nonce+AESGCM(key).encrypt(nonce,text.encode(),purpose.encode())).decode()
    def open(self,actor,purpose,text):
        raw=base64.b64decode(text);key=hashlib.sha256(('isolated/'+str(actor)).encode()).digest()
        return AESGCM(key).decrypt(raw[:12],raw[12:],purpose.encode()).decode()
    def sealControl(self,purpose,text):return self.seal(90000001,purpose,text)
    def openControl(self,purpose,text):return self.open(90000001,purpose,text)
    def clearActorKey(self,actor):
        if self.fail_delete_once:self.fail_delete_once=False;raise OSError('isolated key removal failure')
        self.deleted.append(actor)

class AuthServer:
    def __init__(self):
        self.calls=[];self.offline=False;self.refreshes={};self.sequence=0;self.reject_refresh=False
        self.refresh_receipts={};self.lose_refresh_once=False;self.erase_receipts={};self.lose_erase_once=False;self.erased=[]
    @staticmethod
    def user(actor):
        return {'id':actor,'username':'synthetic'+str(actor),'username_normalized':'synthetic'+str(actor),
            'created_at':'2026-10-01T00:00:00+00:00','last_login_at':None,'role_code':'member',
            'account_status':'active','updated_at':'2026-10-01T00:00:00+00:00'}
    def envelope(self,actor):
        self.sequence+=1;refresh='fr1_'+hashlib.sha256(('refresh-'+str(self.sequence)).encode()).hexdigest()[:43]
        self.refreshes[refresh]=actor;now=datetime.now(timezone.utc)
        return {'access_token':str(actor).rjust(43,'T'),'token_type':'bearer','user':self.user(actor),
            'offline_lease':{'version':1,'actor_id':actor,'role_code':'member','server_instance_id':'isolated-phone-server-10516',
                'issued_at':now.isoformat(),'expires_at':(now+timedelta(hours=12)).isoformat()},
            'recovery_token':'X'*24+'.'+'Y'*24,'sync_protocol':2,
            'refresh_token':refresh,'refresh_expires_at':(now+timedelta(days=30)).isoformat()}
    def __call__(self,request):
        body=json.loads(request.content) if request.content else {}
        self.calls.append((request.method,request.url.path,body))
        if self.offline:raise httpx.ConnectError('isolated transport unavailable',request=request)
        path=request.url.path
        if path=='/v1/member/local-sync/account-state':
            actor=int(request.headers['authorization'][-1])
            return httpx.Response(200,json={'actor_id':actor,'server_instance_id':'isolated-phone-server-10516',
                'data_generation':hashlib.sha256(str(actor).encode()).hexdigest(),'has_reset':False})
        if path=='/v1/auth/login':
            if body['password']!='Synthetic!10516':return httpx.Response(401,json={'detail':'authentication'})
            return httpx.Response(200,json=self.envelope(int(body['username'][-1])))
        if path=='/v1/auth/refresh':
            key=(body['refresh_token'],body['request_id'])
            if key in self.refresh_receipts:return httpx.Response(200,json=self.refresh_receipts[key])
            actor=self.refreshes.pop(body['refresh_token'],None)
            if self.reject_refresh or actor is None:return httpx.Response(401,json={'detail':'authentication'})
            envelope=self.envelope(actor);self.refresh_receipts[key]=envelope
            if self.lose_refresh_once:
                self.lose_refresh_once=False;raise httpx.ReadTimeout('isolated lost rotation response',request=request)
            return httpx.Response(200,json=envelope)
        if path in {'/v1/auth/register/complete','/v1/auth/phone/login'}:
            if body['code']!='123456':return httpx.Response(400,json={'detail':'validation'})
            return httpx.Response(200,json=self.envelope(2))
        if path=='/v1/auth/phone/send':
            if set(body)!={'phone','purpose'}:return httpx.Response(422,json={'detail':'validation'})
            return httpx.Response(200,json={'challenge_id':str(uuid4()),'expires_in':300,'retry_after':60,'phone_masked':'138****8000'})
        if path=='/v1/auth/phone':return httpx.Response(200,json={'phone':'13800138000','verified_at':'2026-10-29T10:00:00Z','developer_exempt':False})
        if path=='/v1/auth/phone/bind':return httpx.Response(200,json={'phone':body['phone'],'verified_at':'2026-10-29T10:00:00Z','developer_exempt':False})
        if path=='/v1/auth/logout':return httpx.Response(200,json={'signed_out':True})
        if path=='/v1/auth/account/erase':
            key=(request.headers.get('authorization'),body['request_id'])
            if key in self.erase_receipts:return httpx.Response(200,json=self.erase_receipts[key])
            if body['current_password']!='Synthetic!10516':return httpx.Response(401,json={'detail':'authentication'})
            actor=int(request.headers['authorization'][-1]);self.erased.append(actor)
            result={'erased':True,'local_purge_required':True};self.erase_receipts[key]=result
            if self.lose_erase_once:
                self.lose_erase_once=False;raise httpx.ReadTimeout('isolated lost erasure response',request=request)
            return httpx.Response(200,json=result)
        if path=='/v1/auth/me':return httpx.Response(401,json={'detail':'authentication'})
        return httpx.Response(404,json={'detail':'not_found'})

@pytest.fixture
def fixture(tmp_path):
    server=AuthServer();vault=Vault()
    factory=lambda endpoint:ScopedCloudClient(endpoint,transport=httpx.MockTransport(server))
    def app():return MobileApplication(tmp_path,client_factory=factory,base_url='https://isolated.example.org',vault=vault)
    return app,server,vault,tmp_path

def login(app,actor=1,**extra):
    return app.handle('auth.login',{'username':'synthetic'+str(actor),'password':'Synthetic!10516',**extra})

def close(app):
    if app.local:app.local.store.close()

def test_default_auto_login_is_encrypted_and_refreshes_on_restart(fixture):
    factory,server,vault,root=fixture;app=factory()
    result=login(app);assert result['ok'];assert set(result['result'])==set(server.user(1))
    assert 'access_token' not in json.dumps(result) and 'refresh_token' not in json.dumps(result)
    old=app.bridge._api._client._refresh_token;assert old
    raw=b''.join(p.read_bytes() for p in (root/'local149-session').iterdir())
    assert old.encode() not in raw and b'Synthetic!10516' not in raw
    close(app);restored=factory()
    assert restored.handle('app.bootstrap',{})['result']['user']['id']==1
    assert restored.bridge._api._client._refresh_token!=old
    assert [path for _,path,_ in server.calls[-2:]]==['/v1/auth/refresh','/v1/member/local-sync/account-state']
    close(restored)

def test_unchecked_keeps_this_session_but_requires_login_after_restart(fixture):
    factory,server,vault,root=fixture;app=factory()
    assert login(app,auto_login=False)['ok'];assert app._local_identity()['authorized']
    assert app.handle('app.bootstrap',{})['result']['user']['id']==1
    assert not list((root/'local149-session').iterdir())
    assert all('auto_login' not in payload for _,_,payload in server.calls)
    close(app);restarted=factory();assert restarted.handle('app.bootstrap',{})['result']['user'] is None;close(restarted)

def test_successful_second_account_replaces_previous_credentials(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];assert login(app,2)['ok']
    sealed=list((root/'local149-session').glob('*.sealed'));assert len(sealed)==1 and sealed[0].name.endswith('-2.sealed')
    close(app);restored=factory();assert restored.session.user()['id']==2;close(restored)

def test_unchecked_success_forgets_old_remembered_account(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];assert login(app,2,auto_login=False)['ok']
    assert not list((root/'local149-session').iterdir());close(app)
    restarted=factory();assert restarted.session.user() is None;close(restarted)

def test_failed_login_keeps_last_successful_disk_credentials(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok']
    files={p.name:p.read_bytes() for p in (root/'local149-session').iterdir()}
    failed=app.handle('auth.login',{'username':'synthetic2','password':'wrong','auto_login':False})
    assert not failed['ok'];assert app.handle('app.bootstrap',{})['result']['user'] is None
    assert files=={p.name:p.read_bytes() for p in (root/'local149-session').iterdir()};close(app)
    restored=factory();assert restored.session.user()['id']==1;close(restored)

def test_logout_erases_remembered_bearer_refresh_and_recovery(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok']
    assert app.handle('auth.logout',{})['ok'];assert not list((root/'local149-session').iterdir())
    client=app.bridge._api._client
    assert client._token is None and client._refresh_token is None and client._recovery_token is None
    close(app);restarted=factory();assert restarted.session.user() is None;close(restarted)

def test_expired_offline_lease_does_not_prevent_online_cross_day_refresh(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok']
    old=datetime.now(timezone.utc)-timedelta(days=2)
    app.session.value['lease'].update(issued_at=old.isoformat(),expires_at=(old+timedelta(hours=12)).isoformat())
    app.session._save();close(app)
    restarted=factory();assert restarted.session.user()['id']==1;close(restarted)

def test_network_failure_only_restores_still_valid_offline_authority(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];close(app);server.offline=True
    restored=factory();assert restored.session.user()['id']==1;close(restored)
    sealed=next((root/'local149-session').glob('*.sealed'))
    # Move the saved authorization interval into the past without changing its length.
    value=json.loads(vault.open(1,restored.session._purpose(),sealed.read_text()))
    old=datetime.now(timezone.utc)-timedelta(days=2)
    value['lease'].update(issued_at=old.isoformat(),expires_at=(old+timedelta(hours=12)).isoformat())
    sealed.write_text(vault.seal(1,restored.session._purpose(),json.dumps(value)))
    denied=factory();assert denied.session.user() is None;close(denied)

def test_refresh_rejection_never_falls_back_to_offline_session(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];close(app);server.reject_refresh=True
    denied=factory();assert denied.session.user() is None;assert not list((root/'local149-session').iterdir());close(denied)

def test_lost_refresh_response_reuses_original_request_id_on_next_restart(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];close(app)
    server.lose_refresh_once=True
    offline=factory();assert offline.session.user()['id']==1;close(offline)
    original=next(payload for _,path,payload in server.calls if path=='/v1/auth/refresh')
    assert offline.session.value['refresh_request_id']==original['request_id']
    restored=factory();assert restored.session.user()['id']==1
    refresh=[body for _,path,body in server.calls if path=='/v1/auth/refresh']
    assert refresh[0]==refresh[1] and len(server.refresh_receipts)==1
    assert restored.session.value['refresh_request_id'] is None;close(restored)

def erase(app,**extra):
    return app.handle('account.erase',{'current_password':'Synthetic!10516','confirmation':'删除账号及全部数据',**extra})

def test_confirmed_erase_purges_own_history_credentials_voice_and_key_only(fixture):
    from mobile_local149 import Identity
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];identity=app.local.identity()
    app.local.store.put(identity,'draft','input',{'text':'isolated private draft'})
    other=Identity(2,identity.endpoint,'other-session');app.local.store.put(other,'draft','input',{'text':'other member retained'})
    voice=root/'voice-capture';voice.mkdir();(voice/'capture.wav').write_bytes(b'isolated private voice')
    avatars=root/'member-avatars';avatars.mkdir();(avatars/'1.sealed').write_text('isolated private avatar');(avatars/'2.sealed').write_text('other avatar')
    result=erase(app);assert result=={'ok':True,'result':{'erased':True,'local_purged':True}}
    assert server.erased==[1] and vault.deleted==[1]
    assert app.local.store.get(identity,'draft','input') is None
    assert app.local.store.get(other,'draft','input')['text']=='other member retained'
    assert not voice.exists() and not (avatars/'1.sealed').exists() and (avatars/'2.sealed').exists()
    assert not list((root/'local149-session').iterdir()) and app._erasure_pending is None
    assert app.handle('app.bootstrap',{})['result']['user'] is None
    assert app.handle('auth.logout',{})['ok'];close(app)

def test_lost_erasure_response_recovers_receipt_on_restart_without_refresh(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];identity=app.local.identity()
    app.local.store.put(identity,'draft','input',{'text':'private before confirmed erase'})
    server.lose_erase_once=True;reply=erase(app);assert reply['error']['code']=='account_erasure_pending'
    assert app.local.store.get(identity,'draft','input') is not None and not vault.deleted
    saved=app._erasure_pending.copy();assert app.handle('app.bootstrap',{})['result']['account_erasure_pending']
    assert not login(app,2)['ok'];raw=next((root/'client-erasure10516').glob('*.sealed')).read_text()
    assert saved['bearer'] not in raw and saved['request_id'] not in raw and 'Synthetic!10516' not in raw
    close(app);before=len(server.calls);restarted=factory()
    assert restarted._erasure_pending is None and vault.deleted==[1]
    recovery=server.calls[before:];assert [path for _,path,_ in recovery]==['/v1/auth/account/erase']
    assert recovery[0][2]['request_id']==saved['request_id'] and server.erased==[1]
    assert restarted.local.store.get(identity,'draft','input') is None;close(restarted)

def test_confirmed_cloud_erase_local_failure_retries_locally_without_resubmitting(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];vault.fail_delete_once=True
    reply=erase(app);assert reply['error']['code']=='local_erasure_failed'
    assert app._erasure_pending['phase']=='cloud_confirmed'
    assert app.handle('app.bootstrap',{})['result']['account_erasure_local_pending'];close(app)
    before=len(server.calls);restarted=factory()
    assert restarted._erasure_pending is None and len(server.calls)==before and vault.deleted==[1]
    close(restarted)

def test_failed_erase_never_claims_success_or_discards_retry_identifier(fixture):
    factory,server,vault,root=fixture;app=factory();assert login(app)['ok'];server.offline=True
    reply=erase(app);assert reply['error']['code']=='account_erasure_pending';identifier=app._erasure_pending['request_id']
    reply=erase(app,request_id=str(uuid4()));assert reply['error']['code']=='account_erasure_pending'
    assert app._erasure_pending['request_id']==identifier and not server.erased and not vault.deleted
    server.offline=False;assert erase(app)['result']['local_purged'];assert server.erased==[1];close(app)

@pytest.mark.parametrize('action,body',[
    ('auth.phone.login',{'phone':'13800138000','challenge_id':'challenge-123456789','code':'123456'}),
    ('auth.register.complete',{'username':'synthetic2','password':'Synthetic!10516','phone':'+8613800138000','challenge_id':'challenge-123456789','code':'123456'}),
])
def test_phone_auth_keeps_secrets_native_and_auto_login_out_of_wire(fixture,action,body):
    factory,server,vault,root=fixture;app=factory();reply=app.handle(action,{**body,'auto_login':False})
    assert reply['ok'] and reply['result']['id']==2
    assert app.session.user()['id']==2 and not list((root/'local149-session').iterdir())
    assert [data for _,path,data in server.calls if path.startswith('/v1/auth/')][-1]==body
    assert not {'code','refresh_token','access_token'}&set(reply['result']);close(app)

def test_sms_send_and_own_phone_binding_have_fixed_contract(fixture):
    factory,server,vault,root=fixture;app=factory()
    reply=app.handle('auth.phone.send',{'phone':'13800138000','purpose':'login'})
    assert reply['ok'] and set(reply['result'])=={'challenge_id','expires_in','retry_after','phone_masked'}
    assert login(app)['ok'];assert app.handle('auth.phone.get',{})['result']['phone']=='13800138000'
    assert app.handle('auth.phone.bind',{'phone':'13900139000','challenge_id':'challenge-123456789','code':'123456','current_password':'Synthetic!10516'})['ok']
    close(app)

@pytest.mark.parametrize('include_ui_marker',[False,True])
def test_sms_resend_keeps_ui_marker_out_of_strict_server_payload(fixture,include_ui_marker):
    factory,server,vault,root=fixture;app=factory()
    first=app.handle('auth.phone.send',{'phone':'13800138000','purpose':'login'})
    assert first['ok']
    body={'phone':'13800138000','purpose':'login'}
    if include_ui_marker:body['current_challenge_id']=first['result']['challenge_id']
    second=app.handle('auth.phone.send',body)
    assert second['ok'] and second['result']['challenge_id']!=first['result']['challenge_id']
    sends=[row for row in server.calls if row[1]=='/v1/auth/phone/send']
    assert len(sends)==2 and all(row[2]=={'phone':'13800138000','purpose':'login'} for row in sends)
    close(app)

@pytest.mark.parametrize('action,body',[
    ('auth.login',{'username':'synthetic1','password':'Synthetic!10516','auto_login':'false'}),
    ('auth.phone.send',{'phone':'13800138000','purpose':'admin'}),
    ('auth.phone.send',{'phone':'13800138000','purpose':'login','base_url':'https://evil.invalid'}),
    ('auth.phone.send',{'phone':'13800138000','purpose':'login','auto_login':True}),
    ('auth.phone.login',{'phone':'13800138000','challenge_id':'challenge-123456789','code':'12345'}),
    ('auth.phone.login',{'phone':'not-phone','challenge_id':'challenge-123456789','code':'123456'}),
    ('auth.phone.get',{'user_id':2}),
])
def test_bad_auth_inputs_never_reach_cloud(fixture,action,body):
    factory,server,vault,root=fixture;app=factory();assert not app.handle(action,body)['ok'];assert not server.calls;close(app)
