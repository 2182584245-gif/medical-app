"""Authenticated Flash-only AI, bounded structured extraction and real streaming."""
import json
from ..config import DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_SUPPORTED_MODELS
from ..services.cloud_client import CloudAPIError
from .base import ChatProvider, ProviderCapabilities, ProviderError
from .deepseek_cloud import DeepSeekCloudProvider, _completion_request_body

class PlatformDeepSeekProvider(ChatProvider):
    """Tools execute only inside the authenticated cloud proxy, never in legacy agents."""
    supports_library_retrieval = True

    def __init__(self, client, *, thinking_enabled=False, reasoning_effort="high",
                 web_search=False, tools_enabled=False, personal_context=False, on_event=None,
                 max_tokens=None):
        if any(type(v) is not bool for v in (thinking_enabled, web_search, tools_enabled, personal_context)):
            raise ProviderError("AI 开关格式无效。", code="invalid_parameters")
        if not isinstance(reasoning_effort,str) or reasoning_effort not in {"low", "high", "max"}:
            raise ProviderError("请选择有效的思考强度。", code="invalid_parameters")
        self.client = client
        self.thinking_enabled, self.reasoning_effort = thinking_enabled, reasoning_effort
        self.web_search, self.tools_enabled = web_search, tools_enabled
        self.personal_context, self.on_event = personal_context, on_event
        self.max_tokens=393216 if max_tokens is None else max_tokens
        self.conversation_id = self.assistant_message_id = None
        self.memory_revision = None
        self.last_sources, self.last_proposals = [], []
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
        if kind not in {"thinking", "tool_start", "tool_done", "web_sources", "proposal"}: return
        safe = {"type": kind}
        if kind == "web_sources":
            safe["sources"] = _sources(event.get("sources"))
            self.last_sources = safe["sources"]
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

def _failure(error):
    if error.code == "limited":
        return ProviderError("AI 请求过于频繁，请稍后手动重试。", code="rate_limited")
    if error.code == "unavailable":
        return ProviderError("默认 AI 暂不可用，请稍后再试。", code="service_unavailable")
    return ProviderError(str(error), code=error.code)
