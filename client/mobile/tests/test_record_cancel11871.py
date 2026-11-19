"""Contract tests for Android's cloud-backed record draft cancellation."""
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import sys

import pytest

source = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(source / "android/app/src/main/python"), str(source / "src")]
from mobile_ai139 import metadata_view
from mobile_local149 import LocalDataError
from mobile_providers import MobileMemberAPI
from mobile_record_assistant import annotate_history, decide, list_pending_tasks

D = str(UUID(int=21))
OTHER = str(UUID(int=22))
STAMP = '2026-11-18T08:00:00+08:00'


class Store:
    def __init__(self):
        self.data = {}

    def get(self, owner, kind, key):
        return deepcopy(self.data.get((owner.actor_id,kind,key)))

    def put(self, owner, kind, key, item):
        self.data[owner.actor_id,kind,key] = deepcopy(item)

    def operation(self, owner, key):
        return None


class Local:
    def __init__(self):
        self.actor = 7
        self.store = Store()
        self.record_assistant_lock = threading.Lock()

    def _guard(self, owner):
        if owner.actor_id != self.actor:
            raise LocalDataError('account_changed')

    def status(self):
        return {}


def choose(local, online, confirm=False, message=10):
    op = D if confirm else str(uuid4())
    return decide(local,SimpleNamespace(actor_id=7),online,'chat.records.' + ('confirm' if confirm else 'cancel'),
                  {'_fawnsee_actor_id':7,'request_id':op,'draft_id':D,'assistant_message_id':message},op)


def test_cancel_requires_exact_cloud_ack_and_retries_lost_reply():
    local = Local()
    calls = []
    def api(action, body, operation):
        assert action == 'chat.records.cancel' and operation is None
        assert body == {'assistant_message_id':10,'draft_id':D}
        calls.append(body)
        if len(calls)==1:
            return {'ok':False,'error':{'code':'network'}}
        if len(calls)==2:
            return {'ok':True,'result':{'id':OTHER,'status':'cancelled'}}
        return {'ok':True,'result':{'id':D,'status':'cancelled'}}
    assert choose(local,api)['ok'] is False
    with pytest.raises(LocalDataError):
        choose(local,api)
    assert not local.store.data
    assert choose(local,api)['result'] == {'id':D,'status':'cancelled'}
    assert choose(local,api)['result']['status']=='cancelled' and len(calls)==3
    with pytest.raises(LocalDataError):
        choose(local,api,confirm=True)


def test_no_write_if_owner_changes_or_backend_rejects():
    for code in ['not_found','conflict']:
        local=Local()
        answer=choose(local,lambda *args:{'ok':False,'error':{'code':code}})
        assert answer['error']['code']==code and not local.store.data
    local=Local()
    def switch(*args):
        local.actor=8
        return {'ok':True,'result':{'id':D,'status':'cancelled'}}
    with pytest.raises(LocalDataError):
        choose(local,switch)
    assert not local.store.data


def test_legacy_cancellation_checks_message_owner_and_all_pages_before_reloading_page_one():
    local=Local(); owner=SimpleNamespace(actor_id=7)
    all_drafts=[str(UUID(int=500+i)) for i in range(85)]
    def marker(i, actor, **fields):
        local.store.put(SimpleNamespace(actor_id=actor),'meta','record-assistant:'+all_drafts[i],fields)
    marker(80,7,message=81,cancelled=True)
    marker(48,7,message=49,cancelled=False)
    marker(42,8,message=43,cancelled=True)
    marker(47,7,message=999,cancelled=True)
    local.store.put(owner,'meta','record-assistant:recent-decisions-v1',[all_drafts[20],all_drafts[48]])
    cancelled = []
    def api(action, body, operation):
        assert operation is None
        if action=='chat.records.cancel':
            assert body=={'assistant_message_id':81,'draft_id':all_drafts[80]}
            cancelled.append(body['draft_id'])
            return {'ok':True,'result':{'id':all_drafts[80],'status':'cancelled'}}
        assert action=='chat.record_tasks.list' and body['conversation_id']==91 and '_fawnsee_actor_id' not in body
        assert 1 <= body['limit'] <= 40
        rows=[{'assistant_message_id':i+1,'draft_id':item} for i,item in enumerate(all_drafts) if item not in cancelled]
        pos=int(body.get('cursor',0));end=pos+body['limit']
        return {'ok':True,'result':{'total':len(rows),'tasks':rows[pos:end],'next_cursor':str(end) if end<len(rows) else None}}
    payload={'conversation_id':91,'limit':1,'_fawnsee_actor_id':7}
    packet=list_pending_tasks(local,owner,api,payload)['result']
    assert packet['total']==84 and len(packet['tasks'])==1 and cancelled==[all_drafts[80]]
    assert list_pending_tasks(local,owner,api,payload)['result']==packet and cancelled==[all_drafts[80]]


def test_cloud_receipt_annotates_other_device_and_blocks_stale_confirm():
    draft={'id':D,'kind':'record_draft','operation':'create','category':'water','occurred_at':STAMP,
           'content':'喝水','details':{'amount_ml':250},'target':None,'blocked':False,'missing_optional':[],'missing_required':[]}
    data={'assistant_message_id':10,'record_drafts_ready':True,'record_drafts':[draft],
          'record_receipts':[{'id':OTHER,'status':'cancelled'},{'id':D,'status':'saved'}, {'id':D,'status':'cancelled'}]}
    got=metadata_view(data,expected_message=10)
    assert got['record_receipts']==[{'id':D,'status':'cancelled'}]
    local=Local();owner=SimpleNamespace(actor_id=7)
    row=annotate_history(local,owner,{'ok':True,'result':{'messages':[{'id':10,'role':'assistant',**got}]}})
    assert row['result']['messages'][0]['record_receipts']==got['record_receipts']
    def api(action, *args):
        assert action=='chat.tools.proposals'
        return {'ok':True,'result':{'assistant_message_id':10,**got}}
    with pytest.raises(LocalDataError):
        choose(local,api,confirm=True)
    assert not local.store.data


def test_provider_posts_authenticated_ids_without_body():
    calls=[]
    provider=object.__new__(MobileMemberAPI)
    provider._client=SimpleNamespace(request=lambda *a,**kw:calls.append((a,kw)) or {'id':D,'status':'cancelled'})
    assert provider.mobile_cancel_record_draft({'assistant_message_id':10,'draft_id':D})['status']=='cancelled'
    assert calls==[(('POST',f'/v1/ai/messages/10/record-drafts/{D}/cancel'),{})]


def test_member_bridge_permits_only_scoped_cloud_cancel_entry():
    from mobile_providers import MobileMemberBridge
    calls=[]
    client=SimpleNamespace(is_authenticated=True,request=lambda *args,**kw:calls.append((args,kw)) or {'id':D,'status':'cancelled'})
    bridge=MobileMemberBridge(base_url='https://test.invalid', client=client)
    bridge._api._user={'id':7,'role_code':'member'}
    body={'assistant_message_id':10,'draft_id':D,'_fawnsee_actor_id':7}
    result=bridge.request('chat.records.cancel',body)
    assert result.get('ok') and result['data']=={'id':D,'status':'cancelled'},result
    assert not bridge.request('chat.records.cancel',{**body,'_fawnsee_actor_id':8})['ok']
    assert calls==[(('POST',f'/v1/ai/messages/10/record-drafts/{D}/cancel'),{})]
