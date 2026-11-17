"""Authenticated Flash-only AI, bounded structured extraction and real streaming."""
import json
from datetime import date, time
from uuid import UUID

from ..config import DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_SUPPORTED_MODELS
from ..services.cloud_client import CloudAPIError
from ..services.record_photos11839 import photo_ids
from .base import ChatProvider, ProviderCapabilities, ProviderError
from .deepseek_cloud import DeepSeekCloudProvider, _completion_request_body

RECORD_PHOTO_ATTACHMENT = 'try1.2-photo-v1'
RECORD_DIALOGUE = 'try1.2-dialogue-v1'
RECORD_DIALOGUE_TIME_HINTS = 'try1.2-dialogue-v2'
RECORD_DIALOGUE_PHOTOS = 'try1.2-dialogue-v3'
RECORD_TASKS = 'try1.2-tasks-v1'
RECORD_DIALOGUE_TEAMS = (RECORD_DIALOGUE, RECORD_DIALOGUE_TIME_HINTS, RECORD_DIALOGUE_PHOTOS)
RECORD_DIALOGUE_MAX_DECISIONS = 24
RECORD_PHOTO_CATEGORIES = frozenset({'diet', 'environment', 'medical'})


def _canonical_draft_id(value):
    return type(value) is str and len(value) == 36 and str(UUID(value)) == value


class PlatformDeepSeekProvider(ChatProvider):
    """Tools execute only inside the authenticated cloud proxy, never in legacy agents."""
    supports_library_retrieval = True

    def __init__(self, client, *, thinking_enabled=False, reasoning_effort="high",
                 web_search=False, tools_enabled=False, personal_context=False, on_event=None,
                 max_tokens=None, assistant_team=None, decided_record_draft_ids=(),
                 record_tasks=None, record_task_id=None):
        if any(type(v) is not bool for v in (thinking_enabled, web_search, tools_enabled, personal_context)):
            raise ProviderError("AI 开关格式无效。", code="invalid_parameters")
        if not isinstance(reasoning_effort,str) or reasoning_effort not in {"low", "high", "max"}:
            raise ProviderError("请选择有效的思考强度。", code="invalid_parameters")
        if assistant_team not in (None,'try1.1-v1','try1.2-v1',RECORD_PHOTO_ATTACHMENT,*RECORD_DIALOGUE_TEAMS):
            raise ProviderError("助手协作版本无效。", code="invalid_parameters")
        try:
            if (record_tasks not in (None, RECORD_TASKS)
                    or record_task_id is not None and (record_tasks != RECORD_TASKS or not _canonical_draft_id(record_task_id))):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise ProviderError("待完成记录编号无效。", code="invalid_parameters") from None
        try:
            if (type(decided_record_draft_ids) not in (list, tuple)
                    or len(decided_record_draft_ids) > RECORD_DIALOGUE_MAX_DECISIONS
                    or not all(map(_canonical_draft_id, decided_record_draft_ids))
                    or len(set(decided_record_draft_ids)) != len(decided_record_draft_ids)):
                raise ValueError
        except ValueError:
            raise ProviderError("已处理草稿编号无效。", code="invalid_parameters") from None
        self.assistant_team=assistant_team
        self.record_tasks, self.record_task_id = record_tasks, record_task_id
        self.decided_record_draft_ids = tuple(decided_record_draft_ids)
        self.client = client
        self.thinking_enabled, self.reasoning_effort = thinking_enabled, reasoning_effort
        self.web_search, self.tools_enabled = web_search, tools_enabled
        self.personal_context, self.on_event = personal_context, on_event
        self.max_tokens=393216 if max_tokens is None else max_tokens
        self.conversation_id = self.assistant_message_id = None
        self.memory_revision = None
        self.last_sources, self.last_proposals = [], []
        self.last_record_drafts=[]
        self._last_used_model = self._last_response_model = None

    capabilities = property(lambda self: ProviderCapabilities(supports_images=True))
    last_used_model = property(lambda self: self._last_used_model)
    last_response_model = property(lambda self: self._last_response_model)
    resolve_model = DeepSeekCloudProvider.resolve_model

    def _payload(self, model, messages, max_tokens=None, *, json_mode=False):
        chosen = self.resolve_model(model, messages)
        maximum = 393216  # Official output limit, not an application cost cap.
        budget = (32768 if json_mode else self.max_tokens) if max_tokens is None else max_tokens
        if type(budget) is not int or not 1<=budget<=maximum:
            raise ProviderError("输出预算超出模型支持范围。",code="invalid_parameters")
        raw = _completion_request_body(chosen, messages,
            thinking={"type": "disabled" if json_mode or not self.thinking_enabled else "enabled"},
            max_tokens=budget)
        value = json.loads(raw)
        payload = {"model": chosen, "messages": value["messages"], "max_tokens": value["max_tokens"]}
        if json_mode:
            payload.update(thinking={"type": "disabled"}, response_format={"type": "json_object"})
        else:
            payload.update(thinking=value["thinking"], reasoning_effort=self.reasoning_effort,
                web_search=self.web_search, tool_mode="confirm" if self.tools_enabled else "none",
                personal_context=self.personal_context)
            if (self.assistant_team is not None and self.tools_enabled
                    and self.conversation_id is not None and self.assistant_message_id is not None):
                payload['assistant_team']=self.assistant_team
                if self.assistant_team in RECORD_DIALOGUE_TEAMS and self.decided_record_draft_ids:
                    payload['decided_record_draft_ids'] = list(self.decided_record_draft_ids)
                if self.assistant_team == RECORD_DIALOGUE_PHOTOS and self.record_tasks:
                    payload['record_tasks'] = self.record_tasks
                    if self.record_task_id:
                        payload['record_task_id'] = self.record_task_id
            for name in ("conversation_id", "assistant_message_id"):
                identifier = getattr(self, name)
                if identifier is not None:
                    if type(identifier) is not int or identifier <= 0:
                        raise ProviderError("对话编号无效。", code="invalid_parameters")
                    payload[name] = identifier
            if self.personal_context:
                if not isinstance(self.memory_revision,str) or not 1<=len(self.memory_revision)<=128:
                    raise ProviderError("个人资料权限版本不可用，请重新提问。",code="memory_changed")
                payload["memory_revision"]=self.memory_revision
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 32 * 1024 * 1024:
            raise ProviderError("本次附件与历史内容合计过大，请减少附件后重试。", code="request_too_large")
        return payload

    def validate_request(self, model, messages):
        return self._payload(model, messages)["model"]

    @staticmethod
    def _model_matches(actual):
        return isinstance(actual, str) and actual in DEEPSEEK_SUPPORTED_MODELS

    def _complete(self, model, messages, max_tokens=None, *, json_mode=False):
        self._last_used_model = self._last_response_model = None
        payload = self._payload(model, messages, max_tokens, json_mode=json_mode)
        try:
            result = self.client.request("POST", "/v1/ai/chat", json=payload)
        except CloudAPIError as error:
            raise _failure(error) from None
        if (not isinstance(result, dict) or not self._model_matches(result.get("model"))
                or not isinstance(result.get("content"), str) or not result["content"].strip()):
            raise ProviderError("默认 AI 返回了无效内容。", code="invalid_response")
        self._last_used_model = self._last_response_model = DEEPSEEK_DEFAULT_MODEL
        self.last_record_drafts=_record_drafts(result.get('record_drafts'),
                                             expanded_photos=self.assistant_team==RECORD_DIALOGUE_PHOTOS)
        return result["content"]

    def chat(self, model, messages, *, max_tokens=None):
        return self._complete(model, messages, max_tokens)

    def chat_json(self, model, messages, *, max_tokens=None):
        """Actual JSON mode; callers still validate their own field schema."""
        return self._complete(model, messages, max_tokens, json_mode=True)

    def _event(self, event):
        kind = event.get("type")
        if kind is None and event.get("status") == "thinking": kind = "thinking"
        if kind is None and isinstance(event.get("sources"), list): kind = "web_sources"
        if kind is None and isinstance(event.get("proposal"), dict): kind = "proposal"
        if kind is None and isinstance(event.get("activity"), dict):
            activity = event["activity"]
            kind = "tool_start" if activity.get("status") == "working" else "tool_done"
            event = {**activity, "type": kind}
        if kind not in {"thinking", "tool_start", "tool_done", "web_sources", "proposal", "record_drafts"}: return
        safe = {"type": kind}
        if kind == "web_sources":
            safe["sources"] = _sources(event.get("sources"))
            self.last_sources = safe["sources"]
        elif kind == 'record_drafts':
            expanded_photos=self.assistant_team==RECORD_DIALOGUE_PHOTOS
            safe['drafts']=_record_drafts(event.get('drafts'),expanded_photos=expanded_photos)
            self.last_record_drafts=_record_drafts(self.last_record_drafts+safe['drafts'],expanded_photos=expanded_photos)
        elif kind == "proposal":
            safe["proposal"] = _proposal(event.get("proposal"))
            if not safe["proposal"]: return
            self.last_proposals = [p for p in self.last_proposals if p["id"] != safe["proposal"]["id"]] + [safe["proposal"]]
            self.last_proposals = self.last_proposals[-8:]
        elif kind != "thinking":
            for key, maximum in (("tool",80),("label",120),("detail",400),("status",32)):
                if isinstance(event.get(key),str):safe[key]=event[key][:maximum]
        # Never forward arbitrary events, raw reasoning, or provider exceptions.
        if callable(self.on_event):
            try:self.on_event(safe)
            except Exception:pass

    def chat_stream(self, model, messages, *, cancel_event=None, max_tokens=None):
        self._last_used_model = self._last_response_model = None
        self.last_sources, self.last_proposals = [], []
        self.last_record_drafts=[]
        payload = self._payload(model, messages, max_tokens)
        done, text_seen, count = False, False, 0
        try:
            for event in self.client.ai_stream(payload, cancel_event=cancel_event):
                if not isinstance(event, dict):
                    raise ProviderError("默认 AI 流响应格式无效。", code="invalid_response")
                if event.get("error"):
                    raise ProviderError("默认 AI 暂未完成回答，请稍后手动重试。", code="incomplete_response")
                if event.get("done") is True:
                    if not self._model_matches(event.get("model")):
                        raise ProviderError("默认 AI 模型校验失败。", code="invalid_response")
                    if not text_seen:
                        raise ProviderError("默认 AI 返回了空回答。", code="incomplete_response")
                    if 'record_drafts' in event:self._event({'type':'record_drafts','drafts':event['record_drafts']})
                    done = True
                    self._last_used_model = self._last_response_model = DEEPSEEK_DEFAULT_MODEL
                    break
                if isinstance(event.get("delta"), str):
                    delta = event["delta"];count += len(delta)
                    if count > 2000000:
                        raise ProviderError("本次回答超出显示上限。", code="incomplete_response")
                    text_seen = text_seen or bool(delta.strip())
                    yield delta
                else:self._event(event)
        except CloudAPIError as error:
            raise _failure(error) from None
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderError("已停止回答。", code="cancelled")
        if not done:
            raise ProviderError("默认 AI 连接在完成前中断。", code="incomplete_response")

def _sources(value):
    from urllib.parse import urlsplit
    if not isinstance(value,list):return []
    result=[]
    for row in value[:8]:
        if not isinstance(row,dict):continue
        title,url=row.get("title"),row.get("url")
        if not isinstance(title,str) or not isinstance(url,str) or not title.strip() or len(url)>2048:continue
        try:parsed=urlsplit(url)
        except ValueError:continue
        if parsed.scheme not in {"https","http"} or not parsed.hostname or parsed.username or parsed.password:continue
        item={"title":title[:200],"url":url}
        if isinstance(row.get("date"),str):item["date"]=row["date"][:64]
        result.append(item)
    return result

def _proposal(row):
    if not isinstance(row,dict):return None
    if not all(isinstance(row.get(k),str) and row[k] for k in ("id","kind","title","summary","token")):return None
    # The signed cloud receipt stores Unix seconds; the UI uses an ISO date.
    # Normalize the display field only, never the signed execution token.
    expiry=row.get('expires_at')
    if type(expiry) is int:
        from datetime import datetime,timezone
        try:expiry=datetime.fromtimestamp(expiry,timezone.utc).isoformat().replace('+00:00','Z')
        except (ValueError,OverflowError,OSError):return None
        row={**row,'expires_at':expiry}
    if not isinstance(expiry,str) or not expiry:return None
    limits=(("id",100),("kind",80),("title",200),("summary",2400),("expires_at",80),("token",24000))
    if any(len(row[k])>maximum for k,maximum in limits):return None
    status=row.get('status')
    result={**{k:row[k] for k,_ in limits if k!='token'},
            "token":row["token"],"status":status if isinstance(status,str) and status in {"pending","saved","cancelled","expired"} else "pending"}
    fields=[]
    raw_fields=row.get('fields',[])
    if not isinstance(raw_fields,list) or len(raw_fields)>64:return None
    for item in raw_fields:
        if (not isinstance(item,dict) or not isinstance(item.get('label'),str)
                or not isinstance(item.get('value'),str) or len(item['label'])>100 or len(item['value'])>4000):return None
        fields.append({'label':item['label'],'value':item['value']})
    if fields:result['fields']=fields
    if len(json.dumps(result,ensure_ascii=False).encode())>240*1024:return None
    return result

_DRAFT_FIELDS={
    'diet':{'meal','meal_type','food','amount_g','calories_kcal','sugar_g','notes','estimated'},
    'water':{'amount_ml','notes'},
    'activity':{'activity','activity_type','duration_minutes','steps','energy_kcal','notes'},
    'sleep':{'duration_hours','quality','notes'},
    'environment':{'temperature_c','humidity_percent','ventilation_minutes','quality_score','description','notes'},
    'medical':{'event_type','name','description','location'},
    'metric':{'kind','value','unit','context','notes','diastolic','metric_name'},
}
_DRAFT_NUMBERS={'amount_g':(0,20000),'calories_kcal':(0,20000),'sugar_g':(0,20000),
    'amount_ml':(1,10000),'duration_minutes':(0,1440),'steps':(0,200000),'energy_kcal':(0,20000),
    'duration_hours':(0,24),'quality':(1,5),'temperature_c':(-80,80),'humidity_percent':(0,100),
    'ventilation_minutes':(0,1440),'quality_score':(0,100),'value':(0.000001,1000000000),
    'diastolic':(0.000001,1000000000)}

_DRAFT_KEYS={'id','kind','category','occurred_at','content','details'}
_V12_DRAFT_KEYS={'operation','target','missing_required','missing_optional','blocked'}
_V12_DRAFT_EXTRAS={'photo_file_ids','supersedes_draft_id','superseded','record_date','record_time','record_time_hint'}
_RECORD_TIME_HINTS={'dawn','morning','forenoon','noon','afternoon','dusk','evening','night'}


def _draft_timestamp(stamp):
    from datetime import datetime
    import re
    if stamp is None:
        return None
    if not isinstance(stamp,str) or len(stamp)>80 or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})',stamp):
        raise ValueError
    datetime.fromisoformat(stamp.replace('Z','+00:00'))
    return stamp


def _draft_details(details,category):
    import math
    if type(details) is not dict or set(details)-_DRAFT_FIELDS[category]:
        raise ValueError
    for key,item in details.items():
        if item is None:
            continue
        if key in _DRAFT_NUMBERS:
            low,high=_DRAFT_NUMBERS[key]
            if category=='metric' and key=='value':
                low=-1000000
            if type(item) not in (int,float) or not math.isfinite(item) or not low<=item<=high:
                raise ValueError
            if key in {'calories_kcal','duration_minutes','steps','quality'} and not float(item).is_integer():
                raise ValueError
        elif key=='estimated':
            if type(item) is not bool:
                raise ValueError
        elif not isinstance(item,str) or len(item)>2000:
            raise ValueError
    return dict(details)


def _draft_missing(value):
    import re
    if type(value) is not list or len(value)>16:
        raise ValueError
    result=[];seen=set()
    for item in value:
        if (type(item) is not dict or set(item)!={'field','label'}
                or not isinstance(item['field'],str) or not re.fullmatch(r'[a-z][a-z_0-9.]{0,39}',item['field'])
                or item['field'] in seen or not isinstance(item['label'],str)
                or not item['label'].strip() or len(item['label'])>80):
            raise ValueError
        result.append({'field':item['field'],'label':item['label'].strip()})
        seen.add(item['field'])
    return result


def _linked_meal(value,identifier):
    import math
    if (type(value) is not dict or set(value)!={'id','version','changes'}
            or type(value['id']) is not int or value['id']!=identifier
            or not isinstance(value['version'],str) or not 1<=len(value['version'])<=256):
        raise ValueError
    changes=value['changes']
    if type(changes) is not dict or not changes or set(changes)-{'meal_type','occurred_at','notes','items'}:
        raise ValueError
    if 'meal_type' in changes and (type(changes['meal_type']) is not str
            or changes['meal_type'] not in {'breakfast','lunch','dinner','snack'}):
        raise ValueError
    if 'occurred_at' in changes and _draft_timestamp(changes['occurred_at']) is None:
        raise ValueError
    if 'notes' in changes and (type(changes['notes']) is not str or len(changes['notes'])>4000):
        raise ValueError
    if 'items' in changes:
        items=changes['items']
        if type(items) is not list or not 1<=len(items)<=20:
            raise ValueError
        for item in items:
            if (type(item) is not dict or set(item)!={'food_id','name','amount_g'}
                    or not isinstance(item['name'],str) or not item['name'].strip() or len(item['name'])>200):
                raise ValueError
            food_id,amount=item['food_id'],item['amount_g']
            if food_id is not None and (type(food_id) is not str or not 1<=len(food_id)<=100):
                raise ValueError
            if amount is not None and (type(amount) not in (int,float) or not math.isfinite(amount) or not 0.1<=amount<=20000):
                raise ValueError
    return {'id':identifier,'version':value['version'],'changes':changes}


def _draft_target(value,category):
    if value is None:
        return None
    if (type(value) is not dict or not {'id','version'}<=set(value)
            or set(value)-{'id','version','linked_meal_id','before','linked_meal'}
            or type(value['id']) is not int or not 0<value['id']<=9007199254740991
            or not isinstance(value['version'],str) or not 1<=len(value['version'])<=256):
        raise ValueError
    result={'id':value['id'],'version':value['version']}
    linked_meal=value.get('linked_meal_id')
    if linked_meal is not None:
        if type(linked_meal) is not int or not 0<linked_meal<=9007199254740991:
            raise ValueError
        result['linked_meal_id']=linked_meal
    if value.get('linked_meal') is not None:
        if category!='diet' or linked_meal is None:
            raise ValueError
        result['linked_meal']=_linked_meal(value['linked_meal'],linked_meal)
    before=value.get('before')
    if before is not None:
        if (type(before) is not dict or set(before)!={'occurred_at','content','details'}
                or not isinstance(before['content'],str) or len(before['content'])>2000):
            raise ValueError
        result['before']={'occurred_at':_draft_timestamp(before['occurred_at']),
            'content':before['content'],'details':_draft_details(before['details'],category)}
    return result


def _record_drafts(value, *, expanded_photos=True):
    """Check record suggestions and any owned-file references bound by the cloud."""
    if not isinstance(value,list):return []
    result=[];seen=set()
    for row in value[:8]:
        if (type(row) is not dict or not _DRAFT_KEYS<=set(row)
                or row.get('kind')!='record_draft' or not isinstance(row.get('category'),str) or row['category'] not in _DRAFT_FIELDS
                or not isinstance(row.get('id'),str) or not isinstance(row.get('content'),str)
                or len(row['content'])>2000):continue
        try:
            identifier=str(UUID(row['id']))
            if identifier!=row['id'].lower() or identifier in seen:continue
            stamp=_draft_timestamp(row['occurred_at'])
            category=row['category']
            clean={key:row[key] for key in _DRAFT_KEYS}
            clean.update(id=identifier,details=_draft_details(row['details'],category))
            if 'operation' in row:
                expected=_DRAFT_KEYS|_V12_DRAFT_KEYS
                if not expected<=set(row)<=expected|_V12_DRAFT_EXTRAS:continue
                operation,blocked=row['operation'],row['blocked']
                if type(operation) is not str or operation not in {'create','update','supplement'} or type(blocked) is not bool:continue
                required=_draft_missing(row['missing_required'])
                optional=_draft_missing(row['missing_optional'])
                if {item['field'] for item in required}&{item['field'] for item in optional}:continue
                target=_draft_target(row['target'],category)
                if operation=='create' and target is not None:continue
                if not blocked and not required and (not stamp or not row['content'].strip()
                        or (operation!='create' and target is None)):
                    continue
                clean.update(operation=operation,target=target,missing_required=required,missing_optional=optional,blocked=blocked)
                clean.update(_draft_dialogue_metadata(row, identifier, stamp, blocked))
                if 'photo_file_ids' in row:
                    references=photo_ids(row['photo_file_ids'])
                    if not references or category not in RECORD_PHOTO_CATEGORIES:
                        continue
                    if not expanded_photos and (category!='diet' or operation!='create' or len(references)>2):
                        continue
                    clean['photo_file_ids']=references
            elif set(row)!=_DRAFT_KEYS or not row['content'].strip():
                continue
        except (ValueError,TypeError,OverflowError):continue
        seen.add(identifier);result.append(clean)
    return result


def _draft_dialogue_metadata(row, identifier, stamp, blocked):
    result = {}
    if 'supersedes_draft_id' in row:
        parent = row['supersedes_draft_id']
        if not _canonical_draft_id(parent) or parent == identifier:
            raise ValueError
        result['supersedes_draft_id'] = parent
    if 'superseded' in row:
        if row['superseded'] is not True or not blocked:
            raise ValueError
        result['superseded'] = True
    if 'record_date' in row:
        day = row['record_date']
        if stamp is not None or type(day) is not str or len(day) != 10 or date.fromisoformat(day).isoformat() != day:
            raise ValueError
        result['record_date'] = day
    if 'record_time' in row:
        clock = row['record_time']
        if stamp is not None or type(clock) is not str or len(clock) != 5 or time.fromisoformat(clock).isoformat(timespec='minutes') != clock:
            raise ValueError
        result['record_time'] = clock
    if 'record_time_hint' in row:
        hint = row['record_time_hint']
        if stamp is not None or 'record_time' in row or type(hint) is not str or hint not in _RECORD_TIME_HINTS:
            raise ValueError
        result['record_time_hint'] = hint
    return result


def _failure(error):
    if error.code == "limited":
        return ProviderError("AI 请求过于频繁，请稍后手动重试。", code="rate_limited")
    if error.code == "unavailable":
        return ProviderError("默认 AI 暂不可用，请稍后再试。", code="service_unavailable")
    return ProviderError(str(error), code=error.code)
