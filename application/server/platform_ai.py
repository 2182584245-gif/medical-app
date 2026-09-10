"""AI generation stays on the user's device; the server validates and stores drafts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

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
            # Revalidate current authorization after potentially slow, external AI generation.
            self.build_member_context(
                actor_user_id, values["context"]["member_user_id"], context_days=1
            )
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
        context = self.build_member_context(
            actor_user_id,
            member_user_id,
            context_days=self._configured_context_days(config, context_days),
        )
        values = {
            "kind": "member",
            "actor_id": actor_user_id,
            "context": context,
            "provider": provider_name,
            "model": model,
            "question": question,
            "had_images": bool(had_images),
            "expires_at": int(time.time()) + 600,
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
