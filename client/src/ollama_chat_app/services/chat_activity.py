"""Bounded public operation receipts, never model reasoning or source contents.

Only application-observed steps belong here. Callers must derive statuses from
actual requests/results; this validator does not treat model claims as evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re

VERSION = 1
MAX_ACTIVITY_BYTES = 12 * 1024
KINDS = frozenset({'welcome_cleanup','personal_records','knowledge_cards','language_cards','web_search','controlled_tools','answer'})
LABELS = {
    'welcome_cleanup': {'working':'正在清除无用消息......','done':'已整理未回应的欢迎语','skipped':'没有需要整理的欢迎语','failed':'欢迎消息整理未完成'},
    'personal_records': {'done':'查询了个人记录','skipped':'未使用个人记录','failed':'个人记录暂不可用'},
    'knowledge_cards': {'done':'检索了知识资料卡','skipped':'未使用知识资料卡','failed':'知识资料卡暂不可用'},
    'language_cards': {'done':'检索了语言资料卡','skipped':'未使用语言资料卡','failed':'语言资料卡暂不可用'},
    'web_search': {'skipped':'未搜索网页','done':'搜索了公开网页','failed':'网页搜索未完成'},
    'controlled_tools': {'done':'调用了应用工具','failed':'应用工具未完成','skipped':'未调用应用工具'},
    'answer': {'done':'生成了回答','failed':'回答未完整生成'},
}
DOMAIN_LABELS = frozenset({'用户个人资料','个人资料','生活记录','提醒','目标与习惯','服务与预约',
    '用户阅读','阅读记录','用户购物','购物记录','用户论坛','社区记录','社区交流','用户操作记录','操作记录','应用操作',
    '用户偏好','长期偏好','用户AI记录','用户智能对话记录','AI 对话记录','智能对话记录','历史对话'})
DETAILS = frozenset({
    '本次没有需要折叠的欢迎语。','欢迎消息整理未完成。',
    '已关闭个人记录参考。','个人记录参考已关闭。','个人记录暂不可用。',
    '未取得可用的个人记录。','没有取得可参考的个人记录。','仅使用本轮提供的信息。',
    '已关闭资料卡参考。','资料卡参考已关闭。','未找到相关资料卡。','资料卡暂不可用。',
    '没有可用的语言资料卡。','没有可用的知识资料卡。',
    '检索了资料目录。','本次没有可参考的个人记录。',
    '本次未调用网页搜索。','本次没有进行网页搜索。','当前未启用网页搜索。',
    '搜索了公开网页。','未取得可用的公开网页。','网页搜索暂不可用。',
    '查询了本人授权的资料。','已生成待确认操作，尚未保存。','应用工具未完成。','本次未调用应用工具。',
    '回答已整理并保存。','回答未完整生成。','依据可用资料整理回答。',
    '本次由主助手直接处理。','本次协作记录未完成。',
    '已整理记录草稿，需核对后自行保存。','分析了本人授权记录中的变化。','整理了待本人选择的日常生活建议。',
})
COLLABORATION_ROLES={'organize_record_drafts':'记录整理','analyze_record_trends':'趋势分析','plan_daily_life':'生活规划'}
COLLABORATION_STATES={'working':'处理中','done':'完成','failed':'未完成'}


class ActivityValidationError(ValueError):
    def __init__(self):
        super().__init__('操作记录格式无效。')


def collaboration_detail(rows, *, incomplete=False):
    """Fixed public role/status summary, only for application-observed calls."""
    if type(rows) is not list or len(rows)>2 or type(incomplete) is not bool:
        raise ActivityValidationError()
    if not rows:return '本次协作记录未完成。' if incomplete else '本次由主助手直接处理。'
    seen=set();parts=[]
    for row in rows:
        if type(row) is not dict or set(row)!={'tool','status'}:
            raise ActivityValidationError()
        tool,status=row['tool'],row['status']
        if type(tool) is not str or tool not in COLLABORATION_ROLES or tool in seen or type(status) is not str or status not in COLLABORATION_STATES:
            raise ActivityValidationError()
        seen.add(tool);parts.append(COLLABORATION_ROLES[tool]+'（'+COLLABORATION_STATES[status]+'）')
    return '本次协作：'+'、'.join(parts)+'。'


def _collaboration_summary(value):
    match=re.fullmatch(r'本次协作：(.+)。',value)
    if not match:return False
    roles={name:tool for tool,name in COLLABORATION_ROLES.items()}
    states={name:status for status,name in COLLABORATION_STATES.items()}
    rows=[]
    for part in match[1].split('、'):
        item=re.fullmatch(r'(记录整理|趋势分析|生活规划)（(处理中|完成|未完成)）',part)
        if not item:return False
        rows.append({'tool':roles[item[1]],'status':states[item[2]]})
    try:return collaboration_detail(rows)==value
    except ActivityValidationError:return False


def _timestamp(value):
    if not isinstance(value,str) or len(value)>40 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)',value):
        raise ActivityValidationError()
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ActivityValidationError() from None
    return result


def _detail(value):
    """Allow generic public summaries, dates and category labels only."""
    if not isinstance(value,str) or not 1<=len(value)<=300 or value!=value.strip():
        raise ActivityValidationError()
    if value in DETAILS:
        return value
    if _collaboration_summary(value):return value
    if re.fullmatch(r'已折叠[0-9]{1,6}条未回应的欢迎语。',value):return value
    if re.fullmatch(r'(?:实际参考了|实际使用了|已打开|已参考|参考了|使用了)[0-9]{1,3}份(?:知识|语言)?资料卡。',value):
        return value
    if re.fullmatch(r'(?:读取了|参考了|查询了)[0-9]{1,5}条(?:已保存的)?记录。',value):
        return value
    match=re.fullmatch(r'(?:参考了|查询范围：|参考范围：)(.+)。',value)
    if match and len(match[1].split('、'))<=11 and all(part in DOMAIN_LABELS for part in match[1].split('、')):
        return value
    match=re.fullmatch(r'(?:资料日期：|记录日期：|资料范围：)(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})。',value)
    if match:
        try:
            start,end=(datetime.strptime(part,'%Y-%m-%d') for part in match.groups())
        except ValueError:
            raise ActivityValidationError() from None
        if start<=end:
            return value
    raise ActivityValidationError()


def validate_activity(value):
    if type(value) is not dict or set(value)!={'version','status','started_at','finished_at','steps'}:
        raise ActivityValidationError()
    if type(value['version']) is not int or value['version']!=VERSION or type(value['status']) is not str or value['status'] not in {'complete','error'}:
        raise ActivityValidationError()
    started,finished=(_timestamp(value[key]) for key in ('started_at','finished_at'))
    if finished<started or finished-started>timedelta(minutes=130):
        raise ActivityValidationError()
    raw_steps=value['steps']
    if type(raw_steps) is not list or not 1<=len(raw_steps)<=12:
        raise ActivityValidationError()
    steps=[]
    for row in raw_steps:
        if type(row) is not dict or not {'kind','status','label'}<=set(row) or set(row)-{'kind','status','label','detail','at'}:
            raise ActivityValidationError()
        kind,status=row['kind'],row['status']
        if type(kind) is not str or type(status) is not str or status=='working' or kind not in KINDS or status not in LABELS[kind] or row['label']!=LABELS[kind][status]:
            raise ActivityValidationError()
        item={'kind':kind,'status':status,'label':row['label']}
        if 'detail' in row:
            item['detail']=_detail(row['detail'])
        if 'at' in row:
            at=_timestamp(row['at'])
            if not started<=at<=finished:
                raise ActivityValidationError()
            item['at']=at.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        steps.append(item)
    expected='done' if value['status']=='complete' else 'failed'
    if not any(row['kind']=='answer' and row['status']==expected for row in steps):
        raise ActivityValidationError()
    if any(row['kind']=='answer' and row['status']!=expected for row in steps):
        raise ActivityValidationError()
    result={'version':VERSION,'status':value['status'],
            'started_at':started.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'),
            'finished_at':finished.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z'),
            'steps':steps}
    if len(activity_json(result).encode('utf8'))>MAX_ACTIVITY_BYTES:
        raise ActivityValidationError()
    return result


def activity_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


__all__=['ActivityValidationError','LABELS','DETAILS','DOMAIN_LABELS','validate_activity','activity_json',
         'COLLABORATION_ROLES','COLLABORATION_STATES','collaboration_detail']
