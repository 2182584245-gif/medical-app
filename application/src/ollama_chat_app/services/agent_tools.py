"""Allowlisted tools over an already-authorized snapshot, never arbitrary SQL."""

from __future__ import annotations

import json
from collections.abc import Mapping

from ..providers.base import ProviderError

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_life_data",
            "description": "读取已授权的本人资料。没有记录不代表没有发生。不能指定其他用户。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["profile", "records", "reminders"]},
                },
                "required": ["section"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": "提出待确认生活记录、偏好、单次提醒或观察；不直接保存业务数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal": {
                        "type": "object",
                        "description": "必须遵循系统给出的 proposal 格式。",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "life_record",
                                    "profile_fact",
                                    "reminder",
                                    "insight",
                                ],
                            },
                            "category": {
                                "type": "string",
                                "enum": ["diet", "water", "activity", "sleep", "environment"],
                            },
                            "occurred_at": {
                                "type": "string",
                                "description": "带时区 ISO 8601 时间",
                            },
                            "content": {"type": "string"},
                            "details": {
                                "type": "object",
                                "description": "仅用户明确提供或照片可见的信息；未知指标不填",
                            },
                            "fact_key": {"type": "string"},
                            "value": {"type": "string"},
                            "title": {"type": "string"},
                            "scheduled_at": {
                                "type": "string",
                                "description": "提醒的带时区 ISO 8601 时间",
                            },
                            "reminder_type": {"type": "string"},
                        },
                        "required": ["type"],
                    },
                },
                "required": ["proposal"],
                "additionalProperties": False,
            },
        },
    },
]


class SnapshotTools:
    def __init__(self, context: Mapping, *, input_source: str = "text") -> None:
        self.context = context
        self.input_source = input_source if input_source in {"text", "voice", "photo"} else "text"
        self.proposals: list[dict] = []
        self.trace: list[str] = []
        self._seen: set[str] = set()

    def execute(self, name: str, raw_arguments: str) -> dict:
        if not isinstance(raw_arguments, str) or len(raw_arguments) > 16000:
            raise ProviderError("智能体工具参数超限。", code="invalid_tool_call")
        try:
            args = json.loads(raw_arguments)
        except ValueError as exc:
            raise ProviderError("智能体工具参数不是有效 JSON。", code="invalid_tool_json") from exc
        if not isinstance(args, dict):
            raise ProviderError("智能体工具参数必须是对象。", code="invalid_tool_call")
        if name == "read_life_data" and set(args) == {"section"}:
            section = args["section"]
            if section == "profile":
                result = {
                    k: self.context.get(k, {})
                    for k in (
                        "profile",
                        "confirmed_facts",
                        "confirmed_fact_sources",
                    )
                }
            elif section == "records":
                result = {
                    "records": self.context.get("recent_life_records", []),
                    "context_days": self.context.get("context_days"),
                    "note": "有限记录，不能据此断言未记录的行为没有发生。",
                }
            elif section == "reminders":
                result = {"reminders": self.context.get("active_reminders", [])}
            else:
                raise ProviderError("智能体请求了不允许的资料范围。", code="invalid_tool_call")
            self.trace.append(f"read_life_data:{section}")
            return result
        if name == "propose_action" and set(args) == {"proposal"}:
            from .ai_assistant import MEMBER_PROPOSAL_TYPES, AiAssistantService

            proposal = AiAssistantService._validate_proposal(
                args["proposal"], allowed_types=MEMBER_PROPOSAL_TYPES
            )
            if proposal["type"] == "life_record":
                # Origin is assigned by the trusted input adapter, never by the model.
                proposal["details"]["input_source"] = self.input_source
                proposal["details"].pop("external_id", None)
            encoded = json.dumps(proposal, sort_keys=True, ensure_ascii=False)
            if encoded in self._seen:
                return {"status": "already_proposed", "note": "本轮已有同一草稿，不重复添加。"}
            if len(self.proposals) >= 8:
                raise ProviderError("本轮待确认操作超过 8 项。", code="agent_limit")
            self._seen.add(encoded)
            self.proposals.append(proposal)
            self.trace.append(f"propose_action:{proposal['type']}")
            return {
                "status": "pending_confirmation",
                "note": "仅暂存建议，尚未写入生活档案或提醒。",
            }
        raise ProviderError("智能体请求了未授权的工具或参数。", code="invalid_tool_call")
