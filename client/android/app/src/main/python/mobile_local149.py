"""Android account-scoped durable storage; business payloads are always sealed.

The injected vault is Android Keystore in the app and an authenticated cipher in
host tests. SQLite metadata contains hashes, fixed domain names and operation
UUIDs only. Clearing downloaded files never touches pending business changes.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

MAX_VALUE_BYTES = 32 * 1024 * 1024
CACHE_BUDGET_BYTES = 200 * 1024 * 1024
MAX_PENDING_OPERATIONS = 10000
MAX_DRAFT_BYTES = 64 * 1024 * 1024
PENDING_STATUSES = frozenset({'queued', 'uncertain', 'conflict', 'rejected'})


class LocalDataError(RuntimeError):
    def __init__(self, code='local_storage', message='本机数据暂时无法读取，请保留应用数据后重试。'):
        super().__init__(message); self.code = code


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':'), default=_json_default)


def _json_default(value):
    if isinstance(value, datetime):return value.isoformat()
    if hasattr(value, '__dataclass_fields__'):
        from dataclasses import asdict
        return asdict(value)
    raise TypeError('Unsupported local value')


def digest(value):return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


@dataclass(frozen=True)
class Identity:
    actor_id: int
    endpoint: str
    epoch: str
    server_instance_id: str = ''
    data_generation: str = ''

    @property
    def namespace(self):return digest([self.endpoint, self.actor_id])

    @classmethod
    def checked(cls, value, *, now=None):
        if isinstance(value,dict)and value.get('account_verification_required')is True:
            raise LocalDataError('account_verification_required','账号资料尚未核验，请联网后重试。')
        if not isinstance(value, dict) or value.get('authorized') is not True:
            raise LocalDataError('not_authenticated', '请先登录会员账号。')
        if value.get('role_code','member')!='member':
            raise LocalDataError('permission','请使用会员账号登录。')
        actor=value.get('actor_id');endpoint=value.get('endpoint');epoch=value.get('epoch')
        if type(actor) is not int or not 0<actor<2**53 or not isinstance(endpoint,str):
            raise LocalDataError('account_changed', '当前账号资料无效，请重新登录。')
        parsed=urlsplit(endpoint)
        if parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LocalDataError('account_changed', '当前服务资料无效，请重新登录。')
        if parsed.scheme=='http' and parsed.hostname not in {'localhost','127.0.0.1','testserver'}:
            raise LocalDataError('account_changed', '当前服务连接无效。')
        if type(epoch) not in {str,int} or not str(epoch) or len(str(epoch))>200:
            raise LocalDataError('account_changed', '当前登录状态已变化，请重新打开页面。')
        expiry=value.get('lease_expires_at')
        if expiry is not None:
            try:
                if type(expiry) in {float,int}:stamp=float(expiry)
                else:
                    date=datetime.fromisoformat(str(expiry).replace('Z','+00:00'))
                    if date.tzinfo is None:raise ValueError
                    stamp=date.timestamp()
                if not math.isfinite(stamp) or stamp <= (time.time() if now is None else now):raise ValueError
            except (ValueError,TypeError,OverflowError):
                raise LocalDataError('offline_expired','离线使用授权已到期，请联网登录。') from None
        server=value.get('server_instance_id') or ''
        if not isinstance(server,str) or len(server)>200:
            raise LocalDataError('account_changed','当前服务资料无效。')
        generation=value.get('data_generation') or ''
        if not isinstance(generation,str) or generation and not re.fullmatch(r'[a-f0-9]{64}',generation):
            raise LocalDataError('account_verification_required','账号资料尚未核验，请联网后重试。')
        return cls(actor,endpoint.rstrip('/'),str(epoch),server,generation)


def account_guard(method):
    """Serialize the generation check with the entire local read/write."""
    @wraps(method)
    def guarded(self,identity,*args,**kwargs):
        with self._lock:
            self._check_generation(identity)
            return method(self,identity,*args,**kwargs)
    return guarded


class LocalStore149:
    def __init__(self, root, vault, *, cache_budget=CACHE_BUDGET_BYTES, clock=time.time):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'member-local149.sqlite3';self.vault=vault;self.clock=clock
        self.cache_budget=max(1024,min(int(cache_budget),1024*1024*1024))
        self._lock=threading.RLock()
        self._connection=sqlite3.connect(self.path,timeout=10,check_same_thread=False)
        self._connection.row_factory=sqlite3.Row
        self._connection.execute('PRAGMA journal_mode=WAL');self._connection.execute('PRAGMA synchronous=FULL')
        self._connection.execute('PRAGMA secure_delete=ON');self._connection.execute('PRAGMA busy_timeout=10000')
        self._connection.execute('PRAGMA temp_store=MEMORY')
        with self._db() as conn:
            conn.executescript('''
              CREATE TABLE IF NOT EXISTS local_values(
                namespace TEXT NOT NULL,key_hash TEXT NOT NULL,kind TEXT NOT NULL,
                updated_at REAL NOT NULL,accessed_at REAL NOT NULL,size_bytes INTEGER NOT NULL,
                payload TEXT NOT NULL,PRIMARY KEY(namespace,key_hash,kind));
              CREATE INDEX IF NOT EXISTS local_cache_age ON local_values(namespace,kind,accessed_at);
              CREATE TABLE IF NOT EXISTS local_cache_tags(namespace TEXT NOT NULL,key_hash TEXT NOT NULL,tag TEXT NOT NULL,PRIMARY KEY(namespace,key_hash));
              CREATE TABLE IF NOT EXISTS local_operations(
                sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,namespace TEXT NOT NULL,
                operation_id TEXT NOT NULL,action TEXT NOT NULL,resource TEXT NOT NULL,
                status TEXT NOT NULL,attempted INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,payload TEXT NOT NULL,
                UNIQUE(namespace,operation_id));
              CREATE INDEX IF NOT EXISTS local_operations_pending ON local_operations(namespace,status,sequence_no);
              CREATE TABLE IF NOT EXISTS local_account_generations(
                namespace TEXT PRIMARY KEY,generation TEXT NOT NULL,status TEXT NOT NULL);
            ''')

    @contextmanager
    def _db(self):
        with self._lock:
            with self._connection:yield self._connection

    def close(self):
        with self._lock:self._connection.close()

    def account_generation(self,identity):
        """Control metadata only, deliberately available while payloads are invalid."""
        with self._lock:
            row=self._connection.execute('SELECT generation,status FROM local_account_generations WHERE namespace=?',(identity.namespace,)).fetchone()
            return dict(row)if row is not None else None

    def _check_generation(self,identity):
        row=self.account_generation(identity)
        if row is None:
            if identity.data_generation:
                raise LocalDataError('account_verification_required','账号资料尚未核验，请联网后重试。')
            return
        if row['status']!='active' or not identity.data_generation or row['generation']!=identity.data_generation:
            raise LocalDataError('account_data_reset','这份账号资料已失效，请联网重新读取。')

    def invalidate_account(self,identity):
        """Durable tombstone before any removal or remote wait; never removes it."""
        with self._db()as conn:
            conn.execute("INSERT INTO local_account_generations VALUES(?,?,'invalid') ON CONFLICT(namespace) DO UPDATE SET status='invalid'",
                         (identity.namespace,identity.data_generation))

    def bind_generation(self,identity,*,has_reset):
        """Activate a verified generation after old payloads have been purged.

        The sole preservation migration is an unbound, never-reset account.
        Existing frozen mutation UUIDs and business hashes remain unchanged.
        """
        if not re.fullmatch(r'[a-f0-9]{64}',identity.data_generation)or type(has_reset)is not bool:
            raise LocalDataError('account_verification_required','账号资料核验结果无效，请联网后重试。')
        with self._db()as conn:
            row=conn.execute('SELECT generation,status FROM local_account_generations WHERE namespace=?',(identity.namespace,)).fetchone()
            preserve=row is None and not has_reset
            unchanged=row is not None and row['status']=='active'and row['generation']==identity.data_generation
            if not preserve and not unchanged:
                for table in ('local_values','local_operations'):
                    if conn.execute('SELECT count(*) FROM '+table+' WHERE namespace=?',(identity.namespace,)).fetchone()[0]:
                        raise LocalDataError('account_data_reset','旧账号资料已失效，清理完成后才能读取。')
            if preserve:
                rows=conn.execute('SELECT operation_id,payload FROM local_operations WHERE namespace=?',(identity.namespace,)).fetchall()
                for record in rows:
                    operation=self._open(identity,'operation/'+record['operation_id'],record['payload'])
                    previous=operation.get('data_generation')
                    if previous and previous!=identity.data_generation:
                        raise LocalDataError('account_data_reset','旧账号资料已失效，请重新读取。')
                    operation['data_generation']=identity.data_generation
                    sealed,_=self._seal(identity,'operation/'+record['operation_id'],operation)
                    conn.execute('UPDATE local_operations SET payload=? WHERE namespace=? AND operation_id=?',(sealed,identity.namespace,record['operation_id']))
            conn.execute("INSERT INTO local_account_generations VALUES(?,?,'active') ON CONFLICT(namespace) DO UPDATE SET generation=excluded.generation,status='active'",
                         (identity.namespace,identity.data_generation))
        return {'legacy_preserved':preserve,'unchanged':unchanged}

    def _seal(self,identity,purpose,value):
        raw=json_text(value)
        if len(raw.encode('utf8'))>MAX_VALUE_BYTES:raise LocalDataError('local_limit','本次内容较大，请分批保存。')
        try:result=str(self.vault.seal(identity.actor_id,identity.namespace+'/'+purpose,raw))
        except Exception:raise LocalDataError('local_crypto','本机加密存储暂不可用，请重启应用后重试。') from None
        if not result or result==raw:raise LocalDataError('local_crypto','本机加密存储暂不可用。')
        return result,len(result.encode('utf8'))

    def _open(self,identity,purpose,sealed):
        try:
            raw=str(self.vault.open(identity.actor_id,identity.namespace+'/'+purpose,sealed))
            if len(raw.encode('utf8'))>MAX_VALUE_BYTES:raise ValueError
            return json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except Exception:raise LocalDataError('local_crypto','本机资料校验未通过，请勿清除应用数据。') from None

    @account_guard
    def put(self,identity,kind,key,value):
        if kind not in {'resource','cache','draft','meta','mapping','refresh'}:raise LocalDataError('validation')
        hashed=digest(key);sealed,size=self._seal(identity,kind+'/'+hashed,{'key':key,'value':value})
        now=self.clock()
        with self._db() as conn:
            if kind=='draft':
                used=conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM local_values WHERE namespace=? AND kind='draft' AND key_hash<>?",(identity.namespace,hashed)).fetchone()[0]
                if used+size>MAX_DRAFT_BYTES:raise LocalDataError('local_limit','本机未上传草稿较多，请先上传或手动整理；已有草稿保持保存。')
            conn.execute('INSERT INTO local_values VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,key_hash,kind) DO UPDATE SET updated_at=excluded.updated_at,accessed_at=excluded.accessed_at,size_bytes=excluded.size_bytes,payload=excluded.payload',
                (identity.namespace,hashed,kind,now,now,size,sealed))
            if kind=='cache' and isinstance(key,list) and key and isinstance(key[0],str):
                conn.execute('INSERT INTO local_cache_tags VALUES(?,?,?) ON CONFLICT(namespace,key_hash) DO UPDATE SET tag=excluded.tag',
                    (identity.namespace,hashed,key[0]))
            if kind=='cache':self._prune_cache(conn,identity.namespace)
            if kind=='refresh':
                conn.execute("DELETE FROM local_values WHERE namespace=? AND kind='refresh' AND key_hash IN (SELECT key_hash FROM local_values WHERE namespace=? AND kind='refresh' ORDER BY updated_at DESC LIMIT -1 OFFSET 256)",(identity.namespace,identity.namespace))

    @account_guard
    def get(self,identity,kind,key,default=None):
        hashed=digest(key)
        with self._db() as conn:
            row=conn.execute('SELECT payload,accessed_at FROM local_values WHERE namespace=? AND key_hash=? AND kind=?',
                (identity.namespace,hashed,kind)).fetchone()
            if row is None:return default
            if kind=='cache' and row['accessed_at']<self.clock()-60:
                conn.execute('UPDATE local_values SET accessed_at=? WHERE namespace=? AND key_hash=? AND kind=?',
                    (self.clock(),identity.namespace,hashed,kind))
        value=self._open(identity,kind+'/'+hashed,row['payload'])
        if value.get('key')!=key:raise LocalDataError('local_crypto')
        return value['value']

    @account_guard
    def resources_present(self,identity,names):
        """Downloaded-resource presence only; payload reads still authenticate.

        Status must not decrypt a large unrelated history merely to report
        whether its snapshot exists. This never claims ciphertext validity.
        """
        names=tuple(names)
        if not names or len(names)>32 or any(type(name)is not str or not 1<=len(name)<=100 for name in names):
            raise LocalDataError('validation')
        keys={digest(name)for name in names}
        with self._db() as conn:
            rows=conn.execute("SELECT key_hash FROM local_values WHERE namespace=? AND kind='resource' AND key_hash IN ("+
                ','.join('?'for _ in keys)+')',(identity.namespace,*keys)).fetchall()
        return {row['key_hash']for row in rows}==keys

    @account_guard
    def delete(self,identity,kind,key):
        with self._db() as conn:conn.execute('DELETE FROM local_values WHERE namespace=? AND kind=? AND key_hash=?',
            (identity.namespace,kind,digest(key)))

    @account_guard
    def list_values(self,identity,kind,limit=8):
        if kind!='refresh' or type(limit)is not int or not 1<=limit<=256:raise LocalDataError('validation')
        with self._db() as conn:rows=conn.execute('SELECT key_hash,payload FROM local_values WHERE namespace=? AND kind=? ORDER BY updated_at DESC LIMIT ?',
            (identity.namespace,kind,limit)).fetchall()
        return [self._open(identity,kind+'/'+row['key_hash'],row['payload']) for row in rows]

    @account_guard
    def invalidate_cache(self,identity,prefix):
        if not isinstance(prefix,str) or not prefix or not prefix.endswith('.'):raise LocalDataError('validation')
        with self._db() as conn:
            conn.execute("DELETE FROM local_values WHERE namespace=? AND kind='cache' AND key_hash IN (SELECT key_hash FROM local_cache_tags WHERE namespace=? AND tag LIKE ?)",
                (identity.namespace,identity.namespace,prefix+'%'))
            conn.execute('DELETE FROM local_cache_tags WHERE namespace=? AND tag LIKE ?',(identity.namespace,prefix+'%'))

    @account_guard
    def invalidate_action_cache(self,identity,action):
        if type(action)is not str or not 1<=len(action)<=120 or any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for char in action):raise LocalDataError('validation')
        with self._db() as conn:
            conn.execute("DELETE FROM local_values WHERE namespace=? AND kind='cache' AND key_hash IN (SELECT key_hash FROM local_cache_tags WHERE namespace=? AND tag=?)",(identity.namespace,identity.namespace,action))
            conn.execute('DELETE FROM local_cache_tags WHERE namespace=? AND tag=?',(identity.namespace,action))

    @account_guard
    def put_if_absent(self,identity,kind,key,value):
        if kind not in {'meta','draft','mapping'}:raise LocalDataError('validation')
        hashed=digest(key);sealed,size=self._seal(identity,kind+'/'+hashed,{'key':key,'value':value})
        now=self.clock()
        with self._db() as conn:
            conn.execute('INSERT OR IGNORE INTO local_values VALUES(?,?,?,?,?,?,?)',
                (identity.namespace,hashed,kind,now,now,size,sealed))
            row=conn.execute('SELECT payload FROM local_values WHERE namespace=? AND key_hash=? AND kind=?',
                (identity.namespace,hashed,kind)).fetchone()
        return self._open(identity,kind+'/'+hashed,row['payload'])['value']

    @account_guard
    def replace_resources(self,identity,resources):
        prepared=[];now=self.clock()
        for name,value in resources.items():
            hashed=digest(name);sealed,size=self._seal(identity,'resource/'+hashed,{'key':name,'value':value})
            prepared.append((identity.namespace,hashed,'resource',now,now,size,sealed))
        with self._db() as conn:
            for values in prepared:conn.execute('INSERT INTO local_values VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,key_hash,kind) DO UPDATE SET updated_at=excluded.updated_at,accessed_at=excluded.accessed_at,size_bytes=excluded.size_bytes,payload=excluded.payload',values)
            if resources:
                placeholders=','.join('?' for _ in resources)
                conn.execute("UPDATE local_operations SET status='applied' WHERE namespace=? AND status='confirmed' AND resource IN ("+placeholders+')',
                    (identity.namespace,*resources.keys()))

    def _prune_cache(self,conn,namespace):
        used=conn.execute("SELECT COALESCE(SUM(size_bytes),0) FROM local_values WHERE namespace=? AND kind='cache'",(namespace,)).fetchone()[0]
        if used<=self.cache_budget:return
        rows=conn.execute("SELECT key_hash,size_bytes FROM local_values WHERE namespace=? AND kind='cache' ORDER BY accessed_at,key_hash",(namespace,)).fetchall()
        for row in rows:
            if used<=self.cache_budget:break
            conn.execute("DELETE FROM local_values WHERE namespace=? AND kind='cache' AND key_hash=?",(namespace,row['key_hash']))
            conn.execute('DELETE FROM local_cache_tags WHERE namespace=? AND key_hash=?',(namespace,row['key_hash']))
            used-=row['size_bytes']

    @account_guard
    def clear_cache(self,identity):
        with self._db() as conn:
            row=conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS n FROM local_values WHERE namespace=? AND kind='cache'",(identity.namespace,)).fetchone()
            conn.execute("DELETE FROM local_values WHERE namespace=? AND kind IN ('cache','refresh')",(identity.namespace,))
            conn.execute('DELETE FROM local_cache_tags WHERE namespace=?',(identity.namespace,))
        return {'cleared_bytes':row['n'],'retained_pending':self.status(identity)['pending_count']}

    def purge_account(self,identity):
        """Remove one namespace, including drafts and unsent mutations, after cloud erasure."""
        with self._db() as conn:
            for table in ('local_values','local_cache_tags','local_operations'):
                conn.execute('DELETE FROM '+table+' WHERE namespace=?',(identity.namespace,))
        with self._lock:self._connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    @account_guard
    def enqueue(self,identity,operation):
        operation=deepcopy(operation)
        if identity.data_generation:
            if operation.get('data_generation',identity.data_generation)!=identity.data_generation:
                raise LocalDataError('account_data_reset','这份账号修改已失效，请重新读取。')
            operation['data_generation']=identity.data_generation
        identifier=operation['operation_id']
        try:
            if str(UUID(identifier))!=identifier:raise ValueError
        except (ValueError,TypeError,AttributeError):raise LocalDataError('validation','操作编号无效。') from None
        sealed,_=self._seal(identity,'operation/'+identifier,operation);now=self.clock()
        with self._db() as conn:
            existing=conn.execute('SELECT payload FROM local_operations WHERE namespace=? AND operation_id=?',(identity.namespace,identifier)).fetchone()
            if existing is not None:
                previous=self._open(identity,'operation/'+identifier,existing['payload'])
                if previous.get('request_digest')!=operation.get('request_digest'):
                    raise LocalDataError('conflict','同一操作编号不能保存不同内容。')
                return previous
            pending=conn.execute("SELECT COUNT(*) FROM local_operations WHERE namespace=? AND status IN ('queued','uncertain','conflict','rejected')",(identity.namespace,)).fetchone()[0]
            if pending>=MAX_PENDING_OPERATIONS:raise LocalDataError('local_limit','待同步记录较多，请先联网同步；已有记录保持保存。')
            conn.execute('INSERT INTO local_operations(namespace,operation_id,action,resource,status,attempted,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                (identity.namespace,identifier,operation['action'],operation['resource'],'queued',0,now,now,sealed))
        return operation

    @account_guard
    def operations(self,identity,*,include_applied=False):
        condition='' if include_applied else " AND status IN ('queued','uncertain','conflict','rejected','confirmed')"
        with self._db() as conn:rows=conn.execute('SELECT * FROM local_operations WHERE namespace=?'+condition+' ORDER BY sequence_no',(identity.namespace,)).fetchall()
        values=[]
        for row in rows:
            value=self._open(identity,'operation/'+row['operation_id'],row['payload'])
            value.update(status=row['status'],attempted=bool(row['attempted']),sequence_no=row['sequence_no'])
            values.append(value)
        return values

    @account_guard
    def operation(self,identity,identifier):
        with self._db() as conn:row=conn.execute('SELECT * FROM local_operations WHERE namespace=? AND operation_id=?',
            (identity.namespace,identifier)).fetchone()
        if row is None:return None
        value=self._open(identity,'operation/'+identifier,row['payload'])
        value.update(status=row['status'],attempted=bool(row['attempted']),sequence_no=row['sequence_no'])
        return value

    @account_guard
    def change_operation(self,identity,identifier,*,status=None,attempted=None,patch=None):
        if status is not None and status not in PENDING_STATUSES|{'applied','discarded','confirmed'}:raise LocalDataError('validation')
        with self._db() as conn:
            row=conn.execute('SELECT * FROM local_operations WHERE namespace=? AND operation_id=?',(identity.namespace,identifier)).fetchone()
            if row is None:raise LocalDataError('not_found','待同步记录不存在。')
            value=self._open(identity,'operation/'+identifier,row['payload'])
            if patch:
                if row['attempted'] and any(key in patch for key in ('payload','base_version','local_id','wire_payload','wire_base_version')):
                    raise LocalDataError('conflict','已发送记录必须先核对结果，不能改写原操作。')
                value.update(patch)
            sealed,_=self._seal(identity,'operation/'+identifier,value)
            conn.execute('UPDATE local_operations SET status=?,attempted=?,updated_at=?,payload=? WHERE namespace=? AND operation_id=?',
                (status or row['status'],int(row['attempted'] if attempted is None else attempted),self.clock(),sealed,identity.namespace,identifier))

    @account_guard
    def claim_operation(self,identity,identifier,wire_payload,wire_base_version):
        """Freeze before I/O, without resurrecting a concurrently cancelled row."""
        with self._db() as conn:
            row=conn.execute('SELECT * FROM local_operations WHERE namespace=? AND operation_id=?',
                (identity.namespace,identifier)).fetchone()
            if row is None or row['status'] not in {'queued','uncertain'}:return None
            value=self._open(identity,'operation/'+identifier,row['payload'])
            if row['attempted']:
                if value.get('wire_payload')!=wire_payload or value.get('wire_base_version')!=wire_base_version:
                    raise LocalDataError('conflict','已发送操作不能改变内容。')
            else:value.update(wire_payload=wire_payload,wire_base_version=wire_base_version)
            sealed,_=self._seal(identity,'operation/'+identifier,value)
            conn.execute("UPDATE local_operations SET status='uncertain',attempted=1,updated_at=?,payload=? WHERE namespace=? AND operation_id=?",
                (self.clock(),sealed,identity.namespace,identifier))
        return value

    @account_guard
    def resolve_library_intent(self,identity,identifier,*,replacement=None,current=None):
        if replacement is not None and identity.data_generation:
            replacement=deepcopy(replacement)
            if replacement.get('data_generation',identity.data_generation)!=identity.data_generation:
                raise LocalDataError('account_data_reset','旧账号修改已失效，请重新读取。')
            replacement['data_generation']=identity.data_generation
        """Atomically resolve one library intent without rewriting sent bodies.

        This additive helper owns no schema. Every changed/new operation stays
        sealed under the existing account namespace and transaction.
        """
        with self._db() as conn:
            rows=conn.execute("SELECT * FROM local_operations WHERE namespace=? AND status IN ('queued','uncertain','conflict','rejected','confirmed') ORDER BY sequence_no",
                (identity.namespace,)).fetchall()
            records={row['operation_id']:row for row in rows}
            decoded={key:self._open(identity,'operation/'+key,row['payload'])for key,row in records.items()}
            row=records.get(identifier)
            if row is None:raise LocalDataError('not_found','待同步记录不存在。')
            value=decoded[identifier]
            if (value.get('action')!='preferences.update' or value.get('resource')!='preferences'
                    or set(value.get('payload',{}))!={'diet_library_v1'}
                    or row['status']not in {'conflict','rejected'}
                    or row['status']=='rejected' and value.get('safe_to_retry')is not True):
                raise LocalDataError('conflict','请先核对原操作的云端结果。')
            parent=records.get(value.get('depends_on'))
            if parent is not None and parent['status']!='confirmed':
                raise LocalDataError('conflict','请先核对前一项收藏修改，再处理后续修改。')
            dependents={}
            for key,child in decoded.items():dependents.setdefault(child.get('depends_on'),[]).append(key)
            descendants=set();frontier={identifier}
            while frontier:
                found={key for parent in frontier for key in dependents.get(parent,())if key not in descendants and key!=identifier}
                descendants.update(found);frontier=found
            if value.get('depends_on')in descendants|{identifier}:
                raise LocalDataError('conflict','待同步操作的依赖需要先核对。')
            children=[]
            for key in descendants:
                record=records[key];child=decoded[key]
                if record['attempted'] or record['status']=='uncertain' or child.get('resource')!='preferences':
                    raise LocalDataError('conflict','后续修改尚未确认云端结果，请先核对原操作。')
                children.append((record,child))
            if replacement is not None:
                new_id=replacement['operation_id']
                try:
                    if str(UUID(new_id))!=new_id:raise ValueError
                except (ValueError,TypeError,AttributeError):raise LocalDataError('validation')from None
                if (conn.execute('SELECT 1 FROM local_operations WHERE namespace=? AND operation_id=?',(identity.namespace,new_id)).fetchone() is not None or replacement.get('action')!='preferences.update'
                        or replacement.get('resource')!='preferences' or replacement.get('depends_on')!=identifier
                        or set(replacement.get('payload',{}))!={'diet_library_v1'}):
                    raise LocalDataError('conflict','新的收藏操作无效。')
                pending=sum(r['status']in PENDING_STATUSES for r in rows)
                if pending>MAX_PENDING_OPERATIONS:raise LocalDataError('local_limit')
                if not isinstance(current,dict)or not isinstance(current.get('value'),dict)or not isinstance(current.get('version'),str):
                    raise LocalDataError('sync_protocol','最新云端收藏尚未读取完整。')
                # Adopt the authenticated cloud read in the same transaction as
                # the replacement and child links. Otherwise the local overlay
                # could temporarily hide additions absent from its older base.
                key=digest('preferences')
                resource_sealed,resource_size=self._seal(identity,'resource/'+key,{'key':'preferences','value':current})
                now=self.clock()
                conn.execute('INSERT INTO local_values VALUES(?,?,?,?,?,?,?) ON CONFLICT(namespace,key_hash,kind) DO UPDATE SET updated_at=excluded.updated_at,accessed_at=excluded.accessed_at,size_bytes=excluded.size_bytes,payload=excluded.payload',
                    (identity.namespace,key,'resource',now,now,resource_size,resource_sealed))
                sealed,_=self._seal(identity,'operation/'+new_id,replacement);now=self.clock()
                conn.execute('INSERT INTO local_operations(namespace,operation_id,action,resource,status,attempted,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity.namespace,new_id,'preferences.update','preferences','queued',0,now,now,sealed))
            for record,child in children:
                status=record['status']
                if replacement is None:
                    status='conflict';child.update(error_code='library_parent_discarded',conflict_current=deepcopy(current))
                elif child.get('depends_on')==identifier:child['depends_on']=replacement['operation_id']
                sealed,_=self._seal(identity,'operation/'+record['operation_id'],child)
                conn.execute('UPDATE local_operations SET status=?,updated_at=?,payload=? WHERE namespace=? AND operation_id=?',
                    (status,self.clock(),sealed,identity.namespace,record['operation_id']))
            conn.execute("UPDATE local_operations SET status='discarded',updated_at=? WHERE namespace=? AND operation_id=?",
                (self.clock(),identity.namespace,identifier))
        return {'resolved':True,'operation_id':replacement['operation_id']if replacement else identifier}

    @account_guard
    def cancel_unsent_creates(self,identity,operation,numbers,create_action):
        """An entirely local create/delete pair need never leave the device."""
        if not numbers or any(type(number)is not int or number>=0 for number in numbers):return False
        with self._db() as conn:
            rows=conn.execute("SELECT * FROM local_operations WHERE namespace=? AND status IN ('queued','uncertain','conflict','rejected') ORDER BY sequence_no",(identity.namespace,)).fetchall()
            values=[(row,self._open(identity,'operation/'+row['operation_id'],row['payload'])) for row in rows]
            creates=[(row,value) for row,value in values if value.get('action')==create_action and value.get('local_number') in numbers]
            if {value.get('local_number') for row,value in creates}!=set(numbers) or any(row['attempted'] for row,value in creates):return False
            related=[]
            for row,value in values:
                body=value.get('payload',{})
                referenced=set(item for key,item in body.items() if key in {'record_id','observation_id','reminder_id','goal_id'} and type(item)is int)
                for key in ('record_ids','reminder_ids'):
                    if isinstance(body.get(key),list):referenced.update(item for item in body[key] if type(item)is int)
                if value.get('local_number') in numbers or referenced&set(numbers):
                    if row['attempted']:return False
                    related.append(row['operation_id'])
            sealed,_=self._seal(identity,'operation/'+operation['operation_id'],operation);now=self.clock()
            for identifier in related:conn.execute("UPDATE local_operations SET status='discarded',updated_at=? WHERE namespace=? AND operation_id=?",(now,identity.namespace,identifier))
            conn.execute("INSERT INTO local_operations(namespace,operation_id,action,resource,status,attempted,created_at,updated_at,payload) VALUES(?,?,?,?,'discarded',0,?,?,?)",
                (identity.namespace,operation['operation_id'],operation['action'],operation['resource'],now,now,sealed))
        return True

    @account_guard
    def status(self,identity):
        with self._db() as conn:
            grouped=conn.execute('SELECT status,COUNT(*) AS n FROM local_operations WHERE namespace=? GROUP BY status',(identity.namespace,)).fetchall()
            sizes=conn.execute('SELECT kind,COALESCE(SUM(size_bytes),0) AS n FROM local_values WHERE namespace=? GROUP BY kind',(identity.namespace,)).fetchall()
        counts={row['status']:row['n'] for row in grouped};size={row['kind']:row['n'] for row in sizes}
        return {'pending_count':sum(counts.get(key,0) for key in PENDING_STATUSES),
            'conflict_count':counts.get('conflict',0),'rejected_count':counts.get('rejected',0),
            'uncertain_count':counts.get('uncertain',0),'cache_bytes':size.get('cache',0),
            'awaiting_refresh_count':counts.get('confirmed',0),
            'cache_budget_bytes':self.cache_budget,'core_bytes':sum(v for k,v in size.items() if k not in {'cache','refresh'}),
            'draft_bytes':size.get('draft',0),'draft_budget_bytes':MAX_DRAFT_BYTES,
            'encrypted':True}
