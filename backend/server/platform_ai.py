"""AI generation stays on the user's device; the server validates and stores drafts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from contextvars import ContextVar

from ollama_chat_app.data.database import timestamp_to_db, utc_now
from ollama_chat_app.services.ai_assistant import (
    MEMBER_PROPOSAL_TYPES,
    AiAssistantService,
    AiValidationError,
    _numbered_pending_proposals,
)


class PlatformAIService(AiAssistantService):
    def __init__(self, database, health_service, ticket_secret: str):
        super().__init__(database, health_service)
        self._ticket_secret = bytes.fromhex(ticket_secret)
        self._profile_question=ContextVar('legacy_profile_question_'+str(id(self)),default='个人情况与生活习惯')

    def build_member_context(self,actor_user_id,member_user_id=None,*,context_days=7):
        """Legacy RPC retains its name while using the current privacy gates."""
        with self.database.connect()as connection:
            actor,member=self._authorise_member_context(connection,actor_user_id,member_user_id)
        memory=getattr(self,'profile_memory',None)
        if actor['role_code']=='member' and memory is not None:
            config=memory.settings(member)
            if config['enabled']:
                active=getattr(self.database,'_active',None)
                if active is not None and active.get()is not None:
                    raise AiValidationError('个人资料检索需要独立请求边界，请重新提问。')
                result=memory.core.context(member,self._profile_question.get())
            else:
                result={'member_user_id':member,'generated_at':timestamp_to_db(utc_now()),'profile':{},
                        'confirmed_facts':[],'raw_records':[],'personal_introduction_references':[]}
            after=memory.settings(member)
            if after['revision']!=config['revision']or after['enabled']!=config['enabled']:
                raise AiValidationError('个人资料参考设置已变化，请重新提问。')
            return {**result,'actor_role':'member','context_days':self._context_days(context_days),
                    'memory_revision':config['revision'],'memory_enabled':config['enabled']}
        # An advisor is not the member's vector owner. Preserve the existing
        # authorized advisor service context without leaking the mirrored intro
        # through its old raw-profile path or impersonating a member identity.
        result=super().build_member_context(actor_user_id,member_user_id,context_days=context_days)
        for section in('profile','confirmed_facts','confirmed_fact_sources'):
            if isinstance(result.get(section),dict):
                for field in('dietary_preferences','personal_introduction','phone_default_initialized','personal_introduction_updated_at'):
                    result[section].pop(field,None)
        return result

    @staticmethod
    def _ai_config_revision(config):
        return hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def _validate_legacy_context(self,actor_user_id,context):
        """Recheck sealed/current evidence using only owner-scoped local reads."""
        if type(context)is not dict:
            raise AiValidationError('个人资料参考来源已变化，请重新提问。')
        with self.database.connect()as connection:
            actor,member=self._authorise_member_context(connection,actor_user_id,context['member_user_id'])
        if context.get('actor_role')is not None and context['actor_role']!=actor['role_code']:
            raise AiValidationError('个人资料参考身份已变化，请重新提问。')
        if actor['role_code']!='member' or context.get('memory_revision')is None:
            return
        memory=getattr(self,'profile_memory',None)
        if memory is None:
            raise AiValidationError('个人资料参考设置已变化，请重新提问。')
        current=memory.settings(member)
        if (current['revision']!=context['memory_revision']
                or ('memory_enabled'in context and current['enabled']is not context['memory_enabled'])):
            raise AiValidationError('个人资料参考设置已变化，请重新提问。')
        references=context.get('evidence',[])
        multimodal=context.get('multimodal_references',[])
        if (type(references)is not list or len(references)>128
                or type(multimodal)is not list or len(multimodal)>3
                or (not current['enabled']and (references or multimodal))):
            raise AiValidationError('个人资料参考来源已变化，请重新提问。')
        ordinary=[];assistant=[]
        for item in references:
            if (type(item)is not dict or type(item.get('ref'))is not str
                    or re.fullmatch(r'[a-f0-9]{64}',str(item.get('source_hash','')))is None):
                raise AiValidationError('个人资料参考来源已变化，请重新提问。')
            table,_,identifier=item['ref'].partition(':')
            if table=='messages'and item.get('kind')=='model_inferred':
                if not identifier.isdigit()or not 0<int(identifier)<2**53:
                    raise AiValidationError('个人资料参考来源已变化，请重新提问。')
                assistant.append((item,int(identifier)))
            else:ordinary.append(item)
        if ordinary:
            resolver=getattr(memory.core,'resolve_references',None)
            if not callable(resolver):
                raise AiValidationError('个人资料参考来源无法验证，请重新提问。')
            found=resolver(member,ordinary)
            if any(item['ref']not in found or found[item['ref']].get('source_hash')!=item['source_hash']for item in ordinary):
                raise AiValidationError('个人资料参考来源已变化，请重新提问。')
        if assistant:
            # Preserve assistant-history provenance; never relabel generated
            # output as a user fact or route it through the user-only resolver.
            from .member_memory_core import _row_hash
            with self.database.connect()as connection:
                for item,identifier in assistant:
                    row=connection.execute("SELECT m.id,m.conversation_id,m.sequence_no,m.role,substr(m.content,1,1600) AS content,m.status,m.created_at,m.updated_at,c.title FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.id=? AND m.role='assistant' AND (m.provider IS NULL OR m.provider<>'fawnsee-welcome')",(member,identifier)).fetchone()
                    if row is None or _row_hash(dict(row))!=item['source_hash']:
                        raise AiValidationError('个人资料参考来源已变化，请重新提问。')
        if multimodal:
            from .member_multimodal11842 import PROOF_FIELDS
            coverage=context.get('capability_coverage',{})
            coverage=coverage.get('multimodal_retrieval',{})if type(coverage)is dict else None
            validator=getattr(memory,'multimodal_validator',None)
            if (not callable(validator)or type(coverage)is not dict or type(coverage.get('model_revision'))is not str
                    or any(type(item)is not dict for item in multimodal)):
                raise AiValidationError('个人图片参考来源无法验证，请重新提问。')
            proof={'settings_revision':context['memory_revision'],'model_revision':coverage['model_revision'],
                   'references':[{key:item.get(key)for key in PROOF_FIELDS}for item in multimodal]}
            try:valid=validator(member,proof)
            except (ValueError,PermissionError):
                raise AiValidationError('个人图片参考来源已变化，请重新提问。')from None
            if valid is not True:
                raise AiValidationError('个人图片参考来源已变化，请重新提问。')

    def _validate_legacy_rpc_result(self,actor_user_id,method,result):
        if method=='build_member_context':
            self._validate_legacy_context(actor_user_id,result)
        elif method=='prepare_member_draft':
            self._open(actor_user_id,result['ticket'],'member')
            self._require_ai_feature(self.get_ai_config(actor_user_id),'member_assistant_enabled','会员 AI 助手')

    @staticmethod
    def _member_system_prompt(context,*,tool_mode=False):
        from ollama_chat_app.services.member_memory_prompt import MEMORY_FACT_RULES
        return AiAssistantService._member_system_prompt(context,tool_mode=tool_mode)+'\n'+MEMORY_FACT_RULES

    def _seal(self, values: dict) -> str:
        raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > 200_000:
            raise AiValidationError("AI 上下文过大，请缩小记录范围")
        data = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(
            self._ticket_secret, b"platform-ai-ticket-v1:" + data.encode(), hashlib.sha256
        ).hexdigest()
        return data + "." + signature

    def _open(self, actor_user_id: int, ticket: str, kind: str) -> dict:
        try:
            if not isinstance(ticket, str) or len(ticket) > 300_000:
                raise ValueError
            data, signature = ticket.rsplit(".", 1)
            expected = hmac.new(
                self._ticket_secret, b"platform-ai-ticket-v1:" + data.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            values = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            if (
                values["actor_id"] != actor_user_id
                or values["kind"] != kind
                or values["expires_at"] <= time.time()
            ):
                raise ValueError
            # Ticket completion must not initiate a second external model call
            # while the dispatcher owns its atomic write/idempotency scope.
            self._validate_legacy_context(actor_user_id,values['context'])
            if (values.get('ai_config_revision')is not None
                    and self._ai_config_revision(self.get_ai_config(actor_user_id))!=values['ai_config_revision']):
                raise ValueError
            return values
        except (ValueError, KeyError, TypeError):
            raise AiValidationError("AI 请求凭据无效或已过期，请重新生成") from None

    def prepare_member_draft(
        self,
        actor_user_id: int,
        provider_name: str,
        model: str,
        prompt: str,
        *,
        member_user_id: int | None = None,
        had_images: bool = False,
        context_days: int | None = None,
    ) -> dict:
        if type(had_images) is not bool:
            raise AiValidationError("图片状态格式无效")
        question = self._required_text(prompt, "问题", 4000)
        self._reject_medical_request(question)
        provider_name = self._required_text(provider_name, "AI 服务", 80)
        model = self._required_text(model, "模型", 160)
        config = self.get_ai_config(actor_user_id)
        self._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        question_token=self._profile_question.set(question)
        try:
            context = self.build_member_context(actor_user_id,member_user_id,
                context_days=self._configured_context_days(config, context_days))
        finally:self._profile_question.reset(question_token)
        if self._ai_config_revision(self.get_ai_config(actor_user_id))!=self._ai_config_revision(config):
            raise AiValidationError('AI 功能配置已变化，请重新提问。')
        values = {
            "kind": "member",
            "actor_id": actor_user_id,
            "context": context,
            "provider": provider_name,
            "model": model,
            "question": question,
            "had_images": bool(had_images),
            "expires_at": int(time.time()) + 600,
            "ai_config_revision":self._ai_config_revision(config),
        }
        return {
            "ticket": self._seal(values),
            "model": model,
            "messages": [
                {"role": "system", "content": self._member_system_prompt(context)},
                {"role": "user", "content": question},
            ],
        }

    def finish_member_draft(self, actor_user_id: int, ticket: str, raw_response: str) -> dict:
        values = self._open(actor_user_id, ticket, "member")
        config = self.get_ai_config(actor_user_id)
        self._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        parsed = self._parse_response(raw_response, allowed_types=MEMBER_PROPOSAL_TYPES)
        context = values["context"]
        member_id = context["member_user_id"]
        evidence = {
            "schema_version": 1,
            "draft_kind": "member_assistant",
            "created_by_actor_id": actor_user_id,
            "confirmation_actor_id": member_id,
            "member_user_id": member_id,
            "question": values["question"],
            "had_images": values["had_images"],
            "context_generated_at": context["generated_at"],
            "proposals": _numbered_pending_proposals(parsed["proposals"]),
        }
        insight_id = self._save_insight(
            user_id=member_id,
            insight_type="assistant_proposals",
            content=parsed["answer"],
            evidence=evidence,
            provider=values["provider"],
            model=values["model"],
            actor_user_id=actor_user_id,
        )
        return self.get_draft(actor_user_id, insight_id)

    def prepare_advisor_summary(
        self,
        advisor_user_id: int,
        member_user_id: int,
        provider_name: str,
        model: str,
        *,
        context_days: int | None = None,
    ) -> dict:
        config = self.get_ai_config(advisor_user_id)
        self._require_ai_feature(config, "advisor_summary_enabled", "顾问 AI 摘要")
        with self.database.connect()as connection:
            actor,_=self._authorise_member_context(connection,advisor_user_id,member_user_id)
        if actor['role_code']!='advisor':
            raise AiValidationError('只有当前绑定顾问可以生成顾问摘要')
        context = self.build_member_context(
            advisor_user_id,
            member_user_id,
            context_days=self._configured_context_days(config, context_days),
        )
        if context["actor_role"] != "advisor":
            raise AiValidationError("只有当前绑定顾问可以生成顾问摘要")
        service_context = self._advisor_service_context(
            advisor_user_id, member_user_id, context_days=context["context_days"]
        )
        provider_name = self._required_text(provider_name, "AI 服务", 80)
        model = self._required_text(model, "模型", 160)
        values = {
            "kind": "advisor",
            "actor_id": advisor_user_id,
            "context": context,
            "provider": provider_name,
            "model": model,
            "expires_at": int(time.time()) + 600,
        }
        return {
            "ticket": self._seal(values),
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": self._advisor_system_prompt(context, service_context),
                },
                {
                    "role": "user",
                    "content": "请基于给定事实生成本周期顾问工作摘要；没有证据的内容请写暂无记录。",
                },
            ],
        }

    def finish_advisor_summary(self, advisor_user_id: int, ticket: str, raw_response: str) -> dict:
        values = self._open(advisor_user_id, ticket, "advisor")
        self._require_ai_feature(
            self.get_ai_config(advisor_user_id), "advisor_summary_enabled", "顾问 AI 摘要"
        )
        parsed = self._parse_response(raw_response, allowed_types=frozenset({"advisor_summary"}))
        if len(parsed["proposals"]) != 1:
            raise AiValidationError("顾问摘要必须且只能包含一个待确认摘要")
        member_id = values["context"]["member_user_id"]
        proposal = dict(parsed["proposals"][0])
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_active_advisor_binding(connection, advisor_user_id, member_id)
            cursor = connection.execute(
                "INSERT INTO advisor_summaries (member_user_id, advisor_user_id, period_start, "
                "period_end, content, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'ai_draft', ?, ?)",
                (
                    member_id,
                    advisor_user_id,
                    proposal["period_start"],
                    proposal["period_end"],
                    proposal["content"],
                    now,
                    now,
                ),
            )
            proposal["summary_id"] = int(cursor.lastrowid)
            evidence = {
                "schema_version": 1,
                "draft_kind": "advisor_summary",
                "created_by_actor_id": advisor_user_id,
                "confirmation_actor_id": advisor_user_id,
                "member_user_id": member_id,
                "context_generated_at": values["context"]["generated_at"],
                "proposals": _numbered_pending_proposals([proposal]),
            }
            insight_id = self._save_insight(
                user_id=member_id,
                insight_type="advisor_summary",
                content=parsed["answer"],
                evidence=evidence,
                provider=values["provider"],
                model=values["model"],
                actor_user_id=advisor_user_id,
            )
        return self.get_draft(advisor_user_id, insight_id)
