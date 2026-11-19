"""Confirm authenticated conversation drafts through the local record service."""
from copy import deepcopy
from uuid import UUID

from mobile_local149 import LocalDataError
from mobile_providers import validate_record_candidates
from ollama_chat_app.providers.platform_deepseek import RECORD_DIALOGUE_MAX_DECISIONS, RECORD_PHOTO_CATEGORIES
from ollama_chat_app.services.record_photos11839 import photo_ids


RECORD_DECISIONS=frozenset({'chat.records.confirm','chat.records.cancel'})
_KNOWN_WRITE_ACTIONS=frozenset({
    'health.records.add','expansion.records.update',
    'expansion.observations.create','expansion.observations.update',
})
_RECENT_DECISIONS_KEY = 'record-assistant:recent-decisions-v1'


def _scope_marker(identifier):
    return 'record-assistant:'+identifier


def recent_decided_record_drafts(local, identity):
    local._guard(identity)
    return local.store.get(identity, 'meta', _RECENT_DECISIONS_KEY) or []


def _remember_decision(local, identity, identifier):
    previous = recent_decided_record_drafts(local, identity)
    updated = [key for key in previous if key != identifier]
    updated.append(identifier)
    updated = updated[-RECORD_DIALOGUE_MAX_DECISIONS:]
    if updated != previous:
        local.store.put(identity, 'meta', _RECENT_DECISIONS_KEY, updated)


def _decision(body,operation):
    identifier=body.get('draft_id')
    message=body.get('assistant_message_id')
    if (set(body)!={'draft_id','assistant_message_id','request_id','_fawnsee_actor_id'}
            or type(message) is not int or not 0<message<=9007199254740991 or body['request_id']!=operation):
        raise LocalDataError('validation','记录草稿编号不完整，请回到原对话核对。')
    try:
        if type(identifier) is not str or str(UUID(identifier))!=identifier:
            raise ValueError
    except ValueError:
        raise LocalDataError('validation','记录草稿编号无效，请回到原对话核对。') from None
    return identifier,message


def _receipt(store,identity,identifier,marker):
    if marker['cancelled']:
        return {'id':identifier,'status':'cancelled'} if marker.get('cloud_cancelled') is True else None
    if 'linked_receipt' in marker:
        return dict(marker['linked_receipt'])
    operation=store.operation(identity,identifier)
    if operation is None or operation.get('action') not in _KNOWN_WRITE_ACTIONS:
        return None
    raw_status=operation['status']
    if raw_status=='discarded':
        return {'id':identifier,'status':'discarded'}
    synced=raw_status in {'applied','confirmed'}
    entity=(operation.get('result_entity') or operation.get('result')) if synced else operation.get('local_result')
    if type(entity) is not dict or type(entity.get('id')) is not int or entity['id']==0:
        raise LocalDataError('local_protocol','记录保存结果待核对，请查看记录列表。')
    status='saved' if synced else 'sync_issue' if raw_status in {'rejected','conflict'} else 'pending_sync'
    return {'id':identifier,'status':status,'category':marker['category'],
            'record_id':entity['id'],'version':entity.get('version')}


def _verified_draft(online,message,identifier):
    reply=online('chat.tools.proposals',{'assistant_message_id':message},None)
    if not reply.get('ok'):
        return None,reply
    metadata=reply.get('result') or {}
    draft=next((row for row in metadata.get('record_drafts',[]) if row['id']==identifier),None)
    if (metadata.get('assistant_message_id')!=message or draft is None or 'operation' not in draft
            or metadata.get('record_drafts_ready') is not True):
        raise LocalDataError('not_found','这份草稿尚未生成完成或已不可用，请回到原对话重新核对。')
    if draft.get('superseded') is True:
        raise LocalDataError('conflict','这份草稿已有更新，请核对最新草稿。')
    if any(r.get('id') == identifier and r.get('status') == 'cancelled'
           for r in metadata.get('record_receipts',[]) if isinstance(r,dict)):
        raise LocalDataError('conflict','这份草稿已取消，请回到原对话。')
    return draft,None


def _cloud_cancel(local, identity, online, message, identifier, marker):
    local._guard(identity)
    reply = online('chat.records.cancel', {'assistant_message_id':message, 'draft_id':identifier}, None)
    local._guard(identity)
    if reply.get('ok') is not True:
        return reply
    receipt = {'id':identifier, 'status':'cancelled'}
    if reply.get('result') != receipt:
        raise LocalDataError('local_protocol','取消结果还没核对，请重试原草稿。')
    updated = {**(marker or {}), 'message':message, 'cancelled':True, 'cloud_cancelled':True}
    local.store.put(identity,'meta',_scope_marker(identifier),updated)
    return {'ok':True, 'result':receipt}


def list_pending_tasks(local, identity, online, body):
    payload = {k:v for k,v in body.items() if k != '_fawnsee_actor_id'}
    action = 'chat.record_tasks.list'
    with local.record_assistant_lock:
        local._guard(identity)
        response = online(action,payload,None)
        local._guard(identity)
        if not response.get('ok') or not response['result'].get('total'):
            return response
        # Older versions kept the cancellation only on this device. Look at
        # current server tasks before changing anything, including later pages.
        conversation = payload['conversation_id']
        scan_key = 'record-task-cancel-migration-v1:' + str(conversation)
        scanned = local.store.get(identity,'meta',scan_key) is True
        page = response['result']
        scan_params = {'conversation_id':conversation,'limit':40}
        if not scanned and (payload.get('query') or payload.get('cursor') or page.get('next_cursor')):
            page_reply = online(action,scan_params,None)
            local._guard(identity)
            if not page_reply.get('ok'):
                return response
            page = page_reply['result']
        pending = {}
        seen_cursors = set()
        while True:
            for row in page.get('tasks',[]):
                if not isinstance(row,dict):
                    continue
                draft, message = row.get('draft_id'), row.get('assistant_message_id')
                if not isinstance(draft,str) or type(message) is not int or message <= 0:
                    continue
                marker = local.store.get(identity,'meta',_scope_marker(draft))
                if (marker and marker.get('message') == message and marker.get('cancelled') is True
                        and marker.get('cloud_cancelled') is not True):
                    pending[draft] = (message,marker)
            cursor = page.get('next_cursor')
            if scanned or not cursor:
                break
            if not isinstance(cursor,str) or cursor in seen_cursors:
                raise LocalDataError('local_protocol','待完成记录分页位置不一致，请稍后重试。')
            seen_cursors.add(cursor)
            next_page = online(action,{**scan_params,'cursor':cursor},None)
            local._guard(identity)
            if not next_page.get('ok'):
                return response
            page = next_page['result']
        # Do not invalidate a cursor by cancelling its rows while traversing it.
        complete = True
        for draft,(message,marker) in pending.items():
            reply = _cloud_cancel(local,identity,online,message,draft,marker)
            if not reply.get('ok'):
                complete = False
        if not scanned and complete:
            local.store.put(identity,'meta',scan_key,True)
        if pending:
            response = online(action,payload,None)
            local._guard(identity)
        return response


def _useful(value):
    return value is not None and (not isinstance(value,str) or bool(value.strip()))


def _business_write(draft):
    """Construct a single reviewed write, using only the record form's fields."""
    if draft['blocked'] or draft['missing_required']:
        raise LocalDataError('validation','必要资料还没补齐，这条记录暂时不能保存。')
    category=draft['category']
    purpose=draft['operation']
    fields={key:value for key,value in draft['details'].items() if _useful(value)}
    # The verified server receipt binds chat uploads to the proposed record.
    # The confirmation request itself cannot introduce any new file references.
    if 'photo_file_ids' in draft:
        try:
            references=photo_ids(draft['photo_file_ids'])
        except ValueError as error:
            raise LocalDataError('validation',str(error)) from None
        if category not in RECORD_PHOTO_CATEGORIES or not references:
            raise LocalDataError('validation','照片关联无效，请重新生成记录。')
        fields['photo_file_ids']=references
    # Unspecified context is just the form's neutral default. Empty custom
    # units are legitimate, so keep that field distinct from missing values.
    if category=='metric' and draft['details'].get('kind')=='custom':
        fields.setdefault('unit',draft['details'].get('unit',''))
    try:
        single=validate_record_candidates([{**{key:draft[key] for key in ('category','occurred_at','content')},'details':fields}])[0]
    except ValueError as error:
        raise LocalDataError('validation',str(error)) from None
    required={'water':'amount_ml','diet':'food','sleep':'duration_hours','medical':'name'}.get(category)
    if required and not _useful(fields.get(required)):
        raise LocalDataError('validation','记录缺少必要信息，请补齐后再确认。')
    if category=='activity' and not (_useful(fields.get('activity')) or _useful(fields.get('activity_type'))):
        raise LocalDataError('validation','请补充运动方式后再保存。')
    if category=='environment' and not any(_useful(fields.get(key)) for key in (
            'description','temperature_c','humidity_percent','ventilation_minutes','quality_score')):
        raise LocalDataError('validation','请补充一项环境情况后再保存。')
    if category=='metric':
        payload={**single['details'],'measured_at':single['occurred_at']}
        if purpose=='create':
            return 'expansion.observations.create',payload,None
    else:
        payload=single
        if purpose=='create':
            return 'health.records.add',{**payload,'source':'ai_confirmed'},None
    target=draft['target']
    if target is None:
        raise LocalDataError('validation','请先明确要修改的是哪条记录。')
    if target.get('linked_meal_id'):
        if 'photo_file_ids' in fields:
            raise LocalDataError('validation','这条记录关联餐食明细，请在原记录中管理照片。')
        meal=target.get('linked_meal')
        if meal is None:
            raise LocalDataError('validation','这条记录关联餐食明细，请在原餐食明细中调整。')
        return 'expansion.meals.update',{'meal_id':meal['id'],'base_version':meal['version'],**meal['changes']},target
    reason='本人通过记录助手'+('补充记录' if purpose=='supplement' else '修改记录')
    if category=='metric':
        return 'expansion.observations.update',{
            'observation_id':target['id'],'base_version':target['version'],'reason':reason,
            **{key:value for key,value in payload.items() if key!='kind'},
        },target
    return 'expansion.records.update',{
        'record_id':target['id'],'base_version':target['version'],'changes':payload,'reason':reason,
    },target


def _linked_record(online,target,actor):
    reply=online('expansion.records.get',{'record_id':target['id']},None)
    if not reply.get('ok'):
        return None,reply
    row=reply.get('result')
    if (not isinstance(row,dict) or row.get('id')!=target['id'] or row.get('user_id')!=actor
            or row.get('linked_meal_id')!=target['linked_meal_id']):
        raise LocalDataError('local_protocol','餐食记录归属未能核对，请重新读取。')
    return row,None


def _confirm_linked(local,identity,online,identifier,key,marker,payload,target):
    actor=identity.actor_id
    if marker.get('linked_submitted') is not True:
        current,error=_linked_record(online,target,actor)
        if error:
            return error
        if current.get('version')!=target['version']:
            raise LocalDataError('conflict','这条餐食记录已变更，请重新核对后再确认。')
        marker['linked_submitted']=True
        local._guard(identity)
        local.store.put(identity,'meta',key,marker)
    # Meal CAS updates both the linked life record and its meal in one cloud
    # transaction. The existing RPC journal replays an interrupted response.
    reply=online('expansion.meals.update',{**payload,'request_id':identifier},identifier)
    local._guard(identity)
    if not reply.get('ok'):
        if (reply.get('error') or {}).get('code') in {'validation','conflict','not_found'}:
            marker.pop('linked_submitted',None)
            local.store.put(identity,'meta',key,marker)
        return reply
    saved=reply.get('result')
    if (not isinstance(saved,dict) or saved.get('id')!=target['linked_meal_id']
            or saved.get('user_id')!=actor or saved.get('life_record_id')!=target['id']
            or not isinstance(saved.get('version'),str) or not saved['version']):
        raise LocalDataError('local_protocol','餐食保存结果待核对，请重试原卡片。')
    marker['linked_receipt']={'id':identifier,'status':'saved','category':'diet','record_id':target['id']}
    local.store.put(identity,'meta',key,marker)
    try:
        entity,_=_linked_record(online,target,actor)
        if entity is not None:
            local._adopt_confirmed_record(identity,{'status':'saved','kind':'life_record',
                'actor_id':actor,'result_entity':entity,'result':{'id':target['id']},
                'proposal_id':identifier,'persistence_verified':True})
            marker['linked_receipt']['version']=entity['version']
            local.store.put(identity,'meta',key,marker)
    finally:
        local._ask_sync(force=True)
    local._guard(identity)
    return {'ok':True,'result':dict(marker['linked_receipt']),'_local149':local.status()}


def decide(local,identity,online,action,body,operation):
    """Use the draft UUID as the write UUID across process restarts and retries."""
    identifier,message=_decision(body,operation)
    if type(body['_fawnsee_actor_id']) is not int or body['_fawnsee_actor_id']!=identity.actor_id:
        raise LocalDataError('account_changed','账号已改变，请回到原对话。')
    confirm=action=='chat.records.confirm'
    if confirm and identifier!=operation:
        raise LocalDataError('validation','确认编号与原草稿不一致，请从原卡片确认。')
    key=_scope_marker(identifier)
    with local.record_assistant_lock:
        local._guard(identity)
        marker=local.store.get(identity,'meta',key)
        if marker is not None:
            if marker['message']!=message:
                raise LocalDataError('not_found','这份草稿不属于当前消息，请回到原对话。')
            if confirm and marker.get('cancelled') is True:
                raise LocalDataError('conflict','这份草稿已在本机取消，请回到原对话。')
            receipt=_receipt(local.store,identity,identifier,marker)
            if receipt is not None:
                if (confirm and receipt['status'] in {'cancelled','discarded'}
                        or not confirm and receipt['status']!='cancelled'):
                    raise LocalDataError('conflict','这份草稿已处理，请查看原操作结果。')
                _remember_decision(local,identity,identifier)
                return {'ok':True,'result':receipt,'_local149':local.status()}
            if not confirm and marker.get('linked_submitted') is True:
                raise LocalDataError('conflict','餐食修改已提交，请先重试确认以核对结果。')
        if not confirm:
            reply = _cloud_cancel(local,identity,online,message,identifier,marker)
            if reply.get('ok'):
                _remember_decision(local,identity,identifier)
                reply['_local149'] = local.status()
            return reply
        draft,error=_verified_draft(online,message,identifier)
        local._guard(identity)
        if error:
            return error
        marker={**(marker or {}),'message':message,'category':draft['category'],'cancelled':not confirm}
        if confirm:
            business_action,payload,target=_business_write(draft)
            if business_action=='expansion.meals.update':
                result=_confirm_linked(local,identity,online,identifier,key,marker,payload,target)
                if result.get('ok') or marker.get('linked_submitted') is True:
                    _remember_decision(local,identity,identifier)
                return result
        # Bind account and message before journaling. A crash between the two
        # store writes leaves a retryable draft, and never a second record.
        local.store.put(identity,'meta',key,marker)
        if confirm:
            local.write_record_assistant(identity,business_action,payload,identifier,target,draft)
        local._guard(identity)
        receipt=_receipt(local.store,identity,identifier,marker)
        if receipt is None:
            raise LocalDataError('local_protocol','暂未核对到保存结果，请重试原卡片。')
        _remember_decision(local,identity,identifier)
        return {'ok':True,'result':receipt,'_local149':local.status()}


def annotate_history(local,identity,response):
    if response.get('ok') is not True or type(response.get('result')) is not dict:
        return response
    page=deepcopy(response['result'])
    if type(page.get('messages')) is not list:
        return response
    for row in page['messages']:
        if type(row) is not dict or row.get('role')!='assistant':
            continue
        receipts={r['id']:r for r in row.get('record_receipts',[]) if isinstance(r,dict)
                  and r.get('status')=='cancelled' and isinstance(r.get('id'),str)}
        for draft in row.get('record_drafts',[]):
            if type(draft) is not dict or 'operation' not in draft:
                continue
            marker=local.store.get(identity,'meta',_scope_marker(draft['id']))
            if marker is None or marker['message']!=row.get('id'):
                continue
            receipt=_receipt(local.store,identity,draft['id'],marker)
            if receipt is not None and draft['id'] not in receipts:
                receipts[draft['id']]=receipt
        if receipts:
            row['record_receipts']=list(receipts.values())
    local._guard(identity)
    return {**response,'result':page}
