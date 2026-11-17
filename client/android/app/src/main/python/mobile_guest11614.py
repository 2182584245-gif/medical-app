"""Guest-only Native storage/transport. Never accepts or manufactures member identity."""
from __future__ import annotations
import base64,hashlib,json,os,re,sqlite3,threading,time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID,uuid4
import httpx

GUEST_ACTIONS=frozenset({'chat.greeting.ensure','preferences.get','preferences.update','chat.conversations.list','chat.conversations.create','chat.conversations.rename','chat.conversations.delete','chat.messages.list','chat.message.attachments','files.upload','files.get','files.delete','chat.files.list','chat.image.get','chat.send','chat.tools.proposals','ai.operations.status','ai.operations.cancel','guest.snapshot.refresh','guest.memory.get','guest.memory.update','library.search','library.card','library.document','library.source'})
DRAFT_ACTIONS=frozenset({'local149.draft.get','local149.draft.save','local149.draft-attachment.get','local149.draft-attachment.save','local149.draft-attachment.delete'})
READ_ACTIONS=frozenset({'preferences.get','chat.conversations.list','chat.messages.list','chat.message.attachments','chat.files.list','chat.image.get','files.get','guest.memory.get','library.search','library.card','library.document','library.source','chat.tools.proposals'})
FORBIDDEN=frozenset({'user_id','actor_id','member_user_id','actor_user_id','api_key','base_url','path','token','guest_token','authorization'})

def encode(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
def uid(value):
 if not isinstance(value,str)or str(UUID(value))!=value:raise GuestError('validation','体验操作编号无效。')
 return value
class GuestError(ValueError):
 def __init__(self,code,message='体验服务暂不可用，内容已保留，请稍后重试。'):super().__init__(message);self.code=code

def atomic(path,text):
 if path.is_symlink()or path.with_suffix(path.suffix+'.tmp').is_symlink():raise GuestError('guest_storage')
 tmp=path.with_suffix(path.suffix+'.tmp')
 with tmp.open('w',encoding='utf-8')as f:f.write(text);f.flush();os.fsync(f.fileno())
 os.replace(tmp,path)
def remove_private(directory,base):
 base=base.resolve();directory=Path(directory)
 if directory.is_symlink()or directory.resolve().parent!=base:raise GuestError('guest_storage','体验文件位置校验失败，请重试清理。')
 def remove(p):
  if p.is_symlink()or not p.resolve().is_relative_to(base):raise GuestError('guest_storage')
  if not p.exists():return
  if p.is_dir():
   for child in p.iterdir():remove(child)
   p.rmdir()
  else:p.unlink()
 remove(directory)

AUTO_LOGIN_ENTRY_REVISION=11815

def migrate_auto_login(root):
 """Enter guest once for this repair; keep all member business data and keys.

 A completed older guest migration must not reinstate its saved auto-login on
 the first launch of this repair. Later explicit choices remain authoritative.
 """
 root=Path(root);marker=root/f'auto-login-explicit{AUTO_LOGIN_ENTRY_REVISION}.json'
 if marker.is_symlink():raise GuestError('guest_storage')
 if marker.is_file():
  value=json.loads(marker.read_text(encoding='utf-8'))
  if value=={'version':AUTO_LOGIN_ENTRY_REVISION,'complete':True}:return False
 atomic(marker,encode({'version':AUTO_LOGIN_ENTRY_REVISION,'complete':False}))
 sessions=root/'local149-session'
 if sessions.is_symlink():raise GuestError('guest_storage')
 if sessions.exists():
  for p in sessions.iterdir():
   if re.fullmatch(r'[a-f0-9]{64}(?:\.active(?:\.tmp)?|-[1-9][0-9]*\.sealed(?:\.tmp)?)',p.name):
    if p.is_symlink()or not p.is_file():raise GuestError('guest_storage')
    p.unlink()
 atomic(marker,encode({'version':AUTO_LOGIN_ENTRY_REVISION,'complete':True}));return True

class GuestStore:
 def __init__(self,base,vault,endpoint):
  self.base=Path(base).resolve();self.base.mkdir(parents=True,exist_ok=True)
  if Path(base).is_symlink():raise GuestError('guest_storage')
  self.pointer=self.base/'active.json';self.vault=vault;self.realm=hashlib.sha256(endpoint.encode()).hexdigest();self.lock=threading.RLock();self.closed=False
  if self.pointer.is_symlink():raise GuestError('guest_storage')
  if self.pointer.exists():self.id=uid(json.loads(self.pointer.read_text(encoding='utf-8'))['id'])
  else:self.id=str(uuid4());atomic(self.pointer,encode({'id':self.id}))
  self.root=self.base/self.id
  if self.root.is_symlink():raise GuestError('guest_storage')
  self.root.mkdir(exist_ok=True);self.path=self.root/'guest.sqlite3'
  if self.path.is_symlink():raise GuestError('guest_storage')
  self.db=sqlite3.connect(self.path,timeout=10,check_same_thread=False);self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA secure_delete=ON')
  self.db.execute('CREATE TABLE IF NOT EXISTS values11614(key_hash TEXT PRIMARY KEY,payload TEXT NOT NULL,size_bytes INTEGER NOT NULL)');self.db.commit()
 def key(self,key):return hashlib.sha256(encode(key).encode()).hexdigest()
 def get(self,key,default=None):
  with self.lock:
   if self.closed:raise GuestError('account_changed','体验会话已切换。')
   k=self.key(key);row=self.db.execute('SELECT payload FROM values11614 WHERE key_hash=?',(k,)).fetchone()
   if row is None:return default
   value=json.loads(str(self.vault.openGuest(self.id,'guest11614/'+self.realm+'/'+k,row[0])))
   if value['key']!=key:raise GuestError('guest_storage')
   return value['value']
 def put(self,key,value):
  raw=encode({'key':key,'value':value})
  if len(raw.encode())>32*1024*1024:raise GuestError('guest_limit','体验资料较大，请减少附件后重试。')
  with self.lock:
   if self.closed:raise GuestError('account_changed','体验会话已切换。')
   k=self.key(key);sealed=str(self.vault.sealGuest(self.id,'guest11614/'+self.realm+'/'+k,raw))
   if sealed==raw:raise GuestError('guest_storage')
   total=self.db.execute('SELECT COALESCE(SUM(size_bytes),0) FROM values11614 WHERE key_hash<>?',(k,)).fetchone()[0]
   if total+len(sealed.encode())>256*1024*1024:raise GuestError('guest_limit','体验资料空间已满，请先整理附件。')
   self.db.execute('INSERT OR REPLACE INTO values11614 VALUES(?,?,?)',(k,sealed,len(sealed.encode())));self.db.commit()
 def delete(self,key):
  with self.lock:
   if self.closed:raise GuestError('account_changed')
   self.db.execute('DELETE FROM values11614 WHERE key_hash=?',(self.key(key),));self.db.commit()
 def entries(self):
  with self.lock:
   if self.closed:raise GuestError('account_changed')
   values=[]
   for k,payload in self.db.execute('SELECT key_hash,payload FROM values11614').fetchall():
    value=json.loads(str(self.vault.openGuest(self.id,'guest11614/'+self.realm+'/'+k,payload)));values.append((value['key'],value['value']))
   return values
 def checkpoint(self):
  with self.lock:
   if not self.closed:self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
 def close(self):
  with self.lock:
   if not self.closed:self.db.close();self.closed=True
 def purge(self):
  self.close();remove_private(self.root,self.base)
  self.vault.clearGuestKey(self.id)
  if self.pointer.exists():
   if self.pointer.is_symlink():raise GuestError('guest_storage')
   if json.loads(self.pointer.read_text(encoding='utf-8')).get('id')==self.id:self.pointer.unlink()

class GuestCloud:
 def __init__(self,endpoint,*,transport=None):
  self.endpoint=endpoint.rstrip('/');self.transport=transport
  if not self.endpoint.startswith('https://'):raise GuestError('configuration','体验连接不可用。')
  self._pool_lock=threading.RLock();self._http=None;self._closed=False
 @contextmanager
 def client(self):
  with self._pool_lock:
   if self._closed:raise GuestError('account_changed','此前的体验连接已关闭。')
   if self._http is None:self._http=httpx.Client(verify=True,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(connect=8,read=180,write=30,pool=8),transport=self.transport)
   client=self._http
  yield client
 @contextmanager
 def response(self,path,body,token=None,*,stream=False):
  with self.client()as client:
   with self._pool_lock:
    if self._closed:raise GuestError('account_changed','此前的体验连接已关闭。')
    headers={**self.headers(token),**({'Accept':'application/x-ndjson'}if stream else{})}
    if path=='voice/transcribe':headers['X-Fawnsee-ASR-Contract']='2'
    request=client.build_request('POST',self.endpoint+'/v1/guest/'+path,json=body,headers=headers)
    request.headers.pop('cookie',None)
   response=client.send(request,stream=stream)
   try:
    if self._closed:raise GuestError('account_changed','此前的体验连接已关闭。')
    yield response
   finally:
    response.close()
    with self._pool_lock:client.cookies.clear()
 def close(self):
  with self._pool_lock:
   self._closed=True;client=self._http;self._http=None
  if client is not None:client.close()
 def __enter__(self):return self
 def __exit__(self,*_):self.close()
 def headers(self,token):
  if token is not None and not re.fullmatch(r'gs1_[A-Za-z0-9_-]{43}',token):raise GuestError('guest_storage')
  return {'Authorization':'Guest '+token}if token else{}
 @staticmethod
 def checked(response):
  if not 200<=response.status_code<300:
   try:code=response.json().get('detail')
   except Exception:code='guest_unavailable'
   public={'login_required':'此功能需要登录后使用。','guest_session_gone':'此前的体验服务已结束，请重新打开体验。','cancelled':'已停止回答。','conflict':'本次操作仍需核对，请查看已有记录。'}
   aliases={'guest_conversation_not_found':'not_found','guest_file_not_found':'not_found','guest_message_not_found':'not_found','guest_request_running':'ai_in_progress','guest_request_failed':'ai_attempt_finished','guest_request_conflict':'conflict'}
   code=aliases.get(code,code)
   public.update(not_found='这项体验内容不存在或已删除。',ai_in_progress='上次回答仍在处理中，请稍后查看。',ai_attempt_finished='上次回答已结束，请明确重新发送。')
   if code not in public:code='guest_unavailable'
   raise GuestError(code,public.get(code,'体验服务暂不可用，内容已保留，请稍后重试。'))
 def request(self,path,body,token=None):
  try:
   with self.response(path,body,token)as r:self.checked(r);return r.json()
  except GuestError:raise
  except Exception:raise GuestError('network')from None
 def lines(self,path,body,token,cancel):
  try:
   with self.response(path,body,token,stream=True)as r:
     if not 200<=r.status_code<300:r.read()
     self.checked(r);done=threading.Event()
     def stop():
      while not done.wait(.1):
       if cancel.is_set():
        try:r.close()
        except Exception:pass
        return
     watcher=threading.Thread(target=stop,daemon=True);watcher.start();total=0
     try:
      for line in r.iter_lines():
       if cancel.is_set()or self._closed:raise GuestError('cancelled','已停止回答。')
       if not line:continue
       total+=len(line.encode())
       if len(line)>16*1024*1024 or total>32*1024*1024:raise GuestError('guest_unavailable')
       value=json.loads(line)
       if not isinstance(value,dict):raise GuestError('guest_unavailable')
       yield value
     finally:done.set()
  except GuestError:raise
  except Exception:
   if cancel.is_set():raise GuestError('cancelled','已停止回答。')from None
   raise GuestError('network')from None

class GuestFacade:
 def __init__(self,base,vault,endpoint,*,transport=None):
  self.store=GuestStore(base,vault,endpoint);self.id=self.store.id;self.epoch=str(uuid4());self.lock=threading.RLock();self.cloud_lock=threading.RLock();self.cloud=GuestCloud(endpoint,transport=transport);self.closing=False;self.active={};self.active_conversations={}
 def descriptor(self):return {'enabled':True,'id':self.id,'session_epoch':self.epoch,'cleanup_pending':self.closing}
 def scope(self):return {'kind':'guest','scope_id':'guest:'+self.id,'guest_id':self.id,'session_epoch':self.epoch,'private_root':str(self.store.root)}
 def guard(self,epoch):
  if self.closing or epoch!=self.epoch:raise GuestError('account_changed','体验会话已切换，本次结果未写入。')
 def credentials(self):return self.store.get('credentials',{})
 def ensure_cloud(self,epoch):
  with self.cloud_lock:
   self.guard(epoch);credentials=self.credentials()
   if credentials.get('token'):return credentials['token']
   if not credentials.get('creation_key'):
    credentials={'creation_key':base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')};self.store.put('credentials',credentials)
   value=self.cloud.request('bootstrap',{'creation_key':credentials['creation_key']})
   token=value.get('guest_token');cloud_id=uid((value.get('guest')or{}).get('id'))
   if not isinstance(token,str)or not re.fullmatch(r'gs1_[A-Za-z0-9_-]{43}',token):raise GuestError('guest_unavailable')
   # Retain the capability even if cleanup began during bootstrap. Purge waits
   # for this small lock and must recover every created Cloud Guest.
   self.store.put('credentials',{**credentials,'token':token,'cloud_guest_id':cloud_id})
   self.guard(epoch);self.store.put('state',value.get('state')or{});return token
 def state(self):return self.store.get('state',{'preferences':{},'conversations':[],'messages':{},'files':[],'memory':{'entries':[]},'revision':0})
 def deleted_guard(self,action,body):
  gone=self.store.get('deleted',{'conversations':[],'messages':[],'files':[]})
  if str(body.get('conversation_id'))in gone['conversations']or action=='chat.message.attachments'and str(body.get('message_id'))in gone['messages']or action in {'files.get','chat.image.get'}and str(body.get('file_id',body.get('attachment_id')))in gone['files']:
   raise GuestError('not_found','这项体验内容已删除。')
 def purge_references(self,*,conversations=(),files=()):
  convs={str(x)for x in conversations};fileids={str(x)for x in files};state=self.state();messages={str(m['id'])for cid,rows in state.get('messages',{}).items()if cid in convs for m in rows}
  gone=self.store.get('deleted',{'conversations':[],'messages':[],'files':[]})
  for key,values in [('conversations',convs),('messages',messages),('files',fileids)]:gone[key]=sorted(set(gone[key])|values)
  self.store.put('deleted',gone)
  for operation,cid in list(self.active_conversations.items()):
   if str(cid)in convs and operation in self.active:self.active[operation].set()
  entries=self.store.entries();hashes={r.get('sha256')for r in state.get('files',[])if str(r.get('id'))in fileids and isinstance(r.get('sha256'),str)};uploads=set()
  for identifier in fileids:
   stored=self.store.get(['uploaded-file',int(identifier)])
   if stored:
    try:hashes.add(hashlib.sha256(base64.b64decode(stored['content'].split(',',1)[-1],validate=True)).hexdigest())
    except (ValueError,TypeError,KeyError):pass
  for key,value in entries:
   if isinstance(key,list)and key and key[0]=='receipt'and isinstance(value,dict)and value.get('action')=='files.upload'and str(value.get('result'))in fileids:uploads.add(key[1])
  for key,value in entries:
   if not isinstance(key,list)or not key:continue
   discard=key[0]=='cache'
   if key[0]=='uploaded-file'and str(key[1])in fileids:discard=True
   if key[0]in {'draft','draft-attachment'}:
    if str(key[1]).rsplit(':',1)[-1]in convs:discard=True
    if isinstance(value,dict)and str(value.get('file_id'))in fileids:discard=True
    if key[0]=='draft-attachment'and isinstance(value,dict)and not value.get('file_id'):
     if value.get('upload_operation_id')in uploads or value.get('sha256')in hashes:discard=True
     encoded=value.get('data_base64')or(str(value.get('src','')).partition(',')[2]if str(value.get('src','')).startswith('data:')else None)
     if encoded:
      try:discard=discard or hashlib.sha256(base64.b64decode(encoded,validate=True)).hexdigest()in hashes
      except (ValueError,TypeError):pass
   if key[0]=='receipt'and isinstance(value,dict):
    result=value.get('result');resource=value.get('resource',{})
    if str(resource.get('conversation_id'))in convs or str(resource.get('file_id'))in fileids:discard=True
    if isinstance(result,dict)and str((result.get('conversation')or{}).get('id'))in convs:discard=True
    if isinstance(result,dict)and isinstance(result.get('result'),dict)and str((result['result'].get('conversation')or{}).get('id'))in convs:discard=True
    if value.get('action')in {'chat.conversations.create','chat.conversations.rename'}and isinstance(result,dict)and str(result.get('id'))in convs:discard=True
    if value.get('action')=='files.upload'and str(result.get('id')if isinstance(result,dict)else result)in fileids:discard=True
   if discard:self.store.delete(key)
  self.store.checkpoint()
 def local_read(self,action,body):
  self.deleted_guard(action,body);state=self.state()
  if action=='preferences.get':return True,state.get('preferences')or{'ai_complexity':'standard','ai_tone':'warm','ai_thinking_enabled':True,'ai_reasoning_effort':'high','ai_web_enabled':True,'ai_tools_enabled':True,'ai_context_consent':True,'ai_personal_context_consent':True}
  if action=='chat.conversations.list':return True,state.get('conversations',[])
  if action=='chat.files.list':return True,state.get('files',[])
  if action=='guest.memory.get':return True,state.get('memory',{'entries':[]})
  if action=='chat.messages.list':
   cid=str(body.get('conversation_id'));rows=state.get('messages',{}).get(cid)
   conv=next((r for r in state.get('conversations',[])if str(r.get('id'))==cid),None)
   if rows is not None and conv:
    before=body.get('before_sequence_no');rows=[r for r in rows if before is None or r.get('sequence_no',0)<before];limit=body.get('limit',40)
    if type(limit)is not int or not 1<=limit<=100:raise GuestError('validation')
    selected=rows[-limit:]
    from ollama_chat_app.services.first_meet_greeting import welcome_groups_from_rows,annotate_welcome_rows
    full=state.get('messages',{}).get(str(body['conversation_id']),[])
    groups=welcome_groups_from_rows(full,selected_ids={row['id']for row in selected})
    return True,{'conversation':conv,'messages':annotate_welcome_rows(selected,groups),'welcome_groups':groups,'has_more':len(rows)>limit,'next_before_sequence_no':selected[0].get('sequence_no')if selected and len(rows)>limit else None}
  if action=='chat.message.attachments':
   message=next((r for rows in state.get('messages',{}).values()for r in rows if r.get('id')==body.get('message_id')),None)
   if message is not None:return True,[r for r in state.get('files',[])if r.get('id')in message.get('attachments',[])]
  if action in {'files.get','chat.image.get'}:
   field='attachment_id'if action=='chat.image.get'else'file_id';identifier=body.get(field);stored=self.store.get(['uploaded-file',identifier])
   if stored:
    import mimetypes
    raw=stored['content'].split(',',1)[-1]if stored['content'].startswith('data:')else stored['content'];data=base64.b64decode(raw);media=mimetypes.guess_type(stored['original_name'])[0]or'application/octet-stream'
    if action!='chat.image.get'or media.startswith('image/'):
     return True,{field:identifier,'original_name':stored['original_name'],'media_type':media,'content':raw,'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'extracted_text':stored.get('extracted_text','')}
  missing=object();cached=self.store.get(['cache',action,body],missing)
  return (False,None)if cached is missing else(True,cached)
 def mirror(self,action,body,result,epoch):
  with self.lock:
   self.guard(epoch);self.deleted_guard(action,body);state=self.state()
   tomb=self.store.get('deleted',{'conversations':[]})['conversations']
   if action=='chat.send'and str((result.get('conversation')or{}).get('id'))in tomb or action in {'chat.conversations.create','chat.conversations.rename'}and str(result.get('id'))in tomb:raise GuestError('not_found','这段体验对话已删除。')
   if action in READ_ACTIONS:self.store.put(['cache',action,body],result)
   if action=='chat.greeting.ensure':
    message=result.get('message')if isinstance(result,dict)else None;cid=str(body.get('conversation_id'))
    if not isinstance(message,dict)or message.get('conversation_id')!=body.get('conversation_id')or message.get('provider')!='fawnsee-welcome' or type(message.get('sequence_no'))is not int or message['sequence_no']<0:raise GuestError('guest_unavailable')
    rows=state.setdefault('messages',{}).setdefault(cid,[])
    if not any(row.get('id')==message.get('id')for row in rows):rows.append(message);rows.sort(key=lambda row:row['sequence_no'])
   elif action in {'preferences.get','preferences.update'}:state['preferences']=result
   elif action=='chat.conversations.list':state['conversations']=result
   elif action in {'chat.conversations.create','chat.conversations.rename'}:
    state['conversations']=[result,*[r for r in state.get('conversations',[])if r.get('id')!=result.get('id')]];state.setdefault('messages',{}).setdefault(str(result['id']),[])
   elif action=='chat.conversations.delete':
    self.purge_references(conversations=body.get('conversation_ids',[]))
    ids={str(x)for x in body.get('conversation_ids',[])};state['conversations']=[r for r in state.get('conversations',[])if str(r.get('id'))not in ids];state['messages']={k:v for k,v in state.get('messages',{}).items()if k not in ids}
   elif action=='chat.messages.list':
    cid=str(body['conversation_id']);old={r['id']:r for r in state.setdefault('messages',{}).get(cid,[])};old.update({r['id']:r for r in result['messages']});state['messages'][cid]=sorted(old.values(),key=lambda r:r.get('sequence_no',r['id']))
   elif action=='chat.files.list':state['files']=result
   elif action=='files.upload':
    identifier=result.get('id')if isinstance(result,dict)else result
    if type(identifier)is not int or identifier<=0:raise GuestError('guest_unavailable')
    self.store.put(['uploaded-file',identifier],{'original_name':body.get('original_name'),'content':body.get('content')})
    import mimetypes
    raw=body.get('content','').split(',',1)[-1];data=base64.b64decode(raw)
    state.setdefault('files',[]).append({'id':identifier,'original_name':body.get('original_name'),'media_type':mimetypes.guess_type(body.get('original_name',''))[0]or'application/octet-stream','size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
   elif action=='files.delete':
    self.purge_references(files=[body.get('file_id')]);self.store.delete(['uploaded-file',body.get('file_id')]);state['files']=[r for r in state.get('files',[])if r.get('id')!=body.get('file_id')]
   elif action=='guest.memory.update':state['memory']=result
   elif action=='chat.send':
    conv=result['conversation'];cid=str(conv['id']);state['conversations']=[conv,*[r for r in state.get('conversations',[])if r.get('id')!=conv['id']]]
    rows={r['id']:r for r in state.setdefault('messages',{}).get(cid,[])}
    for key in ['user_message','assistant_message']:
     if isinstance(result.get(key),dict):rows[result[key]['id']]=result[key]
    state['messages'][cid]=sorted(rows.values(),key=lambda r:r.get('sequence_no',r['id']))
   self.store.put('state',state)
 def refresh(self,epoch):
  try:
   token=self.credentials().get('token')
   if not token:return False
   value=self.cloud.request('bootstrap',{},token)
   with self.lock:
    self.guard(epoch);old=self.state();new=value['state'];oldconv={r['id']for r in old.get('conversations',[])};newconv={r['id']for r in new.get('conversations',[])};oldfile={r['id']for r in old.get('files',[])};newfile={r['id']for r in new.get('files',[])}
    if oldconv-newconv or oldfile-newfile:self.purge_references(conversations=oldconv-newconv,files=oldfile-newfile)
    gone=self.store.get('deleted',{'conversations':[],'messages':[],'files':[]})
    new={**new,'conversations':[r for r in new.get('conversations',[])if str(r['id'])not in gone['conversations']],
      'messages':{cid:rows for cid,rows in new.get('messages',{}).items()if cid not in gone['conversations']},
      'files':[r for r in new.get('files',[])if str(r['id'])not in gone['files']]}
    self.store.put('state',new)
   return True
  except GuestError:return False
 def snapshot_refresh(self,epoch):
  credentials=self.credentials()
  if not credentials.get('token')and not credentials.get('creation_key'):return {'refreshed':False}
  token=self.ensure_cloud(epoch);refreshed=self.refresh(epoch)
  for key,receipt in self.store.entries():
   if not isinstance(key,list)or len(key)!=2 or key[0]!='receipt'or receipt.get('complete')or receipt.get('action')!='chat.send':continue
   result=self.cloud.request('request',{'action':'ai.operations.status','params':{'operation_id':key[1],'kind':'chat.send'},'request_id':str(uuid4())},token)
   if result.get('state')=='complete'and isinstance(result.get('result'),dict):
    self.mirror('chat.send',{},result['result'],epoch);self.guard(epoch);self.store.put(key,{**receipt,'complete':True,'result':result['result']});refreshed=True
  return {'refreshed':refreshed}
 def draft(self,action,body,epoch):
  if set(body)-{'key','text','attachment'}or not isinstance(body.get('key'),str)or not 1<=len(body['key'])<=120:raise GuestError('validation','草稿内容无效。')
  key=['draft-attachment'if'draft-attachment'in action else'draft',body['key']]
  with self.lock:
   self.guard(epoch)
   if action.endswith('.get'):return self.store.get(key,None if'draft-attachment'in action else{'text':''})
   if action.endswith('.delete'):self.store.delete(key);return {'discarded':True}
   if'draft-attachment'in action:
    value=body.get('attachment')
    if not isinstance(value,dict)or set(value)&FORBIDDEN:raise GuestError('validation')
   else:
    text=body.get('text')
    if not isinstance(text,str)or len(text)>12000:raise GuestError('validation')
    value={'text':text,'updated_at':time.time()}
   self.store.put(key,value);return value
 def request(self,action,body,request_id,*,progress=None):
  epoch=self.epoch;self.guard(epoch)
  if action not in GUEST_ACTIONS|DRAFT_ACTIONS:raise GuestError('login_required','此功能需要登录后使用。')
  if not isinstance(body,dict)or set(body)&FORBIDDEN:raise GuestError('validation','体验请求内容无效。')
  if action in DRAFT_ACTIONS:return self.draft(action,body,epoch)
  if action=='guest.snapshot.refresh':
   if body:raise GuestError('validation')
   return self.snapshot_refresh(epoch)
  self.deleted_guard(action,body)
  if action in READ_ACTIONS:
   found,value=self.local_read(action,body)
   if found:return value
  identifier=uid(request_id or str(uuid4()));request_digest=hashlib.sha256(encode([action,body]).encode()).hexdigest()
  dynamic=action in {'ai.operations.status','ai.operations.cancel'}
  previous=None if dynamic else self.store.get(['receipt',identifier])
  if previous:
   if previous['digest']!=request_digest:raise GuestError('conflict')
   if previous.get('complete'):return previous['result']
   if action=='chat.send':raise GuestError('outcome_uncertain','上次回答结果尚待核对，请先查看对话记录，不会自动重复发送。')
  token=self.ensure_cloud(epoch)
  if action=='chat.send':return self.chat(body,identifier,request_digest,token,epoch,progress)
  if not dynamic:self.store.put(['receipt',identifier],{'digest':request_digest,'action':action,'resource':{'conversation_id':body.get('conversation_id'),'file_id':body.get('file_id')},'complete':False})
  try:result=self.cloud.request('request',{'action':action,'params':body,'request_id':identifier},token)
  except GuestError:
   if action not in READ_ACTIONS:self.refresh(epoch)
   raise
  if action in {'ai.operations.status','ai.operations.cancel'}:
   result=dict(result)
   if result.get('state')=='not_found':result['state']='not_started'
   if result.get('state')=='complete'and isinstance(result.get('result'),dict):
    self.mirror('chat.send',{},result['result'],epoch);operation=body.get('operation_id');old=self.store.get(['receipt',operation],{})
    if old:self.store.put(['receipt',operation],{**old,'complete':True,'result':result['result']})
  self.mirror(action,body,result,epoch);self.guard(epoch)
  if not dynamic:self.store.put(['receipt',identifier],{'digest':request_digest,'action':action,'resource':{'conversation_id':body.get('conversation_id'),'file_id':body.get('file_id')},'complete':True,'result':result})
  if action not in READ_ACTIONS:self.refresh(epoch)
  return result
 def chat(self,body,identifier,request_digest,token,epoch,progress):
  cancel=threading.Event()
  with self.lock:
   self.guard(epoch)
   if self.active:raise GuestError('chat_busy','请先完成当前回答，或停止回答。')
   self.active[identifier]=cancel
  self.store.put(['receipt',identifier],{'digest':request_digest,'action':'chat.send','resource':{'conversation_id':body.get('conversation_id')},'complete':False});seq=0;cid=aid=0;result=None
  def emit(kind,**fields):
   nonlocal seq
   self.guard(epoch)
   if cid and str(cid)in self.store.get('deleted',{'conversations':[]})['conversations']:raise GuestError('not_found','这段体验对话已删除。')
   seq+=1
   value={**fields,'type':kind,'seq':seq,'conversation_id':cid,'assistant_message_id':aid,'scope_id':'guest:'+self.id,'session_epoch':epoch}
   callback=getattr(progress,'onStream',None)
   if callable(callback):callback(encode(value))
  try:
   for event in self.cloud.lines('chat/stream',{'params':body,'request_id':identifier},token,cancel):
    self.guard(epoch);kind=event.get('type')
    if kind=='started':
     cid=event['conversation']['id'];aid=event['assistant_message']['id'];self.active_conversations[identifier]=cid;self.mirror('chat.send',body,event,epoch);marker=self.store.get(['receipt',identifier]);self.store.put(['receipt',identifier],{**marker,'resource':{'conversation_id':cid}});emit('start',welcome_groups=event.get('welcome_groups',[]),welcome_cleanup=event.get('welcome_cleanup'))
    elif kind=='delta':
     text=str(event.get('delta',event.get('text','')))
     for offset in range(0,len(text),2048):emit('answer_delta',delta=text[offset:offset+2048])
    elif kind in {'thinking','tool_start','tool_done','web_sources','login_required'}:emit(kind,**{k:v for k,v in event.items()if k!='type'and k not in {'reasoning','reasoning_content'}})
    elif kind=='activity':
     callback=getattr(progress,'onActivity',None)
     if callable(callback):callback(encode(event.get('activity')or{}))
    elif kind=='result':
     result=event['result']
     if not cid:
      cid=result['conversation']['id'];aid=result['assistant_message']['id'];emit('start',welcome_groups=result.get('welcome_groups',[]),welcome_cleanup=result.get('welcome_cleanup'))
      text=str(result['assistant_message'].get('content',''))
      for offset in range(0,len(text),2048):emit('answer_delta',delta=text[offset:offset+2048])
    elif kind=='error':raise GuestError(event.get('code')if event.get('code')in {'cancelled','login_required'}else'guest_unavailable')
   if cancel.is_set():raise GuestError('cancelled','已停止回答。')
   if not isinstance(result,dict):raise GuestError('outcome_uncertain','回答连接暂时中断，请查看对话记录后再决定是否重新发送。')
   self.mirror('chat.send',body,result,epoch);self.guard(epoch);self.store.put(['receipt',identifier],{'digest':request_digest,'action':'chat.send','resource':{'conversation_id':body.get('conversation_id')},'complete':True,'result':result});emit('done');self.refresh(epoch);return result
  finally:
   with self.lock:
    if self.active.get(identifier)is cancel:self.active.pop(identifier,None)
    self.active_conversations.pop(identifier,None)
 def cancel(self,identifier):
  with self.lock:
   event=self.active.get(identifier)
   if event:event.set()
   return bool(event)
 def closing_scope(self):
  with self.lock:
   self.closing=True;self.epoch=str(uuid4())
   for event in self.active.values():event.set()
 def purge_cloud(self):
  with self.cloud_lock:
   credentials=self.credentials();token=credentials.get('token')
   if not token and credentials.get('creation_key'):
    recovered=self.cloud.request('bootstrap',{'creation_key':credentials['creation_key']});token=recovered.get('guest_token')
    if not token:raise GuestError('guest_cleanup_pending')
    self.store.put('credentials',{**credentials,'token':token,'cloud_guest_id':recovered['guest']['id']})
   if token:
    value=self.cloud.request('purge',{},token)
    if value.get('purged')is not True:raise GuestError('guest_cleanup_pending')
 def transcribe(self,capture_path,progress=None):
  from mobile_voice import read_capture,validate_cloud_result,validate_recognized_text
  epoch=self.epoch;self.guard(epoch);data=read_capture(self.store.root,capture_path);token=self.ensure_cloud(epoch);cancel=threading.Event();identifier=str(uuid4())
  with self.lock:self.guard(epoch);self.active[identifier]=cancel
  stop=threading.Event()
  def watch():
   while not stop.wait(.1):
    if not Path(capture_path).is_file():cancel.set();return
  threading.Thread(target=watch,daemon=True).start();result=None
  try:
   for event in self.cloud.lines('voice/transcribe',{'wav_base64':base64.b64encode(data).decode()},token,cancel):
    self.guard(epoch);kind=event.get('type')
    if cancel.is_set()or not Path(capture_path).is_file():raise GuestError('cancelled','本次语音已取消。')
    if result is not None:raise GuestError('cloud_unavailable','语音结果未能核对，请重新录音。')
    if kind=='partial':
     callback=getattr(progress,'onPartial',None)
     try:partial=validate_recognized_text(event.get('text'))
     except (ValueError,TypeError,UnicodeError):raise GuestError('cloud_unavailable','语音结果未能核对，请重新录音。')from None
     if partial and callable(callback):callback(partial)
    elif kind=='final':
     try:result=validate_cloud_result(event.get('result')or event)
     except (ValueError,TypeError,UnicodeError):raise GuestError('cloud_unavailable','语音结果未能核对，请重新录音。')from None
    elif kind=='error':raise GuestError('cloud_unavailable','语音服务暂时不可用，请稍后重新录入。')
    elif kind!='status'or event.get('status')!='recognizing':raise GuestError('cloud_unavailable','语音结果未能核对，请重新录音。')
   self.guard(epoch)
   if cancel.is_set()or not Path(capture_path).is_file():raise GuestError('cancelled','本次语音已取消。')
   if result is None:raise GuestError('cloud_unavailable')
   return {**result,'scope_id':'guest:'+self.id,'session_epoch':epoch}
  finally:
   stop.set()
   with self.lock:self.active.pop(identifier,None)

class GuestLifecycle:
 """Control-sealed cleanup journal outlives the Guest key being deleted."""
 def __init__(self,member_root,vault,endpoint,*,transport=None):
  self.base=Path(member_root).parent/'guest-ai11614';self.base.mkdir(exist_ok=True)
  if self.base.is_symlink():raise GuestError('guest_storage')
  self.vault=vault;self.endpoint=endpoint;self.transport=transport;self.journal=self.base/'pending-member.sealed';self.purpose='guest-cleanup11614/'+hashlib.sha256(endpoint.encode()).hexdigest();self.lock=threading.RLock();self.facade=None;self.pending=self.load()
 def load(self):
  if self.journal.is_symlink():raise GuestError('guest_storage')
  if not self.journal.exists():return None
  if self.journal.stat().st_size>256000:raise GuestError('guest_storage')
  value=json.loads(str(self.vault.openControl(self.purpose,self.journal.read_text(encoding='utf-8'))))
  if value.get('version')!=11614 or value.get('phase')not in {'prepared','cloud_done','local_done'}:raise GuestError('guest_storage')
  uid(value['guest_id']);return value
 def save(self):
  sealed=str(self.vault.sealControl(self.purpose,encode(self.pending)));atomic(self.journal,sealed)
 def activate(self):
  if self.pending:return None
  if self.facade is None:self.facade=GuestFacade(self.base,self.vault,self.endpoint,transport=self.transport)
  return self.facade
 def descriptor(self):
  if self.pending:return {'enabled':True,'id':self.pending['guest_id'],'session_epoch':self.pending['session_epoch'],'cleanup_pending':True}
  return self.facade.descriptor()if self.facade else None
 def begin(self,session,reply,auto_login):
  if not self.facade:return False
  self.pending={'version':11614,'phase':'prepared','guest_id':self.facade.id,'session_epoch':str(uuid4()),'credentials':self.facade.credentials(),'session':session,'reply':reply,'auto_login':auto_login}
  self.save();self.facade.closing_scope();return True
 def cleanup(self,native_hooks=None):
  with self.lock:
   if not self.pending:return
   p=self.pending
   if self.facade:self.facade.closing_scope()
   callback=getattr(native_hooks,'invalidateGuestScope',None)
   if callable(callback):callback()
   if p['phase']=='prepared':
    # Wait only for bootstrap's capability commit. Inference itself is cancelled
    # and never joined here; its late delivery/write guards already fail closed.
    if self.facade:
     with self.facade.cloud_lock:p['credentials']=self.facade.credentials();self.save()
     self.facade.cloud.close()
    credentials=p['credentials'];token=credentials.get('token')
    with GuestCloud(self.endpoint,transport=self.transport)as cloud:
     if not token and credentials.get('creation_key'):
      try:
       value=cloud.request('bootstrap',{'creation_key':credentials['creation_key']});token=value.get('guest_token')
       if not token:raise GuestError('guest_cleanup_pending')
       p['credentials']={**credentials,'token':token,'cloud_guest_id':value['guest']['id']};self.save()
      except GuestError as error:
       if error.code!='guest_session_gone':raise
     if token:
      value=cloud.request('purge',{},token)
      if value.get('purged')is not True:raise GuestError('guest_cleanup_pending')
    p['phase']='cloud_done';self.save()
   if p['phase']=='cloud_done':
    if self.facade:self.facade.store.close()
    directory=self.base/p['guest_id'];remove_private(directory,self.base)
    camera_base=self.base/'picked-images'
    if camera_base.is_symlink():raise GuestError('guest_storage')
    remove_private(camera_base/p['guest_id'],camera_base)
    self.vault.clearGuestKey(p['guest_id'])
    pointer=self.base/'active.json'
    if pointer.is_symlink():raise GuestError('guest_storage')
    if pointer.exists():
     if json.loads(pointer.read_text(encoding='utf-8')).get('id')!=p['guest_id']:raise GuestError('guest_storage')
     pointer.unlink()
    p['phase']='local_done';self.save()
 def finish(self):
  if not self.pending or self.pending['phase']!='local_done':raise GuestError('guest_cleanup_pending')
  if self.journal.is_symlink():raise GuestError('guest_storage')
  self.journal.unlink(missing_ok=True);self.pending=None;self.facade=None
