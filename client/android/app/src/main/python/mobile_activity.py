"""Observed public chat activity with request-scoped native progress callbacks."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import time

from ollama_chat_app.services.chat_activity import LABELS, validate_activity, COLLABORATION_ROLES, collaboration_detail
from ollama_chat_app.services.cloud_library import CloudKnowledgeLibrary
from ollama_chat_app.services.knowledge_library import LibraryError

_PROGRESS=ContextVar('mobile_chat_activity_progress',default=None)
_ORDER=('welcome_cleanup','personal_records','knowledge_cards','language_cards','web_search','controlled_tools','answer')
_DOMAINS={
 'profile':'个人资料','life_records':'生活记录','reminders':'提醒','goals_habits':'目标与习惯',
 'services_appointments':'服务与预约','reading':'阅读记录','shopping':'购物记录',
 'forum':'社区交流','operations':'应用操作','preferences':'用户偏好','ai_records':'AI 对话记录',
}


@contextmanager
def progress_scope(progress):
    token=_PROGRESS.set(progress)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')


def _count(value):
    if type(value) is not int or not 0<=value<=999:
        raise ValueError('Invalid observed count')
    return value


class ChatActivityReport:
    def __init__(self, *, clock=None, monotonic=None):
        self._monotonic=monotonic or time.monotonic
        self._start=(clock or (lambda:datetime.now(timezone.utc)))()
        if self._start.tzinfo is None:
            raise ValueError('Activity clock requires timezone')
        self._tick=self._monotonic()
        self._steps={}
        self._team={}
        self._library={kind:{'searched':False,'hits':0,'opened':0} for kind in ('knowledge','language')}
        self._phase='references'
        self._final=None
        self._step('web_search','skipped','本次未调用网页搜索。',publish=False)

    def stream_event(self, value):
        """Accept only application-observed outcomes, never model reasoning."""
        kind=value.get('type')
        tool=value.get('tool','')
        web=tool in {'search_web','fetch_web','fetch_webpage','web_search','web_fetch','search','fetch','search_public_web','fetch_public_web'}
        if kind=='thinking':
            self._phase='thinking';self.emit()
        elif kind=='tool_start':
            self._phase='web_search' if web else 'tools'
            if tool in COLLABORATION_ROLES:
                if tool not in self._team and len(self._team)<2:self._team[tool]='working'
                self._team_step()
            else:self.emit()
        elif kind=='web_sources':
            self._phase='answer'
            self._step('web_search','done','搜索了公开网页。')
        elif kind=='tool_done':
            self._phase='answer'
            failed=value.get('status') in {'error','failed'}
            if web:
                self._step('web_search','failed' if failed else 'done','网页搜索暂不可用。' if failed else '搜索了公开网页。')
            else:
                if tool in COLLABORATION_ROLES:
                    if tool in self._team or len(self._team)<2:self._team[tool]='failed' if failed else 'done'
                    self._team_step()
                elif self._team:self._team_step()
                else:
                    waiting=self._steps.get('controlled_tools',{}).get('detail')=='已生成待确认操作，尚未保存。'
                    detail='应用工具未完成。' if failed else ('已生成待确认操作，尚未保存。' if waiting else '查询了本人授权的资料。')
                    self._step('controlled_tools','failed' if failed else 'done',detail)
        elif kind=='proposal':
            self._step('controlled_tools','done','已生成待确认操作，尚未保存。')
        elif kind=='record_drafts':
            if self._team:self._team_step()
            else:self._step('controlled_tools','done','已整理记录草稿，需核对后自行保存。')

    def _team_step(self, *, final=False, error=False):
        if final:
            self._team={tool:('failed' if status=='working' else status) for tool,status in self._team.items()}
        rows=[{'tool':tool,'status':status} for tool,status in self._team.items()]
        previous=self._steps.get('controlled_tools',{}).get('status')
        status='failed' if any(row['status']=='failed' for row in rows) else 'done' if rows else (previous if previous in {'done','failed'} else 'skipped')
        self._step('controlled_tools',status,collaboration_detail(rows,incomplete=error and not rows),publish=not final)

    def _now(self):
        return self._start+timedelta(seconds=max(0,self._monotonic()-self._tick))

    def _step(self, kind, status, detail=None, *, publish=True):
        row={'kind':kind,'status':status,'label':LABELS[kind][status],'at':_stamp(self._now())}
        if detail:
            row['detail']=detail
        self._steps[kind]=row
        self._final=None
        if publish:
            self.emit()

    def snapshot(self):
        if self._final is not None:
            return deepcopy(self._final)
        return {'version':1,'status':'working','phase':self._phase,
                'started_at':_stamp(self._start),'finished_at':None,
                'steps':[deepcopy(self._steps[kind]) for kind in _ORDER if kind in self._steps]}

    def emit(self):
        progress=_PROGRESS.get()
        callback=getattr(progress,'onActivity',None)
        if callable(callback):
            try:
                callback(json.dumps(self.snapshot(),ensure_ascii=False,separators=(',',':')))
            except Exception:
                # A detached WebView must not discard a complete cloud answer.
                # Final persistence/return still carries the validated receipt.
                pass

    def personal(self, snapshot, enabled=True):
        if not enabled:
            self._step('personal_records','skipped','已关闭个人记录参考。')
            return
        if not isinstance(snapshot,dict) or snapshot.get('memory_enabled') is not True:
            coverage=snapshot.get('coverage') if isinstance(snapshot,dict) else None
            state=coverage.get('state') if isinstance(coverage,dict) else None
            if state=='disabled':
                self._step('personal_records','skipped','已关闭个人记录参考。')
            else:
                self._step('personal_records','failed','个人记录暂不可用。')
            return
        coverage=snapshot.get('capability_coverage')
        coverage=coverage if isinstance(coverage,dict) else {}
        domains=[kind for kind in _DOMAINS if isinstance(coverage.get(kind),dict) and coverage[kind].get('enabled_for_ai') is True]
        if not domains:
            observed=set()
            if snapshot.get('profile'):
                observed.add('profile')
            for field in ('raw_records','confirmed_facts','inferred_preferences','ai_history','daily_summaries'):
                for row in snapshot.get(field,[]) if isinstance(snapshot.get(field),list) else []:
                    if isinstance(row,dict) and isinstance(row.get('domain'),str) and row['domain'] in _DOMAINS:
                        observed.add(row['domain'])
            domains=[kind for kind in _DOMAINS if kind in observed]
        detail='查询范围：'+'、'.join(_DOMAINS[kind] for kind in domains)+'。' if domains else '本次没有可参考的个人记录。'
        self._step('personal_records','done',detail)

    def library(self, kind, status='done', *, hits=None, opened=None):
        if kind not in self._library or status not in {'done','skipped','failed'}:
            raise ValueError('Invalid observed library step')
        state=self._library[kind]
        if hits is not None:
            state['searched']=True;state['hits']=_count(hits)
        if opened is not None:
            state['opened']=_count(opened)
        if status=='skipped':detail='已关闭资料卡参考。'
        elif status=='failed':detail='资料卡暂不可用。'
        elif state['opened']:
            detail='已打开'+str(state['opened'])+'份'+('知识' if kind=='knowledge' else '语言')+'资料卡。'
        elif state['searched'] and state['hits']==0:
            detail='未找到相关资料卡。'
        else:
            detail='检索了资料目录。'
        self._step(kind+'_cards',status,detail)

    def welcome_cleanup(self, receipt, *, applying=False):
        if not isinstance(receipt,dict) or receipt.get('checked')is not True:
            raise ValueError('Welcome cleanup receipt is missing')
        count=receipt.get('folded_count')
        if type(count)is not int or not 0<=count<=999999:
            raise ValueError('Invalid observed welcome count')
        self._phase='welcome_cleanup' if applying and count else 'references'
        if count:
            self._step('welcome_cleanup','working'if applying else'done',
                       None if applying else'已折叠'+str(count)+'条未回应的欢迎语。')
        elif not applying:
            self._step('welcome_cleanup','skipped','本次没有需要折叠的欢迎语。')

    def answering(self):
        self._phase='answer'
        self.emit()

    start_answer=answering

    def finish(self, error=False):
        # Construct first, persist with the answer atomically, then emit/return.
        # A "saved" success must not be announced before its transaction commits.
        desired='error' if error else 'complete'
        if self._final is not None and self._final['status'] in {desired,'error'}:
            return deepcopy(self._final)
        self._phase='answer'
        if self._steps.get('welcome_cleanup',{}).get('status')=='working':
            self._step('welcome_cleanup','failed','欢迎消息整理未完成。',publish=False)
        self._team_step(final=True,error=error)
        self._step('answer','failed' if error else 'done',publish=False)
        value={'version':1,'status':'error' if error else 'complete',
               'started_at':_stamp(self._start),'finished_at':_stamp(self._now()),
               'steps':[deepcopy(self._steps[kind]) for kind in _ORDER if kind in self._steps]}
        self._final=validate_activity(value)
        return deepcopy(self._final)


class ObservedCloudKnowledgeLibrary(CloudKnowledgeLibrary):
    """Report only successful real requests; references are never guessed."""
    def __init__(self, client, report):
        super().__init__(client)
        self.report=report
        self._opened={'knowledge':set(),'language':set()}

    def search(self,kind,query,*,limit=5):
        try:
            rows=super().search(kind,query,limit=limit)
        except LibraryError:
            if kind in self._opened:
                self.report.library(kind,'failed')
            raise
        self.report.library(kind,'done',hits=len(rows))
        return rows

    def _open_observed(self,kind,document_id,section_id):
        try:
            result=super()._open_card(kind,document_id,section_id)
        except LibraryError:
            self.report.library(kind,'failed')
            raise
        self._opened[kind].add(document_id)
        self.report.library(kind,'done',opened=len(self._opened[kind]))
        return result

    def open_knowledge_card(self,document_id,section_id=None):
        return self._open_observed('knowledge',document_id,section_id)

    def open_language_card(self,document_id,section_id=None):
        return self._open_observed('language',document_id,section_id)

    def open_source(self,document_id,*,anchors=()):
        try:
            result=super().open_source(document_id,anchors=anchors)
        except LibraryError:
            self.report.library('knowledge','failed')
            raise
        self.report.library('knowledge','done')
        return result


__all__=['progress_scope','ChatActivityReport','ObservedCloudKnowledgeLibrary']
