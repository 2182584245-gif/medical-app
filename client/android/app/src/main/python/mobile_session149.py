"""Device-bound member session persistence; no password or broad offline authority."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib,json,os,re,threading
from pathlib import Path
from uuid import UUID,uuid4

from ollama_chat_app.services.offline_access import validate_lease, OfflineAccessError


class LocalSession149:
    def __init__(self,root,vault,endpoint,*,clock=None):
        self.root=Path(root)/'local149-session';self.root.mkdir(parents=True,exist_ok=True)
        if self.root.is_symlink():raise ValueError('Invalid private session directory')
        self.vault,self.endpoint=vault,endpoint.rstrip('/')
        self.realm=hashlib.sha256(self.endpoint.encode()).hexdigest()
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.lock=threading.RLock();self.value=None;self.serial=0;self._persisted_clock=None
        self.persistent=False

    def _file(self,actor):return self.root/(self.realm+'-'+str(actor)+'.sealed')
    def _pointer(self):return self.root/(self.realm+'.active')
    def _purpose(self):return 'mobile149-session/'+self.realm

    @staticmethod
    def _atomic(path,value):
        if path.is_symlink():raise ValueError('Invalid private session file')
        temporary=path.with_suffix(path.suffix+'.tmp')
        if temporary.is_symlink():raise ValueError('Invalid private session file')
        with temporary.open('w',encoding='utf8') as stream:
            stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)

    def _save(self):
        if not self.persistent:return
        actor=self.value['user']['id']
        sealed=str(self.vault.seal(actor,self._purpose(),json.dumps(self.value,ensure_ascii=False,allow_nan=False,separators=(',',':'))))
        self._atomic(self._file(actor),sealed)
        self._persisted_clock=datetime.fromisoformat(self.value['last_clock'])

    def _forget_disk(self,except_actor=None):
        # Remove the pointer first: even an interrupted cleanup cannot restore
        # credentials that the member explicitly asked this device to forget.
        if except_actor is None:
            for path in (self._pointer(),self._pointer().with_suffix('.active.tmp')):
                if path.is_symlink():raise ValueError('Invalid private session file')
                path.unlink(missing_ok=True)
        keep=self._file(except_actor) if except_actor is not None else None
        for path in self.root.glob(self.realm+'-*'):
            if not re.fullmatch(re.escape(self.realm)+r'-[1-9][0-9]{0,15}\.sealed(?:\.tmp)?',path.name):continue
            if path==keep:continue
            if path.is_symlink() or not path.is_file():raise ValueError('Invalid private session file')
            path.unlink()

    def _forget_actor_disk(self,actor):
        for path in (self._file(actor),self._file(actor).with_suffix('.sealed.tmp')):
            if path.is_symlink():raise ValueError('Invalid private session file')
            path.unlink(missing_ok=True)
        pointer=self._pointer()
        if pointer.is_symlink():raise ValueError('Invalid private session file')
        if pointer.is_file()and pointer.read_text(encoding='utf8')==str(actor):pointer.unlink()

    def remember(self,client,user,*,auto_login=True,preserve_other_accounts=False):
        with self.lock:
            if type(auto_login) is not bool:raise ValueError('Invalid automatic login setting')
            if (type(user) is not dict or type(user.get('id')) is not int or user['id']<=0
                    or user.get('role_code')!='member' or user.get('account_status')!='active'):
                raise ValueError('Only validated member sessions may persist')
            with client._lock:
                token=client._token;lease=client._offline_lease;recovery=client._recovery_token
                refresh=getattr(client,'_refresh_token',None);refresh_expiry=getattr(client,'_refresh_expires_at',None)
            if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
                raise ValueError('Validated bearer required')
            lease=validate_lease(lease,actor_id=user['id'],now=self.clock())
            self.serial+=1
            self.value={'version':2,'endpoint':self.endpoint,'user':dict(user),'token':token,
                'lease':lease,'recovery':recovery,'last_clock':self.clock().isoformat(),
                'refresh_token':refresh,'refresh_expires_at':refresh_expiry,'refresh_request_id':None}
            generation=getattr(client,'_data_generation',None)
            if generation is not None:
                if not re.fullmatch(r'[a-f0-9]{64}',generation):raise ValueError('Invalid account generation')
                self.value.update(version=3,data_generation=generation)
            self.persistent=auto_login
            if auto_login:
                self._save();self._atomic(self._pointer(),str(user['id']))
                if not preserve_other_accounts:self._forget_disk(except_actor=user['id'])
            elif not preserve_other_accounts:self._forget_disk()
            else:self._forget_actor_disk(user['id'])
            return self.identity()

    def restore(self,api):
        with self.lock:
            pointer=self._pointer()
            if not pointer.exists():return False
            if pointer.is_symlink() or pointer.stat().st_size>32:return False
            actor_text=pointer.read_text(encoding='utf8')
            if not re.fullmatch(r'[1-9][0-9]{0,15}',actor_text):return False
            actor=int(actor_text);path=self._file(actor)
            if not path.is_file() or path.is_symlink() or path.stat().st_size>128000:return False
            try:
                value=json.loads(str(self.vault.open(actor,self._purpose(),path.read_text(encoding='utf8'))))
                fields={'version','endpoint','user','token','lease','recovery','last_clock'}
                if value.get('version')in {2,3}:fields|={'refresh_token','refresh_expires_at','refresh_request_id'}
                if value.get('version')==3:fields.add('data_generation')
                if (set(value)!=fields
                        or value['version'] not in {1,2,3} or value['endpoint']!=self.endpoint
                        or type(value['user']) is not dict or value['user'].get('id')!=actor
                        or value['user'].get('role_code')!='member' or value['user'].get('account_status')!='active'
                        or not re.fullmatch(r'[A-Za-z0-9_-]{43}',value['token'])):return False
                if value['version']==3 and(type(value['data_generation'])is not str or not re.fullmatch(r'[a-f0-9]{64}',value['data_generation'])):return False
                api._ensure_client();client=api._client
                refresh=value.get('refresh_token')
                if refresh is not None:
                    if (type(refresh) is not str or not re.fullmatch(r'fr1_[A-Za-z0-9_-]{43}',refresh)
                            or type(value.get('refresh_expires_at')) is not str):return False
                    expiry=datetime.fromisoformat(value['refresh_expires_at'].replace('Z','+00:00'))
                    if expiry.tzinfo is None:return False
                    try:
                        request_id=value.get('refresh_request_id') or str(uuid4())
                        if str(UUID(request_id))!=request_id:raise ValueError('Invalid refresh operation')
                        value['refresh_request_id']=request_id
                        self.value=value;self.persistent=True;self._save()
                        user=client.mobile_refresh(refresh,request_id)
                        if user.get('id')!=actor:raise ValueError('Account mismatch')
                        self.remember(client,user,auto_login=True)
                        api._user=dict(user);api._reset_services()
                        return True
                    except Exception as error:
                        # Only transport unavailability may use the independently
                        # bounded offline lease. Revocation/protocol errors never
                        # fall back to a previously valid local identity.
                        if getattr(error,'code',None) not in {'network','timeout','unavailable','service_unavailable'}:
                            client.clear_session();self.deactivate();return False
                now=self.clock()
                if now<datetime.fromisoformat(value['last_clock']):return False
                validate_lease(value['lease'],actor_id=actor,now=now)
                with client._lock:
                    client._session_serial+=1;client._token=value['token'];client._user=dict(value['user'])
                    client._offline_lease=dict(value['lease']);client._offline_session=False
                    client._recovery_token=value['recovery'];client.offline_opt_in=True
                    client._refresh_token=value.get('refresh_token');client._refresh_expires_at=value.get('refresh_expires_at')
                    client._data_generation=value.get('data_generation')
                api._user=dict(value['user']);api._reset_services()
                value['last_clock']=now.isoformat();self.value=value;self.persistent=True;self.serial+=1;self._save()
                return True
            except Exception:
                # A missing or invalid device key locks the session. Keep the
                # sealed account data so a successful login can recover it.
                self.value=None;self.persistent=False
                return False

    def identity(self):
        with self.lock:
            value=self.value
            if value is None:return {'authorized':False,'epoch':self.serial,'endpoint':self.endpoint}
            now=self.clock();valid=True
            try:
                if now<datetime.fromisoformat(value['last_clock']):raise OfflineAccessError('clock')
                validate_lease(value['lease'],actor_id=value['user']['id'],now=now)
            except (ValueError,KeyError,OfflineAccessError):valid=False
            if now>datetime.fromisoformat(value['last_clock']):
                value['last_clock']=now.isoformat()
                # Keep the in-process high-water mark on every read while
                # bounding disk fsync to once a minute. Expiry is persisted
                # immediately so clock rollback cannot revive an expired lease.
                if not valid or self._persisted_clock is None or (now-self._persisted_clock).total_seconds()>=60:
                    self._save()
            return {'actor_id':value['user']['id'],'endpoint':self.endpoint,
                'server_instance_id':value['lease']['server_instance_id'],'authorized':valid,
                'lease_expires_at':value['lease']['expires_at'],'epoch':self.serial,
                'data_generation':value.get('data_generation','')}

    def bind_data_generation(self,generation):
        with self.lock:
            if self.value is None or not re.fullmatch(r'[a-f0-9]{64}',generation):raise ValueError('Validated account generation required')
            if self.value.get('data_generation')!=generation:
                for key in ('refresh_token','refresh_expires_at','refresh_request_id'):self.value.setdefault(key,None)
                self.value.update(version=3,data_generation=generation)
                self.serial+=1;self._save()

    def user(self):
        with self.lock:return dict(self.value['user']) if self.value and self.identity()['authorized'] else None

    def suspend(self):
        """End the in-process identity without changing the last successful login."""
        with self.lock:
            self.serial+=1;self.value=None;self.persistent=False

    def snapshot_for_reset(self):
        """Only the protected reset journal receives this temporary credential snapshot."""
        with self.lock:
            if self.value is None or self.identity().get('authorized') is not True:
                raise ValueError('Validated member session required')
            return {'value':json.loads(json.dumps(self.value,ensure_ascii=False)),
                    'auto_login':bool(self.persistent)}

    def deactivate(self):
        with self.lock:
            self.suspend();self._forget_disk()

    def deactivate_actor(self,actor):
        """Forget only the resetting account after a terminal reauthentication receipt."""
        with self.lock:
            self.suspend();self._forget_actor_disk(actor)

