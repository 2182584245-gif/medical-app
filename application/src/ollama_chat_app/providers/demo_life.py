"""Explicit, deterministic workflow demo. No key, network, or model inference.

Only the documented example grammar is supported. Unknown input is never
guessed into a fact. It shares production validation and confirmation paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from ..time_utils import beijing_now
from .base import ChatProvider, ProviderError

DEMO_MODEL = "life-workflow-demo-v1"
DEMO_LABEL = "【离线流程演示 · 非 DeepSeek 回答】"


def _snapshot(system: str, marker: str) -> dict:
    try:
        value, _ = json.JSONDecoder().raw_decode(system.split(marker, 1)[1])
        return value if isinstance(value, dict) else {}
    except (IndexError, ValueError):
        return {}


class DemoLifeProvider(ChatProvider):
    def chat_tools(self, model: str, messages: Sequence[Mapping], tools: Sequence[Mapping]) -> dict:
        """Exercise the same tool loop with scripted decisions, never a model call."""
        results = [m for m in messages if m.get("role") == "tool"]
        if not results:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"demo-read-{section}",
                        "type": "function",
                        "function": {
                            "name": "read_life_data",
                            "arguments": json.dumps({"section": section}),
                        },
                    }
                    for section in ("profile", "records", "reminders")
                ],
            }
        context = {}
        for result in results:
            data = json.loads(result["content"])
            if "confirmed_facts" in data:
                context.update(data)
            if "records" in data:
                context["recent_life_records"] = data["records"]
            if "reminders" in data:
                context["active_reminders"] = data["reminders"]
        dialogue = [m for m in messages if m.get("role") != "tool" and not m.get("tool_calls")]
        current = next(m for m in reversed(dialogue) if m.get("role") == "user")
        answer, proposals = self._member(str(current["content"]), context, dialogue)
        if proposals and not any(
            str(m.get("tool_call_id", "")).startswith("demo-propose") for m in results
        ):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"demo-propose-{index}",
                        "type": "function",
                        "function": {
                            "name": "propose_action",
                            "arguments": json.dumps({"proposal": proposal}),
                        },
                    }
                    for index, proposal in enumerate(proposals)
                ],
            }
        return {
            "content": json.dumps(
                {"answer": f"{DEMO_LABEL}\n{answer}", "proposals": []}, ensure_ascii=False
            ),
            "tool_calls": [],
        }

    def chat(self, model: str, messages: Sequence[Mapping]) -> str:
        if any(m.get("images") for m in messages):
            raise ProviderError("离线演示不识别图片，请用文字演示；图片接口已预留。")
        system = str(messages[0].get("content", ""))
        prompt = str(messages[-1].get("content", "")).strip()
        if "；服务数据：" in system:
            return self._advisor(system)
        context = _snapshot(system, "稳定数据快照：")
        structured = "proposals" in system and "稳定数据快照：" in system
        answer, proposals = self._member(prompt, context, messages)
        answer = f"{DEMO_LABEL}\n{answer}"
        if structured:
            return json.dumps({"answer": answer, "proposals": proposals}, ensure_ascii=False)
        return answer + "\n勾选“结合我的生活资料回答”后可演示生成草稿与确认保存。"

    def _member(self, prompt: str, context: dict, messages: Sequence[Mapping]) -> tuple[str, list]:
        now = beijing_now()
        if re.search(r"胸痛|呼吸困难|昏迷|喘不过气", prompt):
            return "如正出现这些紧急不适，请立即拨打 120 或请身边人协助。我不能诊断或代为呼叫。", []
        if re.search(r"药|诊断|治疗|处方|血糖|血压", prompt):
            return (
                "这里只整理日常生活，不能提供诊断、治疗、用药建议或用药提醒，请咨询医生或药师。",
                [],
            )
        if "天气" in prompt:
            return "未取得实时天气，请查看当地天气预报。", []
        if re.search(r"记得|偏好|不吃什么|总结|最近|今天喝了多少", prompt):
            facts = context.get("confirmed_facts", {})
            records = context.get("recent_life_records", [])
            text = "已确认偏好：" + str(facts.get("dietary_preferences") or "暂无记录")
            if records:
                text += "\n近期已保存记录：" + "；".join(str(r["content"]) for r in records[:5])
            else:
                text += "\n近期暂无生活记录；没记录不代表没做。"
            return text, []
        if re.fullmatch(r"(?:我)?(?:一直)?不吃[^，。！？?\n]{1,30}[。！]?", prompt):
            value = prompt.rstrip("。！")
            old = str(context.get("confirmed_facts", {}).get("dietary_preferences") or "")
            if value in old:
                return "已确认档案中已有这条偏好，不需要重复保存。", []
            return "这是一条饮食偏好，请在“AI 待确认”里核对保存。", [
                {
                    "type": "profile_fact",
                    "fact_key": "dietary_preferences",
                    "value": "；".join(filter(None, [old, value])),
                }
            ]
        # A narrow clarification example: the previous user explicitly requested a reminder.
        previous = [str(m.get("content", "")) for m in messages[1:-1] if m.get("role") == "user"]
        request = prompt
        if (
            re.fullmatch(r"(?:明天|今天)\s*\d{1,2}(?:点|:\d{2})(?:\d{1,2}分)?", prompt)
            and previous
            and re.fullmatch(r"提醒我[^，。！？?\n]{1,40}", previous[-1])
        ):
            request = prompt + previous[-1]
        if "提醒我" in request:
            match = re.fullmatch(
                r"(?:请)?(今天|明天)\s*(\d{1,2})(?:点(?:(\d{1,2})分)?|:(\d{2}))提醒我(.{1,40})",
                request,
            )
            if not match:
                return (
                    "请说明提醒时间，例如“明天18点提醒我散步”；演示暂只支持今天或明天的单次提醒。",
                    [],
                )
            day, hour, minute1, minute2, title = match.groups()
            hour, minute = int(hour), int(minute1 or minute2 or 0)
            if hour > 23 or minute > 59:
                return "这个时间无效，请使用 0—23 点和 0—59 分。", []
            scheduled = (now + timedelta(days=day == "明天")).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if scheduled <= now:
                return "这个时间已经过去，请换一个将来的时间。", []
            return "已整理为单次提醒草稿，确认保存后才会加入提醒列表。", [
                {
                    "type": "reminder",
                    "title": title.rstrip("。"),
                    "reminder_type": "custom",
                    "scheduled_at": scheduled.isoformat(),
                }
            ]
        sleep = re.search(
            r"入睡 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})，起床 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})",
            prompt,
        )
        if sleep:
            from ..services.device_observations import observation_to_proposal

            try:
                start, end = (
                    datetime.fromisoformat(value).replace(tzinfo=now.tzinfo)
                    for value in sleep.groups()
                )
                if end > now:
                    return "起床时间还没到，请核对实际时间。", []
                proposal = observation_to_proposal(
                    {
                        "schema_version": 1,
                        "kind": "sleep",
                        "source": "self_report",
                        "start_at": start.isoformat(),
                        "end_at": end.isoformat(),
                    }
                )
            except ValueError:
                return "请核对入睡和起床时间，区间需大于 0 且不超过 24 小时。", []
            return "已整理您自报的作息，请核对后保存；这不是设备检测结果。", [proposal]
        if re.fullmatch(r"(?:我)?(?:今天|现在|刚才)?(?:出去)?散步了[。！]?", prompt):
            return "已整理散步记录，未填写您没有提供的时长，请核对后保存。", [
                {
                    "type": "life_record",
                    "category": "activity",
                    "occurred_at": now.isoformat(),
                    "content": prompt,
                    "details": {"activity_type": "散步"},
                }
            ]
        category, details = None, {}
        if re.fullmatch(
            r"(?:我)?(?:刚才|刚刚|今天)?喝了\s*(\d{1,4})\s*(?:毫升|ml|ML)(?:水)?[。！]?", prompt
        ):
            amount = int(re.search(r"\d+", prompt).group())
            if 0 < amount <= 3000:
                category, details = "water", {"amount_ml": amount}
        elif re.fullmatch(
            r"(?:我)?今天(?:中午|早上|晚上|早餐|午餐|晚餐)吃了[^？?\n]{1,100}[。！]?", prompt
        ):
            category = "diet"
        elif re.fullmatch(r"(?:我)?(?:刚才|今天)?散步了?\s*\d{1,3}\s*分钟[。！]?", prompt):
            duration = int(re.search(r"\d+", prompt).group())
            if 0 < duration <= 240:
                category, details = "activity", {"duration_minutes": duration}
        elif re.fullmatch(r"(?:我)?昨晚睡了\d{1,2}小时[。！]?", prompt):
            category = "sleep"
        elif re.fullmatch(r"(?:现在)?室温\d{1,2}度[。！]?", prompt):
            category = "environment"
        if category:
            return "已整理生活记录草稿；具体发生时刻暂用当前时间，请核对后保存。", [
                {
                    "type": "life_record",
                    "category": category,
                    "occurred_at": now.isoformat(),
                    "content": prompt + "（具体时刻待确认）",
                    "details": details,
                }
            ]
        return (
            "演示只支持有限示例，未理解的内容不会写入档案。可以试试："
            "“刚才喝了300毫升水”“今天中午吃了面和两个鸡蛋”“我不吃香菜”"
            "“明天18点提醒我散步”或“总结最近记录”。真实自由对话需配置 DeepSeek Key。",
            [],
        )

    def _advisor(self, system: str) -> str:
        context = _snapshot(system, "会员数据：")
        service = _snapshot(system, "；服务数据：")
        now = beijing_now().date()
        records = context.get("recent_life_records", [])
        tasks = service.get("visit_tasks", [])
        visits = service.get("visit_records", [])
        requests = service.get("recent_member_requests", [])
        content = (
            f"{DEMO_LABEL}\n近期已保存生活记录："
            + ("；".join(str(r["content"]) for r in records[:5]) or "暂无记录")
            + "\n待处理服务事项："
            + (
                "；".join(str(t["title"]) for t in tasks if t.get("status") != "completed")
                or "暂无记录"
            )
            + "\n上次服务："
            + (str(visits[0]["summary"]) if visits else "暂无记录")
            + "\n近期主动表达（不代表已确认事实）："
            + ("；".join(str(r["content"]) for r in requests[:5]) or "暂无记录")
        )
        return json.dumps(
            {
                "answer": content,
                "proposals": [
                    {
                        "type": "advisor_summary",
                        "period_start": (now - timedelta(days=13)).isoformat(),
                        "period_end": now.isoformat(),
                        "content": content,
                    }
                ],
            },
            ensure_ascii=False,
        )
