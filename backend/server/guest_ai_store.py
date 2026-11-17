"""Guest capability scoped history/media. Never reads or writes member tables."""
from __future__ import annotations
import base64,copy,hashlib,hmac,json,re,secrets,sqlite3
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import PurePath
from uuid import UUID,uuid4

from .guest_ai_schema import SCHEMA

MAX_STATE=32*1024*1024
PREFERENCES={'ai_context_consent':True,'ai_personal_context_consent':True,'ai_thinking_enabled':True,
 'ai_reasoning_effort':'high','ai_web_enabled':True,'ai_tools_enabled':True,'ai_complexity':'balanced',
 'ai_tone':'warm','font_scale':1.0}
READ_ACTIONS={'preferences.get','chat.conversations.list','chat.messages.list','chat.files.list','files.get',
 'chat.message.attachments','chat.image.get','chat.tools.proposals','guest.memory.get','ai.operations.status'}
WRITE_ACTIONS={'preferences.update','chat.conversations.create','chat.conversations.rename','chat.conversations.delete',
 'files.upload','files.delete','guest.memory.update','ai.operations.cancel'}


class GuestError(RuntimeError):
 def __init__(self,code='invalid_guest_request',status=422):self.code,self.status=code,status;super().__init__(code)
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def stamp():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def only(body,allowed,required=()):
 if type(body)is not dict or set(body)-set(allowed)or not set(required)<=set(body):raise GuestError()
 return body
def positive(value):
 if type(value)is not int or not 0<value<=9223372036854775807:raise GuestError()
 return value
def text(value,maximum,empty=False):
 if type(value)is not str or len(value)>maximum or not empty and not value.strip():raise GuestError()
 return value.strip()
def request_uuid(value):
 try:
  if type(value)is not str or str(UUID(value))!=value:raise ValueError()
 except(ValueError,TypeError,AttributeError):raise GuestError('invalid_guest_request_id')from None
 return value


def prepare_guest_attachment(name,raw):
 """Server image validation is Pillow-only; desktop Qt is never imported."""
 from ollama_chat_app.services.chat_attachments import ChatAttachment,IMAGE_TYPES,prepare_attachment_bytes
 from .platform_uploads import validate_image,validate_chat_uploads
 media_type=IMAGE_TYPES.get(PurePath(name).suffix.casefold())
 if media_type:
  validate_image(raw,media_type)
  attachment=ChatAttachment(name,media_type,raw,'','native_image',hashlib.sha256(raw).hexdigest())
 else:attachment=prepare_attachment_bytes(name,raw)
 validate_chat_uploads((attachment,));return attachment


class GuestStore:
 def __init__(self,database,pepper):
  self.db=database;self.postgres=getattr(database,'dialect','sqlite')=='postgresql'
  self.key=hmac.new(bytes.fromhex(pepper),b'fawnsee-guest-capability-v1',hashlib.sha256).digest()
 def table(self,name):return SCHEMA+'.'+name if self.postgres else name
 def digest(self,token):
  if type(token)is not str or re.fullmatch(r'gs1_[A-Za-z0-9_-]{43}',token)is None:raise GuestError('guest_authentication_required',401)
  return hmac.new(self.key,token.encode(),hashlib.sha256).hexdigest()
 @contextmanager
 def scope(self,token,write=False):
  digest=self.digest(token)
  with(self.db.transaction()if write else self.db.connect())as connection:
   if self.postgres:connection.execute("SELECT pg_catalog.set_config('medical_app.guest_digest',?,true)",(digest,))
   yield connection,digest
 def row(self,connection,digest,lock=False):
  row=connection.execute('SELECT * FROM '+self.table('guest_ai_sessions')+' WHERE token_digest=?'+(' FOR UPDATE'if lock and self.postgres else''),(digest,)).fetchone()
  if row is None:raise GuestError('guest_session_gone',410)
  return row
 def load(self,token):
  with self.scope(token)as(connection,digest):return dict(self.row(connection,digest))
 def active(self,token):
  # Stream events need current authorization, not the entire conversation state.
  with self.scope(token)as(connection,digest):
   row=connection.execute('SELECT guest_id FROM '+self.table('guest_ai_sessions')+' WHERE token_digest=?',(digest,)).fetchone()
   if row is None:raise GuestError('guest_session_gone',410)
   return row['guest_id']
 def operation_status(self,token,operation):
  """Read only the current operation state; never materialize chat history."""
  operation=request_uuid(operation)
  if self.postgres:
   select="SELECT (state_json::json -> 'receipts' -> CAST(? AS text) ->> 'state') AS operation_state FROM "
  else:
   select="SELECT json_extract(state_json, '$.receipts.\""+operation+"\".state') AS operation_state FROM "
  with self.scope(token)as(connection,digest):
   params=(operation,digest)if self.postgres else(digest,)
   row=connection.execute(select+self.table('guest_ai_sessions')+' WHERE token_digest=?',params).fetchone()
   if row is None:raise GuestError('guest_session_gone',410)
   return row['operation_state']or'not_found'
 def memory_settings(self,token,actor):
  """Fresh authorization and a narrow memory projection, without a TTL cache."""
  projection=("(state_json::json -> 'memory')::text AS memory_json, "
     "(state_json::json -> 'preferences' ->> 'ai_personal_context_consent') AS consent")if self.postgres else(
     "json_extract(state_json, '$.memory') AS memory_json, "
     "json_extract(state_json, '$.preferences.ai_personal_context_consent') AS consent")
  with self.scope(token)as(connection,digest):
   row=connection.execute('SELECT guest_id, '+projection+' FROM '+self.table('guest_ai_sessions')+' WHERE token_digest=?',(digest,)).fetchone()
   if row is None:raise GuestError('guest_session_gone',410)
   if row['guest_id']!=actor:raise GuestError('guest_authentication_required',401)
   consent=row['consent']in(True,1,'true')
   memory=json.loads(row['memory_json'])
   revision=hashlib.sha256(encoded({'memory':memory,'consent':consent}).encode()).hexdigest()
   return {'enabled':consent,'revision':revision}
 def initial(self):return {'preferences':dict(PREFERENCES),'conversations':[],'messages':{},'memory':{'entries':[]},'receipts':{},'next_id':1}
 def creation_token(self,creation_key):
  if type(creation_key)is not str or re.fullmatch(r'[A-Za-z0-9_-]{43}',creation_key)is None:raise GuestError('invalid_guest_creation_key')
  raw=hmac.new(self.key,b'guest-creation-v1\0'+creation_key.encode(),hashlib.sha256).digest()
  return 'gs1_'+base64.urlsafe_b64encode(raw).decode().rstrip('=')
 def bootstrap(self,token=None,creation_key=None):
  if token is not None:row=self.load(token)
  else:
   token=self.creation_token(creation_key);now=stamp();state=self.initial()
   with self.scope(token,True)as(connection,digest):
    if connection.execute('SELECT token_digest FROM '+self.table('guest_ai_retired')+' WHERE token_digest=?',(digest,)).fetchone():raise GuestError('guest_session_gone',410)
    connection.execute('INSERT INTO '+self.table('guest_ai_sessions')+'(token_digest,guest_id,created_at,updated_at,revision,state_json) VALUES(?,?,?,?,?,?) ON CONFLICT(token_digest) DO NOTHING',(digest,str(uuid4()),now,now,1,encoded(state)))
   row=self.load(token)
  return {'guest':{'id':row['guest_id'],**{key:row[key]for key in('revision','created_at','updated_at')}},'guest_token':token,'state':self.snapshot(token,row=row)}
 def snapshot(self,token,row=None):
  row=row or self.load(token);state=json.loads(row['state_json'])
  with self.scope(token)as(connection,digest):files=self._files(connection,digest)
  return {**{key:copy.deepcopy(state[key])for key in('preferences','conversations','messages','memory')},'files':files,'revision':row['revision']}
 def purge(self,token):
  with self.scope(token,True)as(connection,digest):
   row=connection.execute('SELECT guest_id FROM '+self.table('guest_ai_sessions')+' WHERE token_digest=?',(digest,)).fetchone()
   connection.execute('INSERT INTO '+self.table('guest_ai_retired')+'(token_digest,retired_at) VALUES(?,?) ON CONFLICT(token_digest) DO NOTHING',(digest,stamp()))
   connection.execute('DELETE FROM '+self.table('guest_ai_sessions')+' WHERE token_digest=?',(digest,))
  return {'purged':True,'guest_id':row['guest_id']if row else None}
 def _files(self,connection,digest):return [dict(row)for row in connection.execute('SELECT file_id AS id,original_name,media_type,length(content) AS size_bytes,sha256,created_at FROM '+self.table('guest_ai_media')+' WHERE token_digest=? ORDER BY file_id',(digest,)).fetchall()]
 def media(self,token,file_id):
  with self.scope(token)as(connection,digest):
   self.row(connection,digest)
   row=connection.execute('SELECT * FROM '+self.table('guest_ai_media')+' WHERE token_digest=? AND file_id=?',(digest,positive(file_id))).fetchone()
  if row is None:raise GuestError('guest_file_not_found',404)
  result=dict(row);result['content']=bytes(result['content']);return result
 def mutate(self,token,callback):
  with self.scope(token,True)as(connection,digest):
   row=self.row(connection,digest,True);state=json.loads(row['state_json']);result=callback(state,connection,digest)
   raw=encoded(state)
   if len(raw.encode())>MAX_STATE:raise GuestError('guest_storage_full',413)
   changed=connection.execute('UPDATE '+self.table('guest_ai_sessions')+' SET state_json=?,revision=revision+1,updated_at=? WHERE token_digest=?',(raw,stamp(),digest)).rowcount
   if changed!=1:raise GuestError('guest_session_gone',410)
   return result
 @staticmethod
 def next_id(state):value=state['next_id'];state['next_id']+=1;return value
 @staticmethod
 def conversation(state,identifier):
  for row in state['conversations']:
   if row['id']==positive(identifier):return row
  raise GuestError('guest_conversation_not_found',404)
 def _create(self,state,title='新对话'):
  if len(state['conversations'])>=200:raise GuestError('guest_conversation_limit',413)
  now=stamp();row={'id':self.next_id(state),'title':text(title,120),'created_at':now,'updated_at':now,'provider':'deepseek_cloud','model':None,'message_count':0}
  state['conversations'].append(row);state['messages'][str(row['id'])]=[];return row
 def dispatch(self,token,action,params,request_id=None):
  if action=='chat.greeting.ensure':
   return self.mutate(token,lambda state,connection,digest:self._action(state,connection,digest,action,params))
  if action not in READ_ACTIONS|WRITE_ACTIONS:raise GuestError('login_required',403)
  if type(params)is not dict:raise GuestError()
  params=dict(params);inner=params.pop('request_id',None)
  if inner is not None:
   request_uuid(inner)
   if request_id is not None and request_id!=inner:raise GuestError('guest_request_conflict',409)
   request_id=inner
  if action in WRITE_ACTIONS:request_uuid(request_id)
  elif request_id is not None:request_uuid(request_id)
  fingerprint=hashlib.sha256(encoded({'action':action,'params':params}).encode()).hexdigest()
  def apply(state,connection,digest):
   receipt=state['receipts'].get(request_id)if request_id and action in WRITE_ACTIONS else None
   if receipt:
    if receipt['hash']!=fingerprint:raise GuestError('guest_request_conflict',409)
    return copy.deepcopy(receipt['result'])
   result=self._action(state,connection,digest,action,params)
   if action in WRITE_ACTIONS:
    receipt={'hash':fingerprint,'result':copy.deepcopy(result)}
    if action in('chat.conversations.create','chat.conversations.rename'):receipt['conversation_id']=result['id']
    state['receipts'][request_id]=receipt
    while len(state['receipts'])>512:state['receipts'].pop(next(iter(state['receipts'])))
   return copy.deepcopy(result)
  if action in WRITE_ACTIONS:return self.mutate(token,apply)
  with self.scope(token)as(connection,digest):
   row=self.row(connection,digest);return apply(json.loads(row['state_json']),connection,digest)
 def _action(self,state,connection,digest,action,body):
  if action in('ai.operations.status','ai.operations.cancel'):
   only(body,('kind','operation_id'),('kind','operation_id'))
   if body['kind']!='chat.send':raise GuestError('login_required',403)
   operation=request_uuid(body['operation_id']);receipt=state['receipts'].get(operation);status=receipt.get('state','not_found')if receipt else'not_found'
   if action=='ai.operations.cancel'and status=='running':
    receipt['state']=status='cancelled'
    for row in state['messages'].get(str(receipt['conversation_id']),[]):
     if row['id']==receipt['assistant_id']:row.update(status='error',error_message='已停止回答。',updated_at=stamp())
   value={'operation_id':operation,'kind':'chat.send','state':status,'retry_safe':status in('not_found','failed','cancelled'),'requires_new_operation':status!='running'}
   if status=='complete':value['result']=receipt['result']
   return value
  if action=='chat.greeting.ensure':
   only(body,('conversation_id','local_hour','boot_id'),('conversation_id','local_hour'))
   from ollama_chat_app.services.first_meet_greeting import opening_text,valid_boot_id,BOOT_MODEL_PREFIX,welcome_groups_from_rows
   hour=body['local_hour']
   if type(hour)is not int or not 0<=hour<=23:raise GuestError()
   try:boot=valid_boot_id(body.get('boot_id'))
   except ValueError:raise GuestError()from None
   conv=self.conversation(state,body['conversation_id']);rows=state['messages'][str(conv['id'])]
   marker=BOOT_MODEL_PREFIX+boot if boot else None
   if marker:
    found=next((row for items in state['messages'].values()for row in items if row.get('provider')=='fawnsee-welcome'and row.get('model')==marker),None)
    if found and found['conversation_id']!=conv['id']:raise GuestError('guest_request_conflict',409)
   else:found=next((row for row in rows if row.get('provider')=='fawnsee-welcome'),None)
   if found:
    result={'message':copy.deepcopy(found),'created':False,'welcome_groups':welcome_groups_from_rows(rows)}
    if boot:result['boot_id']=boot
    return result
   # Fold the old unanswered tail before constructing the new opening. Guest
   # mutate commits this receipt and message together within its scoped lock.
   groups=welcome_groups_from_rows(rows,fold_pending=True)
   sequence=max((row['sequence_no']for row in rows),default=0)+1 if boot else 0
   now=stamp();row={'id':self.next_id(state),'conversation_id':conv['id'],'sequence_no':sequence,'role':'assistant',
    'content':opening_text('您好',hour),'status':'complete','error_message':None,'provider':'fawnsee-welcome',
    'model':marker,'created_at':now,'updated_at':now,'attachments':[]}
   rows.append(row);rows.sort(key=lambda item:item['sequence_no']);conv['updated_at']=now;conv['message_count']=len(rows)
   result={'message':copy.deepcopy(row),'created':True,'welcome_groups':groups}
   if boot:result['boot_id']=boot
   return result
  if action=='preferences.get':only(body,());return state['preferences']
  if action=='preferences.update':
   only(body,PREFERENCES)
   for key,value in body.items():
    if key.endswith(('_consent','_enabled'))and type(value)is not bool:raise GuestError()
    if key=='ai_reasoning_effort'and value not in('low','high','max'):raise GuestError()
    if key in('ai_complexity','ai_tone'):text(value,40)
    if key=='font_scale'and(type(value)not in(int,float)or not .75<=value<=2):raise GuestError()
   state['preferences'].update(body);return state['preferences']
  if action=='chat.conversations.list':only(body,());return sorted(state['conversations'],key=lambda row:row['updated_at'],reverse=True)
  if action=='chat.conversations.create':only(body,('title',));return self._create(state,body.get('title','新对话'))
  if action=='chat.conversations.rename':
   only(body,('conversation_id','title'),('conversation_id','title'));row=self.conversation(state,body['conversation_id']);row['title']=text(body['title'],120);row['updated_at']=stamp();return row
  if action=='chat.conversations.delete':
   only(body,('conversation_ids',),('conversation_ids',));ids=body['conversation_ids']
   if type(ids)is not list or not 1<=len(ids)<=200 or any(type(i)is not int or i<=0 for i in ids)or len(set(ids))!=len(ids):raise GuestError()
   for identifier in ids:self.conversation(state,identifier)
   state['conversations']=[row for row in state['conversations']if row['id']not in ids]
   for identifier in ids:state['messages'].pop(str(identifier),None)
   state['receipts']={key:row for key,row in state['receipts'].items()if row.get('conversation_id')not in ids}
   return len(ids)
  if action=='chat.messages.list':
   only(body,('conversation_id','limit','before_sequence_no'));identifier=body.get('conversation_id')
   if identifier is None:
    if not state['conversations']:return {'conversation':None,'messages':[],'has_more':False,'next_before_sequence_no':None}
    identifier=state['conversations'][-1]['id']
   row=self.conversation(state,identifier);limit=body.get('limit',40)
   if type(limit)is not int or not 1<=limit<=200:raise GuestError()
   cursor=body.get('before_sequence_no');items=state['messages'][str(identifier)]
   if cursor is not None:items=[item for item in items if item['sequence_no']<positive(cursor)]
   selected=items[-limit:]
   from ollama_chat_app.services.first_meet_greeting import welcome_groups_from_rows,annotate_welcome_rows
   groups=welcome_groups_from_rows(state['messages'][str(identifier)],selected_ids={item['id']for item in selected})
   return {'conversation':row,'messages':annotate_welcome_rows(selected,groups),'welcome_groups':groups,'has_more':len(items)>limit,'next_before_sequence_no':selected[0]['sequence_no']if len(items)>limit else None}
  if action=='chat.files.list':only(body,());return self._files(connection,digest)
  if action=='files.upload':
   only(body,('original_name','content'),('original_name','content'));name=text(body['original_name'],255)
   if '/'in name or '\\'in name or name in('.', '..'):raise GuestError()
   value=body['content']
   if type(value)is not str or not 1<=len(value)<=24*1024*1024:raise GuestError('guest_file_too_large',413)
   if value.startswith('data:'):
    header,separator,value=value.partition(',')
    if not separator or re.fullmatch(r'data:[^,;]{1,100}(?:;[^,;]{1,40})*;base64',header,re.I)is None:raise GuestError()
   try:raw=base64.b64decode(value,validate=True)
   except ValueError:raise GuestError()from None
   if not 0<len(raw)<=15*1024*1024:raise GuestError('guest_file_too_large',413)
   total=connection.execute('SELECT COALESCE(SUM(length(content)),0) FROM '+self.table('guest_ai_media')+' WHERE token_digest=?',(digest,)).fetchone()[0]
   if total+len(raw)>100*1024*1024:raise GuestError('guest_storage_full',413)
   try:attachment=prepare_guest_attachment(name,raw)
   except Exception:raise GuestError('invalid_guest_attachment')from None
   identifier=self.next_id(state)
   connection.execute('INSERT INTO '+self.table('guest_ai_media')+'(token_digest,file_id,original_name,media_type,content,extracted_text,sha256,created_at) VALUES(?,?,?,?,?,?,?,?)',
    (digest,identifier,name,attachment.media_type,sqlite3.Binary(raw),attachment.extracted_text,attachment.sha256,stamp()))
   return identifier
  if action in('files.get','files.delete','chat.image.get'):
   field='attachment_id'if action=='chat.image.get'else'file_id';only(body,(field,),(field,));identifier=positive(body[field])
   row=connection.execute('SELECT * FROM '+self.table('guest_ai_media')+' WHERE token_digest=? AND file_id=?',(digest,identifier)).fetchone()
   if row is None:raise GuestError('guest_file_not_found',404)
   if action=='files.delete':connection.execute('DELETE FROM '+self.table('guest_ai_media')+' WHERE token_digest=? AND file_id=?',(digest,identifier));return None
   value={field:identifier,'media_type':row['media_type'],'size_bytes':len(row['content']),'sha256':row['sha256'],'content':base64.b64encode(row['content']).decode()}
   if action=='files.get':value.update(original_name=row['original_name'],extracted_text=row['extracted_text'])
   elif not row['media_type'].startswith('image/'):raise GuestError()
   return value
  if action=='chat.message.attachments':
   only(body,('message_id',),('message_id',));identifier=positive(body['message_id'])
   found=next((row for rows in state['messages'].values()for row in rows if row['id']==identifier),None)
   if found is None:raise GuestError('guest_message_not_found',404)
   ids=found.get('attachments',[]);return [row for row in self._files(connection,digest)if row['id']in ids]
  if action=='chat.tools.proposals':
   only(body,('assistant_message_id','conversation_id','assistant_message_ids'))
   rows=[row for messages in state['messages'].values()for row in messages if row['role']=='assistant']
   if body.get('assistant_message_id')is not None:
    row=next((r for r in rows if r['id']==positive(body['assistant_message_id'])),None)
    if row is None:raise GuestError('guest_message_not_found',404)
    return {'assistant_message_id':row['id'],'sources':row.get('sources',[]),'proposals':[]}
   identifier=body.get('conversation_id');self.conversation(state,identifier)
   return {'messages':[{'assistant_message_id':row['id'],'sources':row.get('sources',[]),'proposals':[]}for row in state['messages'][str(identifier)]if row['role']=='assistant']}
  if action=='guest.memory.get':only(body,());return state['memory']
  if action=='guest.memory.update':
   only(body,('entries',),('entries',));entries=body['entries']
   if type(entries)is not list or len(entries)>100:raise GuestError()
   cleaned=[]
   for item in entries:
    only(item,('text','kind','date'),('text',));kind=item.get('kind','user_confirmed')
    if kind not in('user_confirmed','raw_record','model_inference'):raise GuestError()
    date=item.get('date',stamp());text(date,80);cleaned.append({'text':text(item['text'],2000),'kind':kind,'date':date})
   state['memory']={'entries':cleaned};return state['memory']
  raise GuestError('login_required',403)
