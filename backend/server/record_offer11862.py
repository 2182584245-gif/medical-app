"""Identify a record offer and the user's separate reply without authorizing hidden writes."""
from __future__ import annotations

import re


OFFER_TEXT = '要把刚才说的情况整理成一张可核对的记录吗？'
DECLINED_TEXT = '好的。以后想记录时，直接告诉我就行。'
_REPORT_PREFIX = re.compile(r'我|本人|自己|今天|昨[天晚早]|前天|今[晚早]|中午|早上|刚(?:刚|才)?|昨天|本次|这次')
_REPORT_FACT = re.compile(
    r'(?:吃了|喝了|饮用了|睡了|只睡|走了|跑了|骑了|散步了|游了|锻炼了|运动了|服用了)'
    r'|(?:量了|测了|测到|测得)[^。？！?]{0,14}(?:血糖|血压|体重|身高|血脂)'
    r'|(?:血糖|血压|体重|身高|血脂)[^。？！?]{0,16}\d'
    r'|(?:去(?:了)?医院|做了(?:检查|复查|体检|抽血)|进行了(?:检查|复查|体检)|(?:检查|复查|体检)了)'
    r'|(?:房间|室内|家里)[^。？！?]{0,15}(?:温度|湿度)[^。？！?]{0,10}\d'
)
_NOT_REPORT = re.compile(
    r'假如|假设|如果|例如|比如|举例|示例|计划|打算|准备|想(?:去|喝|吃|睡|做|走|跑|测)|将要|'
    r'别人|朋友|(?:他|她|他们|她们|患者|病人|猫|狗)[^。？！?]{0,7}(?:刚|今天|吃了|喝了|睡了|量了|测了)|'
    r'(?:我[的家]?)(?:爸|妈|父亲|母亲|妻|老婆|丈夫|老公|孩子|儿子|女儿|哥哥|姐姐|弟弟|妹妹|猫|狗)|'
    r'没(?:有)?(?:吃|喝|睡|走|跑|锻炼|运动|测|量|做|服)|^\s*(?:查|查询|查看|查一下|看看|分析)[^。]{0,12}记录'
)
_CONSENT = re.compile(
    r'(?:(?:好(?:的|啊|呀|吧)?|可以(?:的|啊|呀|吧|哦|呢)?|行(?:啊|呀)?|同意|愿意|要|嗯(?:嗯)?)(?:[，,、 ]*'
    r'(?:那就|请|就|帮我)?(?:记一下|记下(?:来)?|记录(?:一下|下来)?|记吧|整理成记录)(?:吧|呀|啊)?)?'
    r'|(?:那就|请|就|帮我|麻烦你)?(?:记一下|记下(?:来)?|把它记下(?:来)?|记录(?:一下|下来)?|记吧|整理成记录)(?:吧|呀|啊)?'
    r'|(?:那就|就)?按(?:这个|你说的)记(?:录)?(?:吧|呀|啊)?)'
    r'[。！!，, ]*'
)
_REFUSAL = (r'(?:不用(?:记|记录|了)?|不要(?:记|记录|了)?|别(?:记|记录)|不(?:记|记录)|不需要(?:记录)?|'
            r'暂时不(?:记|记录)|先不(?:记|记录)|下次再(?:记|记录)|算了|不了)')
_DECLINE = re.compile(rf'{_REFUSAL}(?:[，, ]*谢谢)?[。！!，, ]*')
_DECLINE_AND_CHAT = re.compile(
    r'^(?:先|这次)?(?:不用记(?:录)?|不要记(?:录)?|别记(?:录)?|不记(?:录)?|不需要记录|算了)[，,；;。！!]')
_AFFIRM_DETAILS = re.compile(r'^(?:好(?:的|啊|呀)?|可以(?:的|啊|呀)?|行(?:啊|呀)?|同意|愿意|嗯(?:嗯)?)[，,。；;：: ]+(.+)$')
_SPECIFIC_FOLLOWUP = re.compile(
    r'^(?:(?:发生时间|时间|日期|时间点|备注|说明|数量|容量|时长|地点|餐次|温度|湿度|读数|数值|单位)'
    r'(?:是|为|填|写|记作|[：:])[^。！？!?]{1,160}|(?:上午|下午|晚上|中午|凌晨|早上)?'
    r'[零〇一二两三四五六七八九十0-9]{1,3}(?:点(?:半|[0-9一二两三四五六七八九十]{1,3}分)?|:[0-9]{2})(?:左右|前后|吧)?)'
    r'[。！! ]*$'
)
_OUTSIDE_DETAIL = re.compile(r'怎么|会不会|能不能|可以吗|可不可以|帮我查|建议|分析|是不是|吗(?:[，,。！! ]|$)')
_OFFER_WORDS = re.compile(r'(?:要不要|需要我|愿意|想不想|是否|要我|要把)[^。！？]{0,55}(?:记录|记下来|整理成)')
_DRAFT_CHAT = re.compile(r'换个话题|先聊聊|聊点别的|闲聊|讲(?:个|一个)笑话|今天天气|现在天气|谢谢(?:你|啦|啊)?[。！! ]*$')
_DRAFT_QUESTION = re.compile(r'[?？]|怎么|为什么|什么原因|帮我查|有什么建议|该怎么办|能不能建议')
_DRAFT_EXPLICIT = re.compile(
    r'备注|说明(?:是|写|填|改|[：:])|发生时间|日期|容量|热量|千卡|居住环境|时间是|餐次|地点|舒张压|收缩压|低压|高压|单位|数值|读数|'
    r'原图|附上|不是[^，。]{1,25}[，, ]+是|(?:改|换|调整|更正)[成到为]|补充|纠正|^(?:其实是|实际是|应为|刚才说错)'
)
_CLOCK_REPLY = re.compile(
    r'^(?:(?:对|是|就是|大概|差不多|约|应该是|那条是|这条是)[，,、 ]*)*'
    r'[昨今前明大年月日号天晚上早下午中凌晨午夜傍昏深夜半点分时周星期一二两三四五六七八九十零〇\d:：.\- ]+'
    r'(?:左右|前后|那条|这条|吧|呀|啊)?[。！!，, ]*$'
)
_CONFLICT_CHOICE = re.compile(r'(?:按|选|用|保留|采用|改用)?(?:原来(?:的)?|前(?:一)?个|后(?:一)?个|第一(?:个|种)|第二(?:个|种)|原值|新值|之前(?:的)?|后面(?:的)?|后来那个|前面那个)')
_REFUSED_FIELD = re.compile(r'^(?:我|这项|这条|暂时|先|那就)* *(?:不想说|不方便(?:说|提供)?|不知道|不记得|保密|跳过|忘了)[吧啦了。！!，, ]*$')
_BARE_NUMBER = re.compile(r'^(?:大概|差不多|实际是|是|约)?[零〇一二两三四五六七八九十百千万\d./ ]{1,24}[。！! ]*$')
_CATEGORY_ANSWER = {
    'diet': re.compile(r'面条|面包|拉面|泡面|汤面|蛋|豆|米|饭|菜|果|肉|鱼|鸡|鸭|虾|饺|粥|馒头|餐|酱|盐|油|辣|清淡|煮|蒸|炖|吃了|吃完'),
    'water': re.compile(r'毫升|公升|ml\b|mL\b|升|杯水|喝了|温水|白水|开水|矿泉水'),
    'activity': re.compile(r'散步|跑|走|骑|跳|游|锻炼|球|运动|登山|拉伸|瑜伽|分钟|小时|\d\s*步'),
    'sleep': re.compile(r'睡|醒|休息|失眠|小时|夜里|中途醒|打盹'),
    'environment': re.compile(r'窗|通风|温度|湿度|室温|有点冷|有点热|闷|光照|噪音|安静|房间|空调|除湿|加湿'),
    'medical': re.compile(r'医院|诊所|检查|体检|复查|看病|牙医|疫苗|抽血|验血|门诊'),
    'metric': re.compile(r'身高|体重|血糖|血脂|血压|空腹|餐前|餐后|mmhg|mmol|mg/dl|公斤|kg|厘米|cm', re.I),
}
_NOTES_ANSWER = {
    'diet': re.compile(r'少盐|少油|油大|清淡|偏咸|咸了|偏辣|微辣|半碗|没吃完|吃得很饱|(?:用|加了|汤)[^。]{0,24}(?:油|盐|生抽|清水)|(?:煮|蒸|炖)的'),
    'water': re.compile(r'^(?:温水|白水|开水|冰水|矿泉水)[。！! ]*$'),
}


def reports_health_fact(value):
    """Offer on the user's own bounded factual report, not general health discussion."""
    if not isinstance(value, str) or not 1 <= len(value) <= 1200:
        return False
    for sentence in re.split(r'[。；;！？?!\n]+', value):
        fact = _REPORT_FACT.search(sentence)
        if fact and _REPORT_PREFIX.search(sentence[:fact.end()]) and not _NOT_REPORT.search(sentence):
            return True
    return False


def accepts_offer(value):
    return isinstance(value, str) and bool(_CONSENT.fullmatch(value.strip()) or offer_details(value))


def offer_details(value):
    match = _AFFIRM_DETAILS.fullmatch(value.strip()) if isinstance(value, str) else None
    details = match[1].strip() if match else ''
    return details if _SPECIFIC_FOLLOWUP.fullmatch(details) and not _OUTSIDE_DETAIL.search(details) else ''


def declines_offer(value):
    return isinstance(value, str) and bool(only_declines_offer(value) or _DECLINE_AND_CHAT.search(value.strip()))


def only_declines_offer(value):
    return isinstance(value, str) and bool(_DECLINE.fullmatch(value.strip()))


def already_offers_record(value):
    return bool(_OFFER_WORDS.search(value))


def grounded_draft_reply(value, prior):
    """An open draft may accept its own clarification, but cannot authorize a new topic."""
    if not prior.get('drafts') or not value or _DRAFT_CHAT.search(value) or _DRAFT_QUESTION.search(value):
        return False
    if _DRAFT_EXPLICIT.search(value) or _REFUSED_FIELD.fullmatch(value):
        return True
    if any(draft.get('_draft_conflicts') for draft in prior['drafts']):
        if accepts_offer(value) or _CONFLICT_CHOICE.search(value):
            return True
        from .record_assistant_try12 import _explicit_time_parts, _resolve_fact_conflict, _TEMPORAL_FACTS
        spoken = _explicit_time_parts(value)
        return any(_resolve_fact_conflict(item, value, spoken)
                   for draft in prior['drafts'] for item in draft.get('_draft_conflicts', [])
                   if (_BARE_NUMBER.fullmatch(value) or _CATEGORY_ANSWER[draft['category']].search(value)
                       or item['field'] in _TEMPORAL_FACTS and _CLOCK_REPLY.fullmatch(value)))
    for draft in prior['drafts']:
        required = {item['field'] for item in draft['missing_required']}
        optional = {item['field'] for item in draft['missing_optional']}
        category = draft['category']
        if (({'record_date', 'record_time', 'occurred_at', 'target'}.intersection(required)
                or prior.get('ready_followup')) and _CLOCK_REPLY.fullmatch(value)):
            return True
        if 'target' in required and re.search(r'(?:那|这)条|今天|昨天|前天', value):
            return True
        if ('record_time' in required or 'occurred_at' in required) and (
                re.search(r'(?:估|推算)(?:个大概|一下)|按(?:上午|下午|晚上|早上|中午)', value)):
            return True
        if required or optional - {'notes'}:
            if _CATEGORY_ANSWER[category].search(value):
                return True
        if 'notes' in optional and category in _NOTES_ANSWER and _NOTES_ANSWER[category].search(value):
            return True
        if _BARE_NUMBER.fullmatch(value) and (
                required.intersection({'amount_ml', 'value', 'diastolic', 'duration_hours'})
                or optional.intersection({'calories_kcal', 'duration_minutes', 'steps'})
                or re.match(r'实际是|本来是|应为', value)):
            return True
    return False
