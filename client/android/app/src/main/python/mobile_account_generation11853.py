"""Authenticated online data-generation checks before member payload access.

The SQLite tombstone is written before cleanup. Offline/failed verification
never falls back to cached private files. An unchanged account keeps its own
unsent mutations when another account is selected.
"""
from __future__ import annotations
import re,threading
import httpx
from mobile_local149 import Identity,LocalDataError

STATE_PATH='/v1/member/local-sync/account-state'


class AccountGeneration11853:
    def __init__(self,app):
        self.app=app;self.lock=threading.RLock();self.required=True;self.state=None

    def suspend(self):
        self.required=True;self.state=None

    def ensure(self):
        app=self.app
        attempted_epoch=app._auth_epoch
        with self.lock:
            with app._auth_lock:
                if attempted_epoch!=app._auth_epoch:
                    raise LocalDataError('account_changed','账号状态已改变，请重新读取当前页面。')
                user=app.bridge._api._user or {};actor=user.get('id');epoch=app._auth_epoch
                if type(actor)is not int or actor<=0 or app.session is None or app.session.value is None:
                    raise LocalDataError('account_verification_required','请联网登录后核验当前账号。')
                raw=app.session.identity()
                if raw.get('authorized')is not True or raw.get('actor_id')!=actor:
                    raise LocalDataError('account_verification_required','登录授权尚待核验，请联网重新登录。')
                client=app.bridge._api._client
                if client is None:raise LocalDataError('account_verification_required','账号资料尚未核验，请联网后重试。')
                snapshot=client._request_snapshot(True)
            try:
                state=client.mobile_credential_request('GET',STATE_PATH,snapshot,
                    timeout=httpx.Timeout(connect=2.0,read=3.0,write=2.0,pool=2.0))
                if (type(state)is not dict or set(state)!={'actor_id','server_instance_id','data_generation','has_reset'}
                    or type(state['actor_id'])is not int or state['actor_id']!=actor or state['server_instance_id']!=raw.get('server_instance_id')
                    or type(state['has_reset'])is not bool or type(state['data_generation'])is not str
                    or not re.fullmatch(r'[a-f0-9]{64}',state['data_generation'])):
                    raise ValueError('Invalid authenticated account generation')
            except Exception as error:
                self.state=None;self.required=True
                revoke_epoch=None
                with app._auth_lock:
                    if epoch==app._auth_epoch and(app.bridge._api._user or{}).get('id')==actor:
                        app._auth_epoch+=1;app.bridge._generation+=1
                        revoke_epoch=app._auth_epoch
                        if app._native_hooks is not None:
                            invalidate=getattr(app._native_hooks,'invalidateGuestScope',None)
                            if callable(invalidate):invalidate()
                            app._native_hooks.deactivate()
                if revoke_epoch is not None and getattr(error,'code',None)in {'authentication','not_authenticated'}:
                    app._revoke_online(actor,revoke_epoch)
                raise LocalDataError('account_verification_required','账号资料尚未核验，请连接网络后重试；旧资料暂不使用。')from None
            with app._auth_lock:
                if epoch!=app._auth_epoch or(app.bridge._api._user or{}).get('id')!=actor or app.bridge._api._client is not client:
                    raise LocalDataError('account_changed','账号已变更，本次资料核验已取消。')
                client._request_current(snapshot)
                current=Identity.checked(raw)
                prior=app.local.store.account_generation(current)
                changed=(prior is None and state['has_reset']or prior is not None and
                         (prior['status']!='active'or prior['generation']!=state['data_generation']))
                if changed:
                    self.required=True
                    app.local.store.invalidate_account(current)
                    app._auth_epoch+=1;app.bridge._generation+=1
                    if app._native_hooks is not None:
                        invalidate=getattr(app._native_hooks,'invalidateGuestScope',None)
                        if callable(invalidate):invalidate()
                        app._native_hooks.deactivate()
                    app._account_reset._purge({'actor':actor,'endpoint':raw['endpoint']})
                    # The cloud session remains valid, but old ciphertext keys
                    # and automatic-login files cannot revive cleared payloads.
                    automatic=app.session.persistent
                    app.session._forget_actor_disk(actor)
                    app._vault.clearActorKey(actor)
                    client._data_generation=state['data_generation']
                    user=app._account_reset.minimal_user(user)
                    client._user=dict(user);app.bridge._api._user=dict(user)
                    app.session.remember(client,user,auto_login=automatic,preserve_other_accounts=True)
                else:
                    client._data_generation=state['data_generation']
                    app.session.bind_data_generation(state['data_generation'])
                current=Identity.checked(app.session.identity())
                app.local.store.bind_generation(current,has_reset=state['has_reset'])
                self.state=dict(state);self.required=False
                return {**state,'generation_changed':bool(changed)}
