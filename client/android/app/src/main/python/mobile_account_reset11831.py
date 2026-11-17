"""Recoverable personal-data reset; account credentials and login choice survive."""
from __future__ import annotations
from copy import deepcopy
import hashlib,json,os,re,threading,time
from pathlib import Path
from datetime import datetime,timezone
from uuid import UUID,uuid4,uuid5

CONFIRMATION='清空账号内全部数据'
RESET_PATH='/v1/auth/account/reset'

class ResetError(RuntimeError):
    def __init__(self,code,message):super().__init__(message);self.code=code

def _uuid(value):
    if type(value)is not str or str(UUID(value))!=value:raise ValueError('Invalid reset request')
    return value

class ResetJournal:
    def __init__(self,root,vault,endpoint):
        self.root=Path(root)/'account-data-reset11831';self.vault=vault;self.endpoint=endpoint.rstrip('/')
        if self.root.is_symlink():raise ValueError('Invalid reset directory')
        self.root.mkdir(parents=True,exist_ok=True)
        self.purpose='account-data-reset11831/'+hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.path=self.root/(hashlib.sha256(self.endpoint.encode()).hexdigest()+'.sealed')
    def load(self):
        if self.path.is_symlink():raise ValueError('Invalid reset journal')
        if not self.path.exists():return None
        if not self.path.is_file()or self.path.stat().st_size>100000:raise ValueError('Invalid reset journal')
        value=json.loads(str(self.vault.openControl(self.purpose,self.path.read_text(encoding='utf8'))))
        base={'version','actor','endpoint','request_id','phase'}
        if (type(value)is not dict or value.get('version')!=1 or type(value.get('actor'))is not int
            or not 0<value['actor']<2**53 or value.get('endpoint')!=self.endpoint
            or value.get('phase')not in {'submitted','cloud_confirmed','done'}):raise ValueError('Invalid reset journal')
        _uuid(value.get('request_id'))
        for key in ('local_invalidated','local_purged','local_key_retired','reset_reauthentication_required'):
            if key in value and type(value[key])is not bool:raise ValueError('Invalid local reset progress')
        if value.get('local_purged')is True and value.get('local_invalidated')is not True:raise ValueError('Invalid reset progress order')
        if value.get('refresh_request_id')is not None:_uuid(value['refresh_request_id'])
        if value['phase']=='done':
            if set(value)!=base:raise ValueError('Invalid reset receipt')
        elif (set(value)-{'local_invalidated','local_purged','local_key_retired','reset_reauthentication_required','refresh_request_id'}!=base|{'auto_login','prior_session','response_session','reauthentication_required'}
              or type(value['auto_login'])is not bool or type(value['prior_session'])is not dict
              or type(value['reauthentication_required'])is not bool
              or value['prior_session'].get('endpoint')!=self.endpoint
              or value['prior_session'].get('user',{}).get('id')!=value['actor']
              or not re.fullmatch(r'[A-Za-z0-9_-]{43}',str(value['prior_session'].get('token','')))
              or (value['phase']=='cloud_confirmed'and not (type(value['response_session'])is dict
                  or value['reauthentication_required']is True and value['response_session']is None))):
            raise ValueError('Invalid protected reset session')
        return value
    def save(self,value):
        temporary=self.path.with_suffix('.tmp')
        if self.root.is_symlink()or self.path.is_symlink()or temporary.is_symlink():raise ValueError('Invalid reset journal')
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(raw.encode('utf8'))>30000:raise ValueError('Invalid reset journal size')
        sealed=str(self.vault.sealControl(self.purpose,raw))
        if not sealed or sealed==raw:raise ValueError('Unprotected reset journal')
        with temporary.open('w',encoding='utf8')as stream:stream.write(sealed);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.path)

class AccountReset11831:
    def __init__(self,app):
        self.app=app;self.lock=threading.RLock()
        self.journal=ResetJournal(app.root,app._vault,app.bridge._api._base_url)
        self.value=self.journal.load()
    @property
    def pending(self):return self.value if self.value and self.value['phase']!='done' else None
    @staticmethod
    def minimal_user(value):
        return {key:value[key]for key in ('id','username','role_code','account_status')if key in value}|{'display_name':''}
    def bootstrap(self):
        item=self.pending
        if not item:return None
        data=self.app.bridge._api._bootstrap_data()
        return {'ok':True,'result':{**data,'user':self.minimal_user(item['prior_session']['user']),
            'account_data_reset_pending':True,'account_data_reset_local_pending':item['phase']=='cloud_confirmed',
            'local_data_invalidated':item.get('local_invalidated')is True,
            'account_data_reset_reauthentication_required':item.get('reset_reauthentication_required')is True}}
    def _freeze(self):
        app=self.app
        with app._auth_lock:
            app._auth_epoch+=1
            if app._account_generation is not None:app._account_generation.suspend()
            if app.session is not None:app.session.suspend()
            app.bridge._generation+=1;app.bridge._api._user=None;app.bridge._api._reset_services()
            if app.bridge._api._client is not None:app.bridge._api._client.ai149_payload_store=None
        if app._native_hooks is not None:
            invalidate=getattr(app._native_hooks,'invalidateGuestScope',None)
            if callable(invalidate):invalidate()
            app._native_hooks.deactivate()
    def _request(self,item):
        api=self.app.bridge._api
        prior=item.get('prior_session')or{}
        if (type(item.get('actor'))is not int or prior.get('user',{}).get('id')!=item['actor']
            or prior.get('endpoint')!=item.get('endpoint')or item.get('endpoint')!=self.journal.endpoint):
            raise ValueError('Reset account differs')
        # A separate connection never changes the main client's current token.
        client=api._client_factory(api._base_url)
        try:
            client._token=item['prior_session']['token']
            body={'confirmation':CONFIRMATION,'request_id':item['request_id']}
            try:result=client.request('POST',RESET_PATH,json=body)
            except Exception as error:
                if getattr(error,'status_code',None)!=401:raise
                # An expired old bearer is tried first so its permanent reset
                # receipt can finish without any fresh destructive request.
                refresh=item['prior_session'].get('refresh_token')
                if not refresh:
                    self._require_reauthentication(item)
                identifier=item.get('refresh_request_id')or str(uuid5(UUID(item['request_id']),'account-clear-refresh11853'))
                updated={**item,'refresh_request_id':identifier};self.journal.save(updated);item.update(updated);self.value=item
                try:
                    user=client.mobile_refresh(refresh,identifier)
                except Exception as refresh_error:
                    if getattr(refresh_error,'status_code',None)in {401,403}:self._require_reauthentication(item)
                    raise
                self._accept_prior_client(item,client,user)
                result=client.request('POST',RESET_PATH,json=body)
            if (type(result)is not dict or result.get('reset')is not True
                or result.get('local_purge_required')is not True or result.get('request_id')!=item['request_id']
                ):raise ValueError('Reset not confirmed')
            if result.get('reauthentication_required')is True and result.get('session')is None:return None
            if type(result.get('session'))is not dict:raise ValueError('Reset session not confirmed')
            user=client._accept_mobile_session(result['session'])
            if (user.get('id')!=item['actor']or user.get('username')!=item['prior_session']['user'].get('username')):
                raise ValueError('Reset account differs')
            return deepcopy(result['session'])
        finally:client.close()

    def _require_reauthentication(self,item):
        updated={**item,'reset_reauthentication_required':True}
        self.journal.save(updated);item.update(updated);self.value=item
        raise ResetError('account_reset_reauthentication_required','本机资料已清除，请验证原账号后继续云端清理。')

    def _accept_prior_client(self,item,client,user):
        prior=item['prior_session']
        if(user.get('id')!=item['actor']or user.get('username')!=prior['user'].get('username')
            or user.get('role_code')!='member'or user.get('account_status')!='active'):
            raise ResetError('account_changed','请使用正在清理的原账号验证，清理任务仍已保留。')
        value={**prior,'version':3 if client._data_generation else 2,'user':self.minimal_user(user),'token':client._token,
            'lease':deepcopy(client._offline_lease),'recovery':client._recovery_token,
            'refresh_token':client._refresh_token,'refresh_expires_at':client._refresh_expires_at,'refresh_request_id':None}
        if client._data_generation:value['data_generation']=client._data_generation
        else:value.pop('data_generation',None)
        updated={**item,'prior_session':value,'reset_reauthentication_required':False}
        self.journal.save(updated);item.update(updated);self.value=item

    def reauthorize(self,params):
        """Reauthenticate only the pending owner, without opening member data."""
        with self.lock:
            item=self.pending
            password_mode=type(params)is dict and set(params)=={'username','password','_fawnsee_actor_id'}
            phone_mode=type(params)is dict and set(params)=={'phone','challenge_id','code','_fawnsee_actor_id'}
            if(item is None or not(password_mode or phone_mode)or type(params.get('_fawnsee_actor_id'))is not int
                or params.get('_fawnsee_actor_id')!=item['actor']):
                return {'ok':False,'error':{'code':'account_changed','message':'请验证正在清理的原账号。'}}
            if password_mode and(type(params.get('username'))is not str or type(params.get('password'))is not str
                or not 1<=len(params['username'])<=64 or not 1<=len(params['password'])<=256):
                return {'ok':False,'error':{'code':'validation','message':'请填写原账号及密码。'}}
            if phone_mode:
                try:phone_body,_=self.app._phone_body('auth.phone.login',{key:params[key]for key in ('phone','challenge_id','code')})
                except(ValueError,TypeError):return {'ok':False,'error':{'code':'validation','message':'请填写原账号绑定的手机号和六位验证码。'}}
            client=self.app.bridge._api._client_factory(self.journal.endpoint);retained=False;completed=False
            try:
                user=(client.mobile_authenticate('/v1/auth/phone/login',phone_body)if phone_mode else client.login(params['username'],params['password']))
                self._accept_prior_client(item,client,user);retained=True
                if item['phase']=='cloud_confirmed':
                    # Cloud deletion is already proven. Never clear again just
                    # because its returned login envelope has since expired.
                    item={**item,'reauthentication_required':True,'response_session':None}
                    self.journal.save(item);self.value=item
                result=self.run({'confirmation':CONFIRMATION,'_fawnsee_actor_id':item['actor'],'request_id':item['request_id']})
                completed=result.get('ok')is True
                return result
            except Exception as error:
                return {'ok':False,'error':{'code':'account_reset_reauthentication_required',
                    'message':str(error)if isinstance(error,ResetError)else'原账号验证未完成，请核对账号密码后重试；清理任务已保留。'}}
            finally:
                if client._token and(not retained or completed):
                    try:client.logout()
                    except Exception:pass
                client.close()

    def send_phone_code(self,params):
        """Only the existing phone-login challenge, never registration/quick-entry."""
        with self.lock:
            item=self.pending
            if(item is None or type(params)is not dict or set(params)!={'phone','_fawnsee_actor_id'}
                or type(params.get('_fawnsee_actor_id'))is not int or params['_fawnsee_actor_id']!=item['actor']):
                return {'ok':False,'error':{'code':'account_changed','message':'请继续验证正在清理的原账号。'}}
            try:body,_=self.app._phone_body('auth.phone.send',{'phone':params['phone'],'purpose':'login'})
            except(ValueError,TypeError):return {'ok':False,'error':{'code':'validation','message':'请填写原账号绑定的手机号。'}}
            client=self.app.bridge._api._client_factory(self.journal.endpoint)
            try:
                value=client.request('POST','/v1/auth/phone/send',json=body,authenticated=False)
                if(type(value)is not dict or type(value.get('challenge_id'))is not str
                    or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',value['challenge_id'])
                    or type(value.get('expires_in'))is not int or not 1<=value['expires_in']<=900
                    or type(value.get('retry_after'))is not int or not 0<=value['retry_after']<=900
                    or type(value.get('phone_masked'))is not str or not re.fullmatch(r'1[3-9][0-9]\*{4}[0-9]{4}',value['phone_masked'])):
                    raise ValueError('Invalid phone challenge')
                return {'ok':True,'result':{key:value[key]for key in ('challenge_id','expires_in','retry_after','phone_masked')}}
            except Exception as error:
                return {'ok':False,'error':{'code':getattr(error,'code','validation'),'message':'验证码发送未完成，请核对手机号后重试；清理任务仍已保留。'}}
            finally:client.close()
    def _wait_for_old_calls(self,actor):
        app=self.app;deadline=time.monotonic()+10
        with app._active_condition:
            while app._active_requests.get(actor,0):
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Previous account operation still finishing')
                app._active_condition.wait(remaining)
    def _purge(self,item):
        from mobile_local149 import Identity
        from mobile_client_reset import clear_voice_files
        app=self.app;self._wait_for_old_calls(item['actor']);owned=set()
        identity=Identity(item['actor'],item['endpoint'],'reset-confirmed')
        if app.local is not None:
            if not app.local._sync_lock.acquire(timeout=10):raise TimeoutError('Previous sync still finishing')
            try:
                store=app.local.store
                # Payload access is already tombstoned. Only technical UUIDs
                # are needed to remove this account's operation receipts.
                with store._lock:
                    operations={row['operation_id']for row in store._connection.execute('SELECT operation_id FROM local_operations WHERE namespace=?',(identity.namespace,)).fetchall()}
                owned={hashlib.sha256(json.dumps([item['actor'],op],ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()for op in operations}
                store.purge_account(identity)
                app.local._last_error=None;app.local._blocked.pop(identity.namespace,None)
            finally:app.local._sync_lock.release()
        app._avatars.delete(item['actor']);clear_voice_files(app.root)
        with app._intent_lock:
            owned|={k for k,v in app._intents.items()if v.get('actor')==item['actor']}
            owned|={k for k,v in app._finished_actors.items()if v==item['actor']}
            # Pre-metadata legacy hashes cannot safely be assigned to another
            # account. Preserve only those anonymous technical receipts.
            app._intents={k:v for k,v in app._intents.items()if k not in owned}
            app._finished={k:v for k,v in app._finished.items()if k not in owned}
            app._finished_actors={k:v for k,v in app._finished_actors.items()if k not in owned}
            app._save_intents()
        if app._native_hooks is not None:app._native_hooks.clearAccountCaches()
    def _resume_session(self,item):
        app=self.app;api=app.bridge._api
        with app._auth_lock:
            api._ensure_client();client=api._client
            if not item['reauthentication_required']:
                stamp=(item.get('response_session')or{}).get('offline_lease',{}).get('expires_at')
                try:expiry=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                except(ValueError,TypeError,AttributeError):expiry=None
                if expiry is not None and expiry.tzinfo is not None and expiry<=datetime.now(timezone.utc):
                    updated={**item,'reauthentication_required':True,'response_session':None}
                    self.journal.save(updated);item.update(updated);self.value=item
            if item['reauthentication_required']:
                app.session.deactivate_actor(item['actor']);client.clear_session(clear_recovery=True)
                api._user=None;api._reset_services();app._auth_epoch+=1;app.bridge._generation+=1
                return None
            with client._auth_lock,client._lock:
                client.clear_session(clear_recovery=True)
                user=client._accept_mobile_session(item['response_session'])
            if user.get('id')!=item['actor']:raise ValueError('Reset account differs')
            user=self.minimal_user(user)
            client._user=dict(user)
            app.session.remember(client,user,auto_login=item['auto_login'],preserve_other_accounts=True)
            if client._data_generation:
                from mobile_local149 import Identity
                app.local.store.bind_generation(Identity.checked(app.session.identity()),has_reset=True)
            api._user=dict(user);api._reset_services();app._auth_epoch+=1;app.bridge._generation+=1
            return dict(user)

    def _invalidate_local(self,item):
        """Commit invalidation before waiting for the cloud or old workers."""
        from mobile_local149 import Identity
        app=self.app
        if not item.get('local_invalidated'):
            identity=Identity(item['actor'],item['endpoint'],'explicit-clear')
            if app.local is not None:app.local.store.invalidate_account(identity)
            item={**item,'local_invalidated':True,'local_purged':False,'local_key_retired':False}
            self.journal.save(item);self.value=item
        if not item.get('local_key_retired'):
            # The control journal has an independent key and is the only
            # retained credential copy allowed to finish the cloud deletion.
            if app.session is not None:app.session.deactivate_actor(item['actor'])
            app._vault.clearActorKey(item['actor'])
            item={**item,'local_key_retired':True}
            self.journal.save(item);self.value=item
        if not item.get('local_purged'):
            self._purge(item)
            item={**item,'local_purged':True};self.journal.save(item);self.value=item
        return item
    def run(self,params):
        with self.lock:
            app=self.app;item=self.pending;frozen=False
            if type(params)is not dict or set(params)-{'confirmation','request_id','_fawnsee_actor_id'}:
                return {'ok':False,'error':{'code':'validation','message':'清空请求格式无效。'}}
            if params.get('confirmation')!=CONFIRMATION:
                return {'ok':False,'error':{'code':'validation','message':'请先确认清空账号内全部数据。'}}
            actor=params.get('_fawnsee_actor_id')
            if type(actor)is not int or actor<=0:
                return {'ok':False,'error':{'code':'account_changed','message':'当前账号不匹配，请重新打开页面。'}}
            if item:
                if actor!=item['actor']or params.get('request_id',item['request_id'])!=item['request_id']:
                    return {'ok':False,'error':{'code':'account_changed','message':'请继续完成原账号的数据清空。'}}
            else:
                try:identifier=_uuid(params['request_id'])if'request_id'in params else str(uuid4())
                except (ValueError,TypeError,AttributeError):
                    return {'ok':False,'error':{'code':'validation','message':'清空操作编号无效。'}}
                from mobile_api import _REQUEST_IDENTITY149
                with app._auth_lock:
                    if app._erasure_pending is not None:
                        return {'ok':False,'error':{'code':'account_erasure_pending','message':'请先继续完成账号注销。'}}
                    user=app.bridge._api._user or {}
                    if (app.guest and app.guest.facade or user.get('id')!=actor or app.session is None
                        or _REQUEST_IDENTITY149.get()!=(actor,app._auth_epoch)):
                        return {'ok':False,'error':{'code':'not_authenticated','message':'请先登录要清空资料的账号。'}}
                    if self.value and self.value['phase']=='done'and self.value['actor']==actor and self.value['request_id']==identifier:
                        return {'ok':True,'result':{'reset':True,'local_purged':True,'request_id':identifier,'user':dict(user)}}
                    try:
                        snapshot=app.session.snapshot_for_reset()
                        if (snapshot['value'].get('user',{}).get('id')!=actor
                            or snapshot['value'].get('endpoint')!=self.journal.endpoint):
                            return {'ok':False,'error':{'code':'account_changed','message':'当前账号已改变，请重新打开页面。'}}
                        snapshot['value']['user']=self.minimal_user(snapshot['value']['user'])
                        item={'version':1,'actor':actor,'endpoint':self.journal.endpoint,'request_id':identifier,
                            'phase':'submitted','auto_login':snapshot['auto_login'],'prior_session':snapshot['value'],
                            'response_session':None,'reauthentication_required':False}
                        self.journal.save(item);self.value=item
                        self._freeze();frozen=True
                    except Exception:
                        code='account_reset_pending'if self.pending else'local_reset_failed'
                        return {'ok':False,'error':{'code':code,'message':'清空尚未完成，请稍后继续。'}}
            try:
                if not frozen:self._freeze()
                item=self._invalidate_local(item)
                if item['phase']=='submitted':
                    envelope=self._request(item)
                    item={**item,'phase':'cloud_confirmed','response_session':envelope,'reauthentication_required':envelope is None}
                    self.journal.save(item);self.value=item
            except Exception as error:
                code=error.code if isinstance(error,ResetError)else'account_reset_pending'
                return {'ok':False,'error':{'code':code,'message':str(error)if isinstance(error,ResetError)else'本机资料已停用，云端清空结果尚待确认，请联网后继续。'}}
            try:
                self._wait_for_old_calls(item['actor'])
                # All old local payloads were invalidated and removed before
                # the first network wait. Only the fresh session is restored.
                user=self._resume_session(item)
                receipt={key:item[key]for key in ('version','actor','endpoint','request_id')};receipt['phase']='done'
                self.journal.save(receipt);self.value=receipt
                # Completion is durable; background scheduling is best effort.
                try:
                    app._attach_payload_store()
                    if user is not None:app._request_sync()
                    if app._native_hooks is not None:app._native_hooks.remindersChanged()
                except Exception:pass
                return {'ok':True,'result':{'reset':True,'local_purged':True,'request_id':item['request_id'],
                    'user':{**user,'display_name':''}if user is not None else None,
                    **({'reauthentication_required':True}if item['reauthentication_required']else {})}}
            except Exception:
                return {'ok':False,'error':{'code':'local_reset_failed','message':'云端已清空，本机清理尚未完成，请继续完成清空。'}}
