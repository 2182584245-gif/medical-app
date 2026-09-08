from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..data.database import Database, timestamp_from_db, timestamp_to_db, utc_now
from ..providers.base import ChatProvider, ProviderError
from ..time_utils import as_beijing, beijing_now, beijing_today
from .health import PROFILE_FIELDS, HealthService

MAX_PROMPT_LENGTH = 4_000
MAX_ANSWER_LENGTH = 8_000
MAX_PROPOSALS = 8
MAX_PROPOSAL_CONTENT = 4_000
DEFAULT_CONTEXT_DAYS = 14
MAX_CONTEXT_DAYS = 90
MAX_CONTEXT_RECORDS = 60
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 30 * 1024 * 1024
AI_CONFIG_SETTING_KEY = "structured_ai_config_v1"
DEFAULT_AI_CONFIG: dict[str, bool | int] = {
    "enabled": True,
    "member_assistant_enabled": True,
    "advisor_summary_enabled": True,
    "context_days": DEFAULT_CONTEXT_DAYS,
}

ALLOWED_PROPOSAL_TYPES = frozenset(
    {"life_record", "profile_fact", "reminder", "advisor_summary", "insight"}
)
MEMBER_PROPOSAL_TYPES = frozenset({"life_record", "profile_fact", "reminder", "insight"})
SAFE_PROFILE_FIELDS = frozenset(PROFILE_FIELDS - {"medical_notes"})
REMINDER_TYPES = frozenset(
    {"water", "diet", "sleep", "activity", "environment", "visit", "membership", "custom"}
)

_REQUEST_MEDICAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:帮我|给我|请|如何|怎么).{0,8}(?:诊断|确诊)",
        r"(?:治疗方案|怎么治疗|如何治疗)",
        r"(?:吃|服用|用|换|停).{0,8}(?:什么药|哪种药|药物|处方药)",
        r"(?:调整|增加|减少).{0,6}(?:剂量|药量)",
        r"(?:能不能|可以不可以).{0,8}(?:不去医院|不看医生|停药)",
    )
)
_OUTPUT_DANGER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:诊断|确诊)(?:为|是)",
        r"(?:建议|应该|必须|立即).{0,8}(?:服用|使用|停用|换用|加量|减量).{0,12}(?:药|剂量)",
        r"(?:不用|无需|不必).{0,5}(?:就医|看医生|去医院)",
        r"(?:可以|能够).{0,6}(?:替代|取代).{0,6}(?:医生|治疗|就医)",
        r"(?:保证|一定).{0,5}(?:治愈|痊愈)",
        r"(?:必须|立即).{0,6}(?:购买|下单)",
        r"(?:不买|不购买).{0,12}(?:恶化|严重|危险|死亡)",
    )
)

MEDICAL_BOUNDARY_MESSAGE = (
    "此功能只提供日常生活记录与一般生活建议，不能进行疾病诊断、治疗或用药决策。"
    "如有明显不适、检查异常或用药疑问，请联系医生或药师；紧急情况请及时求助急救服务。"
)


class AiAssistantError(RuntimeError):
    """Base error for the structured, confirmation-first AI boundary."""


class AiPermissionError(AiAssistantError):
    """Raised when an actor tries to read or confirm data outside their scope."""


class AiValidationError(AiAssistantError, ValueError):
    """Raised when AI or UI input is not safe structured data."""


class AiDraftNotFoundError(AiAssistantError, LookupError):
    """Raised when a draft/proposal does not exist in the actor's scope."""


class AiFeatureDisabledError(AiAssistantError):
    """Raised before provider access when an operator has disabled the feature."""


class UnsafeMedicalRequestError(AiAssistantError):
    """Raised before or after a request crosses the non-medical product boundary."""

    def __init__(self) -> None:
        super().__init__(MEDICAL_BOUNDARY_MESSAGE)


class AiAssistantService:
    """Let AI read stable local data while keeping every database write human-controlled.

    Provider output is treated as untrusted input. It is parsed into a small,
    versioned JSON envelope and persisted only inside ``ai_insights``. A life
    record, profile fact or reminder is created only when the member explicitly
    confirms that one proposal. Advisor summaries use the same rule and can be
    confirmed only by the still-bound advisor who requested the draft.
    """

    def __init__(
        self,
        database: Database | None = None,
        health_service: HealthService | None = None,
    ) -> None:
        self.database = database or Database()
        self.database.initialize()
        self.health = health_service or HealthService(self.database)

    def get_ai_config(self, actor_user_id: int | None = None) -> dict[str, bool | int]:
        """Read non-secret global AI settings; any active signed-in role may read them."""

        with self.database.connect() as connection:
            if actor_user_id is not None:
                self._active_actor(connection, actor_user_id)
            row = connection.execute(
                """
                SELECT value_json FROM app_settings
                WHERE user_id IS NULL AND setting_key = ?
                ORDER BY id DESC LIMIT 1
                """,
                (AI_CONFIG_SETTING_KEY,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_AI_CONFIG)
        return self._normalise_ai_config(_loads_json_object(row["value_json"]))

    def update_ai_config(
        self, operator_user_id: int, values: Mapping[str, Any]
    ) -> dict[str, bool | int]:
        """Update global structured-AI behaviour without accepting or storing API keys."""

        if not isinstance(values, Mapping):
            raise AiValidationError("AI 功能配置格式无效")
        allowed = set(DEFAULT_AI_CONFIG)
        unknown = set(values) - allowed
        if unknown:
            raise AiValidationError(
                f"AI 功能配置包含不支持的字段：{', '.join(sorted(map(str, unknown)))}"
            )
        current = self.get_ai_config(operator_user_id)
        merged: dict[str, Any] = {**current, **dict(values)}
        config = self._normalise_ai_config(merged, strict=True)
        now = timestamp_to_db(utc_now())
        encoded = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            actor = self._active_actor(connection, operator_user_id)
            if str(actor["role_code"]) != "operator":
                raise AiPermissionError("只有运营员可以修改全局 AI 功能配置")
            cursor = connection.execute(
                """
                UPDATE app_settings SET value_json = ?, updated_at = ?
                WHERE user_id IS NULL AND setting_key = ?
                """,
                (encoded, now, AI_CONFIG_SETTING_KEY),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """
                    INSERT INTO app_settings (
                        user_id, setting_key, value_json, created_at, updated_at
                    ) VALUES (NULL, ?, ?, ?, ?)
                    """,
                    (AI_CONFIG_SETTING_KEY, encoded, now, now),
                )
            self._audit(
                connection,
                int(actor["id"]),
                "structured_ai.config_updated",
                "app_setting",
                None,
                config,
            )
        return config

    def build_member_context(
        self,
        actor_user_id: int,
        member_user_id: int | None = None,
        *,
        context_days: int = DEFAULT_CONTEXT_DAYS,
    ) -> dict[str, Any]:
        """Return a bounded snapshot for a member or their active advisor."""

        days = self._context_days(context_days)
        with self.database.connect() as connection:
            actor, member_id = self._authorise_member_context(
                connection, actor_user_id, member_user_id
            )
            day_start = beijing_now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = timestamp_to_db(day_start - timedelta(days=days - 1))
            day_end = timestamp_to_db(day_start + timedelta(days=1))
            profile_rows = connection.execute(
                """
                SELECT id, fact_key, value_json, source, effective_at, updated_at
                FROM profile_facts
                WHERE user_id = ? AND status = 'confirmed'
                ORDER BY updated_at DESC, id DESC
                """,
                (member_id,),
            ).fetchall()
            records = connection.execute(
                """
                SELECT id, category, occurred_at, local_date, content, details_json, source
                FROM life_records
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at DESC, id DESC LIMIT ?
                """,
                (member_id, cutoff, day_end, MAX_CONTEXT_RECORDS),
            ).fetchall()
            reminders = connection.execute(
                """
                SELECT id, title, reminder_type, scheduled_at
                FROM reminders
                WHERE user_id = ? AND status = 'active'
                ORDER BY scheduled_at, id LIMIT 30
                """,
                (member_id,),
            ).fetchall()
            profile = connection.execute(
                """
                SELECT display_name, ai_preferred_name, reminder_frequency,
                       health_goals, dietary_preferences, living_situation
                FROM member_profiles WHERE user_id = ?
                """,
                (member_id,),
            ).fetchone()

        facts: dict[str, Any] = {}
        fact_sources: dict[str, str] = {}
        for row in profile_rows:
            key = str(row["fact_key"])
            if key in facts:
                continue
            facts[key] = _loads_json_value(row["value_json"])
            fact_sources[key] = str(row["source"])

        return {
            "schema_version": 1,
            "member_user_id": member_id,
            "actor_role": str(actor["role_code"]),
            "context_days": days,
            "profile": {} if profile is None else _without_none(dict(profile)),
            "confirmed_facts": facts,
            "confirmed_fact_sources": fact_sources,
            "recent_life_records": [
                {
                    "id": int(row["id"]),
                    "category": str(row["category"]),
                    "occurred_at": str(row["occurred_at"]),
                    "local_date": as_beijing(str(row["occurred_at"])).date().isoformat(),
                    "content": str(row["content"]),
                    "details": _loads_json_object(row["details_json"]),
                    "source": str(row["source"]),
                }
                for row in records
            ],
            "active_reminders": [
                {
                    "id": int(row["id"]),
                    "title": str(row["title"]),
                    "reminder_type": str(row["reminder_type"]),
                    "scheduled_at": str(row["scheduled_at"]),
                }
                for row in reminders
            ],
            "generated_at": timestamp_to_db(utc_now()),
        }

    def propose_life_records(
        self,
        actor_user_id: int,
        provider: ChatProvider,
        provider_name: str,
        model: str,
        text: str,
    ) -> list[dict[str, Any]]:
        """Return validated suggestions without writing drafts or life records.

        Desktop callers show these in editable forms. The usual member-owned
        life-record service persists a row only after the member confirms it.
        """
        question = self._required_text(text, "生活描述", MAX_PROMPT_LENGTH)
        self._reject_medical_request(question)
        self._required_text(provider_name, "AI 服务", 80)
        model_name = self._required_text(model, "模型", 160)
        config = self.get_ai_config(actor_user_id)
        self._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        with self.database.connect() as connection:
            actor, member_id = self._authorise_member_context(connection, actor_user_id, None)
        if str(actor["id"]) != str(member_id):
            raise AiPermissionError("只能为当前会员本人整理生活记录")
        # Form extraction needs only this description and the current timestamp.
        # Existing health facts are unnecessary and were not part of its consent.
        context = {"member_user_id": member_id, "generated_at": timestamp_to_db(utc_now())}
        response = provider.chat(
            model_name,
            [
                {
                    "role": "system",
                    "content": self._member_system_prompt(context)
                    + (
                        "\n本次只提取用户这段描述中的生活记录，proposals 只允许 life_record。"
                        "不要把历史资料重新生成记录，不推测未提及的食物、数量、时间或身体事实。"
                        "缺少时刻时可使用上下文 generated_at 并在 content 注明时间待用户确认。"
                        "不确定的内容明确标注供用户编辑，不能生成用药记录。"
                    ),
                },
                {"role": "user", "content": question},
            ],
        )
        parsed = self._parse_response(response, allowed_types=frozenset({"life_record"}))
        return [dict(item) for item in parsed["proposals"]]

    def create_member_draft(
        self,
        actor_user_id: int,
        provider: ChatProvider,
        provider_name: str,
        model: str,
        prompt: str,
        *,
        member_user_id: int | None = None,
        images: Sequence[str | bytes] | None = None,
        context_days: int | None = None,
    ) -> dict[str, Any]:
        """Generate one untrusted response and store only a reviewable draft."""

        question = self._required_text(prompt, "问题", MAX_PROMPT_LENGTH)
        self._reject_medical_request(question)
        provider_label = self._required_text(provider_name, "AI 服务", 80)
        model_label = self._required_text(model, "模型", 160)
        image_list = self._normalise_images(images)
        if image_list and not provider.supports_images:
            raise ProviderError(
                "当前 AI 服务只支持文字输入；如需识别图片，请选择支持视觉的 Ollama 模型。",
                code="images_not_supported",
            )

        config = self.get_ai_config(actor_user_id)
        self._require_ai_feature(config, "member_assistant_enabled", "会员 AI 助手")
        effective_days = self._configured_context_days(config, context_days)
        context = self.build_member_context(
            actor_user_id, member_user_id, context_days=effective_days
        )
        member_id = int(context["member_user_id"])
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_user_id)
        user_message: dict[str, Any] = {"role": "user", "content": question}
        if image_list:
            user_message["images"] = image_list
        raw_response = provider.chat(
            model_label,
            [
                {"role": "system", "content": self._member_system_prompt(context)},
                user_message,
            ],
        )
        parsed = self._parse_response(raw_response, allowed_types=MEMBER_PROPOSAL_TYPES)
        evidence = {
            "schema_version": 1,
            "draft_kind": "member_assistant",
            "created_by_actor_id": int(actor["id"]),
            "confirmation_actor_id": member_id,
            "member_user_id": member_id,
            "question": question,
            "had_images": bool(image_list),
            "context_generated_at": context["generated_at"],
            "proposals": _numbered_pending_proposals(parsed["proposals"]),
        }
        insight_id = self._save_insight(
            user_id=member_id,
            insight_type="assistant_proposals",
            content=parsed["answer"],
            evidence=evidence,
            provider=provider_label,
            model=model_label,
            actor_user_id=int(actor["id"]),
        )
        return self.get_draft(actor_user_id, insight_id)

    def create_advisor_summary_draft(
        self,
        advisor_user_id: int,
        member_user_id: int,
        provider: ChatProvider,
        provider_name: str,
        model: str,
        *,
        context_days: int | None = None,
    ) -> dict[str, Any]:
        """Create an AI draft that only the active bound advisor may confirm."""

        config = self.get_ai_config(advisor_user_id)
        self._require_ai_feature(config, "advisor_summary_enabled", "顾问 AI 摘要")
        effective_days = self._configured_context_days(config, context_days)
        context = self.build_member_context(
            advisor_user_id, member_user_id, context_days=effective_days
        )
        if context["actor_role"] != "advisor":
            raise AiPermissionError("只有当前绑定顾问可以生成顾问摘要")
        provider_label = self._required_text(provider_name, "AI 服务", 80)
        model_label = self._required_text(model, "模型", 160)
        service_context = self._advisor_service_context(advisor_user_id, member_user_id)
        raw_response = provider.chat(
            model_label,
            [
                {
                    "role": "system",
                    "content": self._advisor_system_prompt(context, service_context),
                },
                {
                    "role": "user",
                    "content": (
                        "请基于给定事实生成本周期顾问工作摘要；没有证据的内容请写“暂无记录”。"
                    ),
                },
            ],
        )
        parsed = self._parse_response(raw_response, allowed_types=frozenset({"advisor_summary"}))
        if len(parsed["proposals"]) != 1:
            raise AiValidationError("顾问摘要必须且只能包含一个待确认摘要")
        proposal = dict(parsed["proposals"][0])
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_active_advisor_binding(connection, advisor_user_id, member_user_id)
            cursor = connection.execute(
                """
                INSERT INTO advisor_summaries (
                    member_user_id, advisor_user_id, period_start, period_end,
                    content, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'ai_draft', ?, ?)
                """,
                (
                    member_user_id,
                    advisor_user_id,
                    proposal["period_start"],
                    proposal["period_end"],
                    proposal["content"],
                    now,
                    now,
                ),
            )
            summary_id = int(cursor.lastrowid)
        proposal["summary_id"] = summary_id
        evidence = {
            "schema_version": 1,
            "draft_kind": "advisor_summary",
            "created_by_actor_id": advisor_user_id,
            "confirmation_actor_id": advisor_user_id,
            "member_user_id": member_user_id,
            "context_generated_at": context["generated_at"],
            "proposals": _numbered_pending_proposals([proposal]),
        }
        insight_id = self._save_insight(
            user_id=member_user_id,
            insight_type="advisor_summary",
            content=parsed["answer"],
            evidence=evidence,
            provider=provider_label,
            model=model_label,
            actor_user_id=advisor_user_id,
        )
        return self.get_draft(advisor_user_id, insight_id)

    def list_drafts(
        self,
        actor_user_id: int,
        *,
        member_user_id: int | None = None,
        include_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        actor_id = self._positive_id(actor_user_id, "操作者编号")
        statuses = (
            ("draft", "shown", "accepted", "dismissed")
            if include_resolved
            else (
                "draft",
                "shown",
            )
        )
        placeholders = ",".join("?" for _ in statuses)
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_id)
            parameters: list[Any] = list(statuses)
            query = (
                "SELECT id, user_id, insight_type, content, evidence_json, provider, model, "
                "status, created_at, updated_at FROM ai_insights "
                f"WHERE status IN ({placeholders})"
            )
            if member_user_id is not None:
                member_id = self._positive_id(member_user_id, "会员编号")
                self._authorise_member_context(connection, actor_id, member_id)
                query += " AND user_id = ?"
                parameters.append(member_id)
            query += " ORDER BY created_at DESC, id DESC LIMIT 100"
            rows = connection.execute(query, parameters).fetchall()
            drafts = [self._draft_from_row(row) for row in rows]
            visible = [
                draft for draft in drafts if self._draft_visible_to_actor(connection, actor, draft)
            ]
        return visible

    def get_draft(self, actor_user_id: int, insight_id: int) -> dict[str, Any]:
        actor_id = self._positive_id(actor_user_id, "操作者编号")
        insight_id_value = self._positive_id(insight_id, "AI 草稿编号")
        with self.database.connect() as connection:
            actor = self._active_actor(connection, actor_id)
            row = connection.execute(
                """
                SELECT id, user_id, insight_type, content, evidence_json, provider,
                       model, status, created_at, updated_at
                FROM ai_insights WHERE id = ?
                """,
                (insight_id_value,),
            ).fetchone()
            if row is None:
                raise AiDraftNotFoundError("AI 草稿不存在或无权访问")
            draft = self._draft_from_row(row)
            if not self._draft_visible_to_actor(connection, actor, draft):
                raise AiDraftNotFoundError("AI 草稿不存在或无权访问")
        return draft

    def confirm_proposal(
        self, actor_user_id: int, insight_id: int, proposal_id: int
    ) -> dict[str, Any]:
        """Apply exactly one proposal after rechecking actor and safety boundaries."""

        actor_id = self._positive_id(actor_user_id, "操作者编号")
        draft = self.get_draft(actor_id, insight_id)
        evidence = draft["evidence"]
        if actor_id != int(evidence.get("confirmation_actor_id", 0)):
            raise AiPermissionError("只有需要确认该信息的本人或当前顾问可以确认")
        proposal = self._pending_proposal(evidence, proposal_id)
        self._ensure_safe_output(_proposal_searchable_text(proposal))
        member_id = int(evidence["member_user_id"])
        proposal_type = str(proposal["type"])

        if proposal_type == "life_record":
            created_entity_id = self.health.add_life_record(
                member_id,
                str(proposal["category"]),
                str(proposal["occurred_at"]),
                str(proposal["content"]),
                source="ai_confirmed",
                details=proposal.get("details"),
            )
        elif proposal_type == "profile_fact":
            field = str(proposal["fact_key"])
            self.health.save_profile(member_id, {field: proposal["value"]})
            created_entity_id = self._save_confirmed_profile_fact(
                actor_id, member_id, field, proposal["value"]
            )
        elif proposal_type == "reminder":
            created_entity_id = self.health.add_reminder(
                member_id,
                str(proposal["title"]),
                str(proposal["scheduled_at"]),
                reminder_type=str(proposal["reminder_type"]),
            )
        elif proposal_type == "advisor_summary":
            created_entity_id = self._confirm_advisor_summary(actor_id, member_id, proposal)
        elif proposal_type == "insight":
            created_entity_id = int(draft["id"])
        else:  # pragma: no cover - validated before persistence
            raise AiValidationError("AI 建议类型无效")

        return self._resolve_proposal(
            actor_id,
            int(draft["id"]),
            proposal_id,
            resolution="accepted",
            created_entity_id=created_entity_id,
        )

    def dismiss_proposal(
        self, actor_user_id: int, insight_id: int, proposal_id: int
    ) -> dict[str, Any]:
        actor_id = self._positive_id(actor_user_id, "操作者编号")
        draft = self.get_draft(actor_id, insight_id)
        evidence = draft["evidence"]
        if actor_id != int(evidence.get("confirmation_actor_id", 0)):
            raise AiPermissionError("只有需要确认该信息的本人或当前顾问可以忽略")
        proposal = self._pending_proposal(evidence, proposal_id)
        if proposal["type"] == "advisor_summary" and proposal.get("summary_id"):
            with self.database.transaction() as connection:
                self._require_active_advisor_binding(
                    connection, actor_id, int(evidence["member_user_id"])
                )
                connection.execute(
                    "DELETE FROM advisor_summaries WHERE id = ? AND source = 'ai_draft'",
                    (int(proposal["summary_id"]),),
                )
        return self._resolve_proposal(
            actor_id, int(draft["id"]), proposal_id, resolution="dismissed"
        )

    def _save_insight(
        self,
        *,
        user_id: int,
        insight_type: str,
        content: str,
        evidence: Mapping[str, Any],
        provider: str,
        model: str,
        actor_user_id: int,
    ) -> int:
        now = timestamp_to_db(utc_now())
        evidence_json = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ai_insights (
                    user_id, insight_type, content, evidence_json, provider,
                    model, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (user_id, insight_type, content, evidence_json, provider, model, now, now),
            )
            insight_id = int(cursor.lastrowid)
            self._audit(
                connection,
                actor_user_id,
                "ai_draft.created",
                "ai_insight",
                insight_id,
                {"member_user_id": user_id, "insight_type": insight_type},
            )
        return insight_id

    def _save_confirmed_profile_fact(
        self, actor_id: int, member_id: int, fact_key: str, value: Any
    ) -> int:
        now = timestamp_to_db(utc_now())
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE profile_facts SET status = 'superseded', updated_at = ?
                WHERE user_id = ? AND fact_key = ? AND status = 'confirmed'
                """,
                (now, member_id, fact_key),
            )
            cursor = connection.execute(
                """
                INSERT INTO profile_facts (
                    user_id, fact_key, value_json, source, status,
                    effective_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'ai_confirmed', 'confirmed', ?, ?, ?)
                """,
                (member_id, fact_key, value_json, now, now, now),
            )
            fact_id = int(cursor.lastrowid)
            self._audit(
                connection,
                actor_id,
                "profile_fact.ai_confirmed",
                "profile_fact",
                fact_id,
                {"member_user_id": member_id, "fact_key": fact_key},
            )
        return fact_id

    def _confirm_advisor_summary(
        self, advisor_id: int, member_id: int, proposal: Mapping[str, Any]
    ) -> int:
        summary_id = self._positive_id(proposal.get("summary_id"), "顾问摘要编号")
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            self._require_active_advisor_binding(connection, advisor_id, member_id)
            cursor = connection.execute(
                """
                UPDATE advisor_summaries SET source = 'ai_confirmed', updated_at = ?
                WHERE id = ? AND member_user_id = ? AND advisor_user_id = ?
                  AND source = 'ai_draft'
                """,
                (now, summary_id, member_id, advisor_id),
            )
            if cursor.rowcount != 1:
                raise AiDraftNotFoundError("顾问摘要草稿不存在或已经处理")
            self._audit(
                connection,
                advisor_id,
                "advisor_summary.ai_confirmed",
                "advisor_summary",
                summary_id,
                {"member_user_id": member_id},
            )
        return summary_id

    def _resolve_proposal(
        self,
        actor_id: int,
        insight_id: int,
        proposal_id: int,
        *,
        resolution: str,
        created_entity_id: int | None = None,
    ) -> dict[str, Any]:
        now = timestamp_to_db(utc_now())
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT evidence_json FROM ai_insights WHERE id = ?",
                (insight_id,),
            ).fetchone()
            if row is None:
                raise AiDraftNotFoundError("AI 草稿不存在或无权访问")
            evidence = _loads_json_object(row["evidence_json"])
            proposal = self._pending_proposal(evidence, proposal_id)
            proposal["status"] = resolution
            proposal["resolved_at"] = now
            if created_entity_id is not None:
                proposal["created_entity_id"] = int(created_entity_id)
            states = {str(item.get("status")) for item in evidence["proposals"]}
            if "pending" in states:
                draft_status = "shown"
            elif "accepted" in states:
                draft_status = "accepted"
            else:
                draft_status = "dismissed"
            connection.execute(
                """
                UPDATE ai_insights
                SET evidence_json = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                    draft_status,
                    now,
                    insight_id,
                ),
            )
            self._audit(
                connection,
                actor_id,
                f"ai_proposal.{resolution}",
                "ai_insight",
                insight_id,
                {"proposal_id": proposal_id, "proposal_type": proposal["type"]},
            )
        return self.get_draft(actor_id, insight_id)

    def _advisor_service_context(self, advisor_user_id: int, member_user_id: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            self._require_active_advisor_binding(connection, advisor_user_id, member_user_id)
            tasks = connection.execute(
                """
                SELECT title, scheduled_at, status, notes
                FROM visit_tasks
                WHERE member_user_id = ? AND advisor_user_id = ?
                ORDER BY COALESCE(scheduled_at, created_at) DESC LIMIT 20
                """,
                (member_user_id, advisor_user_id),
            ).fetchall()
            visits = connection.execute(
                """
                SELECT visited_at, summary, details_json
                FROM visit_records
                WHERE member_user_id = ? AND advisor_user_id = ?
                ORDER BY visited_at DESC LIMIT 10
                """,
                (member_user_id, advisor_user_id),
            ).fetchall()
        return {
            "visit_tasks": [_without_none(dict(row)) for row in tasks],
            "visit_records": [
                {
                    **_without_none({key: row[key] for key in ("visited_at", "summary")}),
                    "details": _loads_json_object(row["details_json"]),
                }
                for row in visits
            ],
        }

    @staticmethod
    def _member_system_prompt(context: Mapping[str, Any]) -> str:
        return (
            "你是健康生活服务平台的日常生活助手，不是医生。只能根据下方已确认事实和近期记录回答。"
            "不得诊断疾病、制定治疗或用药方案、替代就医，也不得用恐吓促销。信息不足时明确说不知道。"
            "不要把推测写成事实。任何记录、档案或提醒都只能作为待用户确认的 proposals。\n"
            f"当前北京时间：{beijing_now().isoformat(timespec='seconds')}。"
            "默认时区为北京时间（UTC+08:00）。用户未说明时区的日期时间，"
            "以及今天、明天等相对日期，均按北京时间理解。"
            "occurred_at、scheduled_at 必须输出带时区的 ISO 8601 时间；"
            "北京时间使用 +08:00 后缀，例如 2026-09-05T08:30:00+08:00。"
            "用户明确指定其他时区时保留其正确时刻。\n"
            "只输出一个 JSON 对象，禁止 Markdown 和额外文字。顶层必须且只能有 answer、proposals。"
            "answer 是中文字符串；proposals 是数组，最多 8 项。允许格式：\n"
            '{"type":"life_record","category":"diet|water|activity|sleep|environment",'
            '"occurred_at":"带时区ISO时间","content":"内容","details":{}}\n'
            '{"type":"profile_fact","fact_key":"安全档案字段","value":"值"}\n'
            '{"type":"reminder","title":"标题","reminder_type":"water|diet|sleep|activity|'
            'environment|visit|membership|custom","scheduled_at":"带时区ISO时间"}\n'
            '{"type":"insight","content":"基于记录的温和一般性提示"}\n'
            f"可用安全档案字段：{','.join(sorted(SAFE_PROFILE_FIELDS))}。\n"
            "稳定数据快照：" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def _advisor_system_prompt(
        member_context: Mapping[str, Any], service_context: Mapping[str, Any]
    ) -> str:
        now = beijing_now()
        start = (now.date() - timedelta(days=13)).isoformat()
        end = now.date().isoformat()
        return (
            "你为当前绑定生活顾问整理工作摘要，不是医生。只归纳所给事实，不诊断、不治疗、不提供用药建议，"
            f"当前北京时间：{now.isoformat(timespec='seconds')}。"
            "默认时区为北京时间（UTC+08:00），摘要日期和相对日期均按北京时间理解。"
            "不得推测疾病或使用恐吓营销。只输出 JSON，顶层必须且只能有 answer、proposals。"
            "proposals 必须且只能有一项："
            '{"type":"advisor_summary","period_start":"YYYY-MM-DD",'
            '"period_end":"YYYY-MM-DD","content":"包括近期问题、饮食/生活变化、待处理事项、'
            '会员需求、上次服务后续；缺失写暂无记录"}。'
            f"建议周期 {start} 至 {end}。会员数据："
            + json.dumps(member_context, ensure_ascii=False, separators=(",", ":"))
            + "；服务数据："
            + json.dumps(service_context, ensure_ascii=False, separators=(",", ":"))
        )

    @classmethod
    def _parse_response(cls, raw: str, *, allowed_types: frozenset[str]) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw.strip():
            raise AiValidationError("AI 返回了空内容，请重试")
        text = raw.strip()
        fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AiValidationError("AI 返回内容不是有效 JSON，未保存任何事实") from exc
        if not isinstance(payload, dict) or set(payload) != {"answer", "proposals"}:
            raise AiValidationError("AI 返回格式不符合安全结构，未保存任何事实")
        answer = cls._required_text(payload["answer"], "AI 回答", MAX_ANSWER_LENGTH)
        cls._ensure_safe_output(answer)
        raw_proposals = payload["proposals"]
        if not isinstance(raw_proposals, list) or len(raw_proposals) > MAX_PROPOSALS:
            raise AiValidationError("AI 待确认项必须是最多 8 项的列表")
        proposals = [
            cls._validate_proposal(item, allowed_types=allowed_types) for item in raw_proposals
        ]
        return {"answer": answer, "proposals": proposals}

    @classmethod
    def _validate_proposal(cls, value: Any, *, allowed_types: frozenset[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AiValidationError("AI 待确认项格式无效")
        proposal_type = value.get("type")
        if proposal_type not in ALLOWED_PROPOSAL_TYPES or proposal_type not in allowed_types:
            raise AiValidationError("AI 返回了当前场景不允许的待确认项")
        expected_fields: dict[str, set[str]] = {
            "life_record": {"type", "category", "occurred_at", "content", "details"},
            "profile_fact": {"type", "fact_key", "value"},
            "reminder": {"type", "title", "reminder_type", "scheduled_at"},
            "advisor_summary": {"type", "period_start", "period_end", "content"},
            "insight": {"type", "content"},
        }
        required_fields: dict[str, set[str]] = {
            **expected_fields,
            "life_record": {"type", "category", "occurred_at", "content"},
        }
        if (
            not required_fields[proposal_type] <= set(value)
            or not set(value) <= expected_fields[proposal_type]
        ):
            raise AiValidationError("AI 待确认项字段不完整或包含未知字段")
        item = dict(value)
        if proposal_type == "life_record":
            if item["category"] not in {"diet", "water", "activity", "sleep", "environment"}:
                raise AiValidationError("AI 生活记录分类无效")
            cls._require_aware_iso(item["occurred_at"], "生活记录时间")
            item["content"] = cls._required_text(
                item["content"], "生活记录内容", MAX_PROPOSAL_CONTENT
            )
            details = item.get("details")
            if details is not None and not isinstance(details, dict):
                raise AiValidationError("AI 生活记录详情必须是对象")
            item["details"] = {} if details is None else details
        elif proposal_type == "profile_fact":
            if item["fact_key"] not in SAFE_PROFILE_FIELDS:
                raise AiValidationError("AI 尝试写入不允许自动建议的档案字段")
            if isinstance(item["value"], (dict, list)):
                raise AiValidationError("AI 档案建议值必须是简单值")
            if item["value"] is None:
                raise AiValidationError("AI 档案建议值不能为空")
            cls._validate_profile_proposal_value(str(item["fact_key"]), item["value"])
        elif proposal_type == "reminder":
            item["title"] = cls._required_text(item["title"], "提醒标题", 200)
            if item["reminder_type"] not in REMINDER_TYPES:
                raise AiValidationError("AI 提醒类型无效；不支持用药提醒")
            cls._require_aware_iso(item["scheduled_at"], "提醒时间")
        elif proposal_type == "advisor_summary":
            start = cls._require_date(item["period_start"], "摘要开始日期")
            end = cls._require_date(item["period_end"], "摘要结束日期")
            if start > end:
                raise AiValidationError("顾问摘要开始日期不能晚于结束日期")
            item["content"] = cls._required_text(item["content"], "顾问摘要", MAX_PROPOSAL_CONTENT)
        else:
            item["content"] = cls._required_text(item["content"], "生活提示", MAX_PROPOSAL_CONTENT)
        cls._ensure_safe_output(_proposal_searchable_text(item))
        try:
            encoded = json.dumps(item, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise AiValidationError("AI 待确认项包含无法保存的数据") from exc
        if len(encoded) > 12_000:
            raise AiValidationError("AI 待确认项过长")
        return item

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> dict[str, Any]:
        evidence = _loads_json_object(row["evidence_json"])
        if not isinstance(evidence.get("proposals"), list):
            evidence["proposals"] = []
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "insight_type": str(row["insight_type"]),
            "answer": str(row["content"]),
            "evidence": evidence,
            "proposals": evidence["proposals"],
            "provider": None if row["provider"] is None else str(row["provider"]),
            "model": None if row["model"] is None else str(row["model"]),
            "status": str(row["status"]),
            "created_at": timestamp_from_db(row["created_at"]),
            "updated_at": timestamp_from_db(row["updated_at"]),
        }

    @staticmethod
    def _draft_visible_to_actor(
        connection: sqlite3.Connection,
        actor: sqlite3.Row,
        draft: Mapping[str, Any],
    ) -> bool:
        actor_id = int(actor["id"])
        role = str(actor["role_code"])
        evidence = draft["evidence"]
        if role == "member":
            if evidence.get("draft_kind") == "advisor_summary" and draft.get("status") in {
                "draft",
                "shown",
            }:
                return False
            return actor_id == int(draft["user_id"])
        if role == "advisor":
            try:
                member_id = int(evidence.get("member_user_id", 0))
                named_actor = actor_id in {
                    int(evidence.get("created_by_actor_id", 0)),
                    int(evidence.get("confirmation_actor_id", 0)),
                }
            except (TypeError, ValueError):
                return False
            if not named_actor:
                return False
            return (
                connection.execute(
                    """
                    SELECT 1 FROM advisor_bindings
                    WHERE advisor_user_id = ? AND member_user_id = ? AND status = 'active'
                    """,
                    (actor_id, member_id),
                ).fetchone()
                is not None
            )
        return False

    @staticmethod
    def _pending_proposal(evidence: Mapping[str, Any], proposal_id: int) -> dict[str, Any]:
        value = AiAssistantService._positive_id(proposal_id, "待确认项编号")
        proposals = evidence.get("proposals")
        if not isinstance(proposals, list):
            raise AiDraftNotFoundError("待确认项不存在或已经处理")
        for proposal in proposals:
            if isinstance(proposal, dict) and proposal.get("id") == value:
                if proposal.get("status") != "pending":
                    raise AiDraftNotFoundError("待确认项不存在或已经处理")
                return proposal
        raise AiDraftNotFoundError("待确认项不存在或已经处理")

    @staticmethod
    def _authorise_member_context(
        connection: sqlite3.Connection,
        actor_user_id: int,
        member_user_id: int | None,
    ) -> tuple[sqlite3.Row, int]:
        actor = AiAssistantService._active_actor(connection, actor_user_id)
        actor_id = int(actor["id"])
        role = str(actor["role_code"])
        member_id = (
            actor_id
            if member_user_id is None
            else AiAssistantService._positive_id(member_user_id, "会员编号")
        )
        member = connection.execute(
            "SELECT id, role_code, account_status FROM users WHERE id = ?",
            (member_id,),
        ).fetchone()
        if (
            member is None
            or str(member["role_code"]) != "member"
            or str(member["account_status"]) != "active"
        ):
            raise AiPermissionError("会员不存在、已停用或无权访问")
        if role == "member" and actor_id == member_id:
            return actor, member_id
        if role == "advisor":
            AiAssistantService._require_active_advisor_binding(connection, actor_id, member_id)
            return actor, member_id
        raise AiPermissionError("只有会员本人或当前绑定顾问可以读取该上下文")

    @staticmethod
    def _active_actor(connection: sqlite3.Connection, actor_user_id: int) -> sqlite3.Row:
        actor_id = AiAssistantService._positive_id(actor_user_id, "操作者编号")
        row = connection.execute(
            "SELECT id, role_code, account_status FROM users WHERE id = ?",
            (actor_id,),
        ).fetchone()
        if row is None or str(row["account_status"]) != "active":
            raise AiPermissionError("账号不存在、已停用或无权访问")
        return row

    @staticmethod
    def _require_active_advisor_binding(
        connection: sqlite3.Connection, advisor_user_id: int, member_user_id: int
    ) -> None:
        advisor_id = AiAssistantService._positive_id(advisor_user_id, "顾问编号")
        member_id = AiAssistantService._positive_id(member_user_id, "会员编号")
        row = connection.execute(
            """
            SELECT 1 FROM advisor_bindings b
            JOIN users a ON a.id = b.advisor_user_id
            JOIN users m ON m.id = b.member_user_id
            WHERE b.advisor_user_id = ? AND b.member_user_id = ?
              AND b.status = 'active' AND a.role_code = 'advisor'
              AND a.account_status = 'active' AND m.role_code = 'member'
              AND m.account_status = 'active'
            """,
            (advisor_id, member_id),
        ).fetchone()
        if row is None:
            raise AiPermissionError("只有当前绑定顾问可以操作该会员摘要")

    @staticmethod
    def _normalise_images(images: Sequence[str | bytes] | None) -> list[str | bytes]:
        if images is None:
            return []
        if isinstance(images, (str, bytes)):
            raise AiValidationError("图片列表格式无效")
        if len(images) > 4:
            raise AiValidationError("一次最多选择 4 张图片")
        result: list[str | bytes] = []
        total_bytes = 0
        for image in images:
            if isinstance(image, bytes):
                if not image:
                    raise AiValidationError("图片内容格式无效")
                if len(image) > MAX_IMAGE_BYTES:
                    raise AiValidationError("单张图片不能超过 15 MB")
                total_bytes += len(image)
                result.append(image)
                continue
            if not isinstance(image, str) or not image.strip():
                raise AiValidationError("图片内容格式无效")
            result.append(image.strip())
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise AiValidationError("本次图片总大小不能超过 30 MB")
        return result

    @staticmethod
    def _reject_medical_request(text: str) -> None:
        if any(pattern.search(text) for pattern in _REQUEST_MEDICAL_PATTERNS):
            raise UnsafeMedicalRequestError

    @staticmethod
    def _ensure_safe_output(text: str) -> None:
        if any(pattern.search(text) for pattern in _OUTPUT_DANGER_PATTERNS):
            raise UnsafeMedicalRequestError

    @classmethod
    def _normalise_ai_config(
        cls,
        value: Mapping[str, Any],
        *,
        strict: bool = False,
    ) -> dict[str, bool | int]:
        config = dict(DEFAULT_AI_CONFIG)
        for key in ("enabled", "member_assistant_enabled", "advisor_summary_enabled"):
            candidate = value.get(key, config[key])
            if not isinstance(candidate, bool):
                if strict:
                    raise AiValidationError(f"{key} 必须是开启或关闭状态")
                continue
            config[key] = candidate
        days = value.get("context_days", config["context_days"])
        try:
            config["context_days"] = cls._context_days(days)
        except AiValidationError:
            if strict:
                raise
        return config

    @staticmethod
    def _require_ai_feature(
        config: Mapping[str, bool | int], feature_key: str, feature_label: str
    ) -> None:
        if not bool(config.get("enabled")) or not bool(config.get(feature_key)):
            raise AiFeatureDisabledError(
                f"{feature_label}当前已由运营人员关闭，未向任何 AI 服务发送数据。"
            )

    @classmethod
    def _configured_context_days(
        cls, config: Mapping[str, bool | int], requested_days: int | None
    ) -> int:
        configured = cls._context_days(config.get("context_days"))
        if requested_days is None:
            return configured
        # A caller may deliberately request less history for privacy, but may
        # never exceed the operator-configured global maximum.
        return min(configured, cls._context_days(requested_days))

    @staticmethod
    def _positive_id(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise AiValidationError(f"{label}无效")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise AiValidationError(f"{label}无效") from exc
        if result <= 0:
            raise AiValidationError(f"{label}无效")
        return result

    @staticmethod
    def _required_text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise AiValidationError(f"{label}必须是文字")
        result = value.strip()
        if not result:
            raise AiValidationError(f"{label}不能为空")
        if len(result) > maximum:
            raise AiValidationError(f"{label}不能超过 {maximum} 个字符")
        return result

    @staticmethod
    def _context_days(value: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_CONTEXT_DAYS
        ):
            raise AiValidationError(f"上下文天数必须在 1 到 {MAX_CONTEXT_DAYS} 之间")
        return value

    @staticmethod
    def _require_aware_iso(value: Any, label: str) -> datetime:
        if not isinstance(value, str):
            raise AiValidationError(f"{label}必须是带时区的 ISO 时间")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AiValidationError(f"{label}格式无效") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise AiValidationError(f"{label}必须包含时区")
        return parsed.astimezone(UTC)

    @staticmethod
    def _require_date(value: Any, label: str) -> date:
        if not isinstance(value, str):
            raise AiValidationError(f"{label}格式无效")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise AiValidationError(f"{label}格式无效") from exc

    @classmethod
    def _validate_profile_proposal_value(cls, field: str, value: Any) -> None:
        if field == "height_cm":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AiValidationError("AI 建议的身高必须是数字")
            if not 30 <= float(value) <= 300:
                raise AiValidationError("AI 建议的身高超出允许范围")
            return
        if field == "gender":
            if value not in {"female", "male", "other", "unspecified"}:
                raise AiValidationError("AI 建议的性别字段无效")
            return
        if field == "reminder_frequency":
            if value not in {"normal", "low", "off"}:
                raise AiValidationError("AI 建议的提醒频率无效")
            return
        if field == "birth_date":
            parsed = cls._require_date(value, "AI 建议的出生日期")
            if parsed > beijing_today():
                raise AiValidationError("AI 建议的出生日期不能晚于今天")
            return
        cls._required_text(value, "AI 建议的档案值", MAX_PROPOSAL_CONTENT)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor_user_id: int,
        action: str,
        entity_type: str,
        entity_id: int | None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, action, entity_type, entity_id, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                entity_type,
                entity_id,
                None if details is None else json.dumps(details, ensure_ascii=False),
                timestamp_to_db(utc_now()),
            ),
        )


def _numbered_pending_proposals(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": index, "status": "pending", **dict(proposal)}
        for index, proposal in enumerate(values, start=1)
    ]


def _proposal_searchable_text(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)


def _loads_json_object(value: Any) -> dict[str, Any]:
    loaded = _loads_json_value(value)
    return loaded if isinstance(loaded, dict) else {}


def _without_none(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


__all__ = [
    "AI_CONFIG_SETTING_KEY",
    "AiAssistantError",
    "AiAssistantService",
    "AiDraftNotFoundError",
    "AiFeatureDisabledError",
    "AiPermissionError",
    "AiValidationError",
    "DEFAULT_AI_CONFIG",
    "MEDICAL_BOUNDARY_MESSAGE",
    "UnsafeMedicalRequestError",
]
