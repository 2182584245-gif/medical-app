"""Public chat may acknowledge a fact, but only the commit path can acknowledge a write."""
from __future__ import annotations

import json
import re
from uuid import uuid5


_DECORATION = r'(?P<lead>\s*(?:[-*#✓✔✅\ufe0f]\s*)*)'
_VERB = r'(?:保存|记录|记下(?:来)?|记好|记上|记入|记进|记到|录入|存下(?:来)?|存好|添加|创建|设置|设好|修改|更新|更正|补充|补上|补齐)'
_DONE = r'(?:下来|好了|完成了?|成功了?|好|了|啦|喽)'
_FACT = r'(?:这(?:个|条|笔|次|份)(?:[\w]{0,8}?(?:记录|信息|数据|情况|饮水|早餐|内容))?|(?:这|该)条记录|记录|数据)'
_CLAIM = re.compile(_DECORATION + r'(?:'
    r'(?:我|这边|系统|助手|凡思)?(?:已经|现已|已|刚刚|刚才)(?:成功)?(?:帮你|为你|给你|替你)?(?:(?:把|将)[^，。！？；：\n]{1,35}?)?' + _VERB + r'(?:' + _DONE + r')?'
    r'|(?:我(?:这边)?(?:也|先)?(?:帮你|为你|给你|替你)?|帮你|为你|给你)(?:把[^，。！？；：\n]{1,35}?)?' + _VERB + _DONE +
    r'|' + _FACT + r'(?:我)?(?:(?:已经|现已|已)(?:帮你|为你|给你)?' + _VERB + r'(?:' + _DONE + r')?|(?:先|都|也|成功)*(?:帮你|为你|给你)?' + _VERB + _DONE + r')'
    r'|' + _VERB + r'(?:成功[了啦]?|完成[了啦]?|好了|啦)|(?:已)?(?:为你|帮你|给你)' + _VERB + r'(?:在|到|进|为|成)'
    r')')
_HISTORICAL_NOUN = re.compile(r'^(?:的|过的|过吗|过么|了吗|了么|吗|么|后|前|时|则|的话|方可|才|才能)')
# Informational/quoted wording is not an assertion by the speaking assistant.
_EXPLANATION = re.compile(r'^(?:[-*#\s>]*)(?:例如|比如|举例|示例|例句|假如|假设|如果|提示语|文案|历史消息|引用|原话|解释|“|「|『|\x22|`)')
_BOUNDARY = re.compile(r'[。！？!?\n]')
_CLAUSE = re.compile(r'[^，,；;：:。！？!?\n]+')
_QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|`[^`\n]*`')
_EMBEDDED_ACTOR = re.compile(r'我(?=已经|现已|已|刚刚|刚才|先|替你|帮你|给你|为你|记下|记好|保存了|记录了|修改了|补充了)')
_ATTRIBUTION = re.compile(r'(?:你说|原文是|例如|假设|假如|如果|比如|举例|以前|过去|之前|昨天|上周|上次|去年|当时)[^，。！？]{0,36}$')
_TEXT_OBJECT = re.compile(r'这句话|这段话|句子|句式|措辞|段落|原文|文案|文章|标点|语法|标题')
_TEXT_TASK = re.compile(r'改通顺|改写|重写|润色|校对|改得|修改|补充|调整|更正|纠正|改成|改为')
_TEXT_EDIT = re.compile(r'修改|更新|补充|更正|补齐|补上|设置')
_RECORD_TARGET = re.compile(r'记录|草稿|档案|提醒|目标|录入|存入')

# A status query is not an instruction to create another draft. Real conditional
# instructions such as "没记的话请帮我记一下" remain ordinary intent routing.
_STATUS = re.compile(r'(?:有没(?:有)?|是否|是不是)[^，。！？；;\n]{0,20}?(?:存(?:好|上|下)?|保存|记入|记进|记录|记下|记上|记好|录入|改好|改了|修改|补上|补充)'
                     r'|(?:保存|存|记进|记入|记到|记上|记下|记好|记录|录入|修改|改好|补充|补上)[^，。！？；;\n]{0,15}?(?:了吗|了么|了没|了没有|成功没|好没|了嘛)')
_WRITE_AFTER = re.compile(r'(?:如果|要是|若|的话)[^。！？]{0,25}(?:请|就|再)(?:帮我|给我)?(?:保存|记|存|补|改)|[，,。；;]\s*(?:那就|请|帮我|麻烦)(?:帮我|给我)?(?:记|保存|补|改)')
# Only a bare reference to this/last operation can be decided by its receipt.
# A named meal, date, another person's activity, or another feature requires contextual lookup.
_FAST_STATUS = re.compile(r'[你帮我请问到底已经刚才方才想知道这条记录那个件事笔次一份上一轮对话消息说的确定核实一下还保存了好没有成功吗么嘛下修改补充更新上在进到入里过否是图片照片原？?。！!，,\s]{1,120}')
_CATEGORIES = {'饮水':'water','喝水':'water','饮食':'diet','餐食':'diet','睡眠':'sleep','运动':'activity','活动':'activity',
               '环境':'environment','就医':'medical','就诊':'medical','指标':'metric'}
_CATEGORY = re.compile('|'.join(_CATEGORIES))
_MEAL_WORDS = re.compile(r'早餐|早饭|午餐|午饭|晚餐|晚饭|餐食|饮食')
_SHARE_TOPIC = (
    ('diet', _MEAL_WORDS), ('water', re.compile(r'喝[^。！？]{0,18}水|饮水|毫升温水|毫升白水')),
    ('sleep', re.compile(r'睡着|睡了|睡眠')), ('activity', re.compile(r'运动|散步|跑步|锻炼')),
    ('environment', re.compile(r'室温|室内温度|室内湿度')), ('medical', re.compile(r'就诊|就医|体检|复查')),
    ('metric', re.compile(r'体重|血压|血糖|心率')))
_REPORT_EXCLUDE = re.compile(r'[？?]|请|帮我|怎么|如何|想知道|举例|例如|假如|引号|查询|分析|对比|之前的记录|上次的记录')
_CHAT_SOURCE = re.compile(r'(?:你|您)[^。！？；;（）()]{0,22}(?:提到过|提过|说过|聊过|分享过|曾提到|曾说过)|(?:此前|之前|以前|上次)的?(?:聊天|对话)(?:中|里)')
_NUM = r'[0-9零〇一二两三四五六七八九十]+'
_NAMED_RECORD = r'(?:正式|已保存的?)?(?:生活|健康|饮食|餐食|早餐|饮水|睡眠|运动|活动|就医|指标|环境)?记录'
_HISTORY_COUNT = re.compile(r'(?P<first>'+_NUM+r')[次条笔份](?:的)?'+_NAMED_RECORD+
    r'(?=都|分[别为]|均|显示|中|里|是|为|[。！；;，,）)\s]|$)|(?<!聊天)(?<!对话)'+_NAMED_RECORD+
    r'(?:里|中)?(?:也|一共|总共|共有|有|共|为|是|一共是|一共有){1,3}(?P<second>'+_NUM+r')[条次笔份]')
_ASIDE = re.compile(r'[（(]([^（）()]{1,190})[）)]')


def share_category(text):
    if not text or len(text)>400 or _REPORT_EXCLUDE.search(text):
        return None
    found = [category for category, pattern in _SHARE_TOPIC if pattern.search(text)]
    return found[0] if len(found)==1 else None


def formal_record_capacity(tools, principal, meta, category):
    """A bounded *upper bound* disproves impossible counts. Never treats chat as rows."""
    if not meta.get('personal_context') or tools is None or not getattr(tools, 'memory', None):
        return 0
    actor = tools._member(principal)
    with tools.auth.identity(principal):
        settings = tools.memory.settings(actor)
        controls = {row['domain']:row for row in settings.get('controls', [])}
        # An inaccessible or forgotten archive is never used to justify a claim.
        control = controls.get('life_records', {})
        if not settings.get('enabled') or not control.get('enabled_for_ai') or control.get('forget_before'):
            return 0
        with tools.db.connect() as conn:
            if category == 'metric':
                rows = conn.execute('SELECT id FROM expansion_observations WHERE user_id=? LIMIT 21',(actor,)).fetchall()
            else:
                rows = conn.execute('SELECT id FROM life_records WHERE user_id=? AND category=? LIMIT 21',(actor,category)).fetchall()
    return len(rows) if len(rows)<=20 else None


def _counter(match):
    text = match['first'] or match['second']
    if text.isascii():
        return int(text[:4])
    digits = dict(zip('零〇一二两三四五六七八九', (0,0,1,2,2,3,4,5,6,7,8,9)))
    if '十' in text:
        tens, _, ones = text.partition('十')
        return digits.get(tens,1)*10 + digits.get(ones,0)
    return digits.get(text, 0)


def _history_wording(value, masked, category, cap):
    """Remove a conflated or impossible count, without converting it into a chat count."""
    if not _HISTORY_COUNT.search(masked):
        return value
    # A formal record count in parentheses immediately following "you mentioned"
    # cannot be inferred from the mentions. Independently named sources are separate.
    keep = list(value)
    for aside in _ASIDE.finditer(masked):
        match = _HISTORY_COUNT.search(aside[1])
        if not match or not _CHAT_SOURCE.search(masked[:aside.start()]):
            continue
        extra_facts = re.search(r'都|分别|均|同样|相同|\d{4}年|\d{1,2}月\d{1,2}', aside[1])
        if (not extra_facts and re.search(r'正式|记录页|已保存',aside[1])
                and (cap is None or _counter(match)<=cap)):
            continue
        keep[aside.start():aside.end()]=['']*(aside.end()-aside.start())
    clean = ''.join(keep)
    if category is None or cap is None:
        return clean
    # This only affects unsolicited counts about the shared category. User-quoted
    # text, other domains and explicit requests for history use their usual paths.
    parts=[]
    for clause in re.finditer(r'[^，,；;。！？!?\n]+[，,；;。！？!?\n]*|[，,；;。！？!?\n]+', clean):
        words=clause.group()
        safe=_QUOTED.sub('',words)
        count=_HISTORY_COUNT.search(safe)
        named={key for key,pat in _SHARE_TOPIC if pat.search(safe)}
        if (count and _counter(count)>cap and (not named or named=={category})
                and re.search(r'曾|之前|以前|此前|上次|历史|正式|已保存|记录页|你的.{0,10}记录|\d{4}年',safe)
                and not re.search(r'聊天记录|对话记录|例如|比如|如果|没有|不足|未(?:满|到)|尚无',safe)):
            continue
        parts.append(words)
    return ''.join(parts) or '收到你这次分享的情况。'


def status_query(text):
    if not isinstance(text, str) or not 1 <= len(text) <= 400 or _EXPLANATION.search(text):
        return False
    visible = _QUOTED.sub('', text)
    return bool(_STATUS.search(visible) and not _WRITE_AFTER.search(visible))


def immediate_status(text):
    return status_query(text) and bool(_FAST_STATUS.fullmatch(_CATEGORY.sub('', text)))


def _applied(conn, actor, draft, *, photo=False):
    from .mobile_sync149 import ACTION_RESOURCE, NAMESPACE
    identifier = draft['id']
    create = draft['operation'] == 'create'
    metric = draft['category'] == 'metric'
    target = draft.get('target') or {}
    linked = target.get('linked_meal')
    if linked and not photo and not create and draft['category'] == 'diet':
        row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_id=?',
                           (actor, identifier)).fetchone()
        if row:
            result = json.loads(row['response_json']).get('result')
            if (isinstance(result, dict) and result.get('id') == linked.get('id') == target.get('linked_meal_id')
                    and result.get('life_record_id') == target.get('id') and result.get('user_id') == actor
                    and isinstance(result.get('version'), str) and result['version'] and result['version'] != linked.get('version')):
                return True
    action = ('expansion.observations.' + ('create' if create else 'update') if metric else
              'health.records.add' if create else 'expansion.records.update')
    mobile_id = str(uuid5(NAMESPACE, f'{actor}:{identifier}'))
    row = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_id=?',
                       (actor, mobile_id)).fetchone()
    if not row:
        return False
    receipt = json.loads(row['response_json']).get('mobile149', {})
    if (receipt.get('operation_id'), receipt.get('action'), receipt.get('resource'), receipt.get('status')) != (
            identifier, action, ACTION_RESOURCE[action], 'applied'):
        return False
    # Large successful receipts may omit the entity. Only a matching immutable operation
    # can enter this branch. Ordinary receipts additionally bind category/owner/target.
    entity = receipt.get('result_entity')
    if not isinstance(entity, dict):
        return (not photo and receipt.get('result_available_in_state') is True
                and receipt.get('result_resource') == ACTION_RESOURCE[action])
    if photo:
        selected = draft.get('photo_file_ids') or []
        saved = (entity.get('details') or {}).get('photo_file_ids') or []
        if not selected or not all(i in saved for i in selected):
            return False
    return (type(entity.get('id')) is int and entity['id'] > 0 and entity.get('user_id') == actor
            and (metric or entity.get('category') == draft['category']) and (create or target.get('id') == entity['id']))


def last_record_status(tools, principal, meta, question):
    """Read only the immediately referenced owned turn, skipping repeated status questions."""
    actor = tools._member(principal)
    with tools.auth.identity(principal), tools.db.connect() as conn:
        rows = conn.execute("""
            SELECT a.id, substr(u.content,1,401) AS question FROM conversations c
            JOIN messages current_message ON current_message.conversation_id=c.id
            JOIN messages a ON a.conversation_id=c.id AND a.role='assistant' AND a.sequence_no<current_message.sequence_no
            JOIN messages u ON u.conversation_id=c.id AND u.role='user' AND u.sequence_no=a.sequence_no-1
            WHERE c.user_id=? AND c.id=? AND current_message.id=? AND current_message.role='assistant'
            ORDER BY a.sequence_no DESC LIMIT 8
        """, (actor, meta['conversation_id'], meta['assistant_message_id'])).fetchall()
        prior = next((row for row in rows if not immediate_status(row['question'])), None)
        run = tools._read(conn, actor, prior['id']) if prior else None
        if run and run.get('status') == 'complete':
            drafts = [d for d in run.get('record_drafts', ()) if d.get('operation') in {'create','update','supplement'}]
            proposals = [p for p in run.get('proposals', ()) if p.get('kind') in {'life_record','observation'}]
            if len(drafts) + len(proposals) > 1:
                return '刚才涉及多条记录。你想核对哪一条？可以在记录列表里查看各条保存结果。'
            if drafts:
                d = drafts[0]
                named_categories = {_CATEGORIES[word] for word in _CATEGORY.findall(question)}
                if named_categories and named_categories != {d['category']}:
                    return '刚才核对的草稿与询问的类别不一致。请在对应的记录列表核对保存结果。'
                if {'id':d['id'], 'status':'cancelled'} in run.get('record_receipts', []):
                    return '刚才的记录草稿已取消，没有保存。'
                if re.search(r'图片|照片|原图', question):
                    if _applied(conn, actor, d, photo=True):
                        return '刚才确认的记录已保存，原草稿中的照片也保存在这条记录里。'
                    return '目前没有查到原草稿的照片已保存在正式记录中的回执。'
                if _applied(conn, actor, d):
                    return {'create':'刚才确认的这条记录已保存。','update':'刚才确认的修改已保存到原记录。','supplement':'刚才确认的补充已保存到原记录。'}[d['operation']]
                return '刚才生成过核对草稿，目前没有查到这条草稿的成功保存回执。'
            if proposals:
                proposal = proposals[0]
                if _CATEGORY.search(question) or re.search(r'图片|照片|原图', question):
                    return '请在对应的记录列表核对这条记录的保存结果。'
                if proposal.get('status') == 'saved' and tools._confirmed_receipt(conn, actor, proposal)['persistence_verified']:
                    return '刚才确认的这条记录已保存。'
                if proposal.get('status') == 'cancelled':
                    return '刚才的记录操作已取消，没有保存。'
    return '没有查到刚才这轮对话成功保存记录的回执。'


def conversation_draft_state(tools, principal, meta):
    """Use the same account, conversation and open-task predicate as the mobile list."""
    from .record_assistant_try12 import CATEGORIES, draft_replacement_marker
    from .record_tasks11864 import RecordTasks

    repo = RecordTasks(tools)
    actor, conversation = tools._member(principal), meta['conversation_id']
    with tools.auth.identity(principal), tools.db.connect() as conn:
        repo._check_chat(conn, actor, conversation)
        open_where, open_args = repo._query(actor, conversation)
        total = conn.execute('SELECT COUNT(*) AS total' + open_where, open_args).fetchone()['total']
        where, args = repo._query(actor, conversation, opened=False)
        recent = conn.execute('SELECT payload' + where + ' ORDER BY stamp DESC LIMIT 4', args).fetchall()
        states = []
        for row in recent:
            task = json.loads(row['payload'])
            identifier = task['draft_id']
            if total and repo._get(conn, actor, conversation, task['id']):
                label = {'conflict':'有冲突待核实', 'needs_input':'待补资料'}.get(task['status'], '待核对')
            else:
                marker = conn.execute('SELECT response_json FROM rpc_requests WHERE user_id=? AND request_id=?',
                                      (actor, draft_replacement_marker(actor, identifier))).fetchone()
                if marker and json.loads(marker['response_json']).get('cancelled_record_draft') == identifier:
                    label = '已取消，未保存'
                else:
                    run = tools._read(conn, actor, task['assistant_message_id'])
                    draft = next((d for d in (run or {}).get('record_drafts', []) if d['id'] == identifier), None)
                    if not draft or not _applied(conn, actor, draft):
                        continue  # A replacement, a client decision or an error is not a successful save.
                    label = '已确认保存'
            # Only include controlled labels. Never put free text, notes or private attachment data in a system message.
            state = {'状态': label}
            if meta.get('personal_context') and task.get('category') in CATEGORIES:
                state['类别'] = CATEGORIES[task['category']]
                if re.fullmatch(r'[0-2]\d:[0-5]\d', task.get('record_time') or ''):
                    state['时间'] = task['record_time']
            states.append(state)
    return {'待处理总数': total, '最近状态（新到旧）': states}


# A pending-state claim talks about an actual card. Do not touch quoted speech,
# instructions about the interface or negative assertions such as "草稿不用核对".
_PENDING_CLAIM = re.compile(
    r'(?:草稿|核对卡)[^。！？!?\n，,；;]{0,16}?(?:还在等|还等着|正等着|等着|正在等|等待|仍(?:然)?待|还(?:在|需|需要|待)|仍(?:需|在)|待)'
    r'(?:你|您)?(?:来|去|再|点击|点)?(?:核对|确认|处理|补充|补全|补齐)'
    r'|(?:还有|仍有|还留着|尚有)[^。！？!?\n，,；;]{0,18}(?:待核对|待确认|待处理)的?(?:记录|草稿|核对卡)')
_PENDING_ASK = re.compile(r'(?:要不要|是否|要|想|需要|可以|记得|别忘了|请)[^。！？!?\n]{0,18}?(?:核对|确认|处理|补充)[^。！？!?\n]{0,15}?(?:草稿|核对卡)')
_DEFER_ASIDE = re.compile(r'^\s*(?:如果|要是)?(?:你|您)?(?:想|要|愿意|可以)?(?:等会儿?|稍后|待会儿?|晚点|下次|以后)再(?:处理|确认|核对)[，,\s]*(?:也|都)?(?:完全)?(?:没问题|可以|不着急)?[。！!\s]*$')
_DRAFT_QUERY = re.compile(r'(?:草稿|核对卡)[^。！？!?\n]{0,18}(?:吗|呢|怎么办|什么状态|什么情况|怎么处理|还有没有)'
                          r'|(?:还有没有|有没有|还有几|还有哪|查看|看看|还有多少|怎么处理)[^。！？!?\n]{0,18}(?:草稿|核对卡)')


def declines_draft_nudges(text):
    from .record_assistant_try12 import declines_whole_record
    return declines_whole_record(text) and not _DRAFT_QUERY.search(_QUOTED.sub('', text))


class PersistenceTextGuard:
    """Buffer complete clauses so chunk boundaries cannot expose half a status assertion."""
    def __init__(self, pending, user_text=''):
        self.pending = pending
        self.buffer = ''
        self.quote = None
        self.line_start = True
        self.quoted_line = False
        request = _QUOTED.sub('', user_text)
        self.editing_text = bool(_TEXT_TASK.search(request) and _TEXT_OBJECT.search(request)
                                 and not _RECORD_TARGET.search(request))
        self.share_topic = None
        self.formal_cap = None
        self.open_drafts = None
        self.no_record_nudge = False
        self.removed_nudge = False
        self.has_removed_nudge = False

    def _sentence(self, value):
        # Keep quote state across streamed sentence boundaries (including multi-sentence examples).
        if self.line_start:
            self.quoted_line = value.lstrip().startswith('>')
        quoted_line = self.quoted_line
        self.line_start = value.endswith('\n')
        masked = list(value)
        endings = {'“':'”', '「':'」', '『':'』', '"':'"', '`':'`'}
        for index, char in enumerate(value):
            if self.quote:
                masked[index] = '□'
                if char == self.quote:
                    self.quote = None
            elif char in endings:
                self.quote = endings[char]
                masked[index] = '□'
        visible = ''.join(masked)
        if self.removed_nudge and visible.strip():
            self.removed_nudge = False
            if _DEFER_ASIDE.fullmatch(visible):
                return ''
        if (not quoted_line and not self.editing_text and not _EXPLANATION.search(value)
                and (self.no_record_nudge or self.open_drafts == 0 and not self.pending())):
            claim = _PENDING_CLAIM.search(visible) or (self.no_record_nudge and _PENDING_ASK.search(visible))
            if (claim and not re.search(r'没有|并非|不再|不需要|无需|不用|已取消', visible[max(0,claim.start()-8):claim.end()])
                    and not re.search(r'当时|那时|以前|过去|之前(?:是|曾)|[你您](?:说|问)|曾经|一度|(?:文章|作文|设计|邮件|文案|小说|论文)草稿', visible)):
                self.removed_nudge = True
                self.has_removed_nudge = True
                # Preserve an independent acknowledgement before the reminder.
                # An introductory time/subject fragment belongs to the reminder.
                prefix = visible[:claim.start()]
                parts = list(re.finditer(r'[^，,；;]+[，,；;]', prefix))
                while parts and re.search(r'对了|顺便|另外|提醒|至于|关于|那(?:条|份|杯|张|次)|草稿|核对卡',
                                          parts[-1].group()):
                    parts.pop()
                return value[:parts[-1].end()-1] + '。' if parts else ''
        if quoted_line or _EXPLANATION.search(value) or value.rstrip().endswith(('?', '？')):
            return value
        if self.share_topic:
            cleaned = _history_wording(value, ''.join(masked), self.share_topic, self.formal_cap)
            if cleaned != value:
                # Rebuild the quote mask for the surviving clauses at their new offsets.
                value = cleaned
                masked = list(_QUOTED.sub(lambda match:'□'*len(match[0]), value))
        result = []
        cursor = 0
        for part in _CLAUSE.finditer(''.join(masked)):
            clause = part.group()
            match = _CLAIM.match(clause)
            if not match:
                for actor in _EMBEDDED_ACTOR.finditer(clause):
                    if not _ATTRIBUTION.search(clause[:actor.start()]):
                        match = _CLAIM.match(clause, actor.start())
                        if match:
                            break
            if not match or _HISTORICAL_NOUN.match(clause[match.end():]):
                continue
            if (_TEXT_EDIT.search(match.group()) and not _RECORD_TARGET.search(clause)
                    and (self.editing_text or _TEXT_OBJECT.search(clause))):
                continue
            # "已记录的数据" / "已经记录过的早餐" describe prior material.
            # A leading completion verb can also precede a supplied object.
            result.append(value[cursor:part.start() + match.start()] + match['lead'])
            result.append('草稿待核对，确认后才会保存' if self.pending() else '这次尚未保存记录')
            cursor = part.end()
        return ''.join(result) + value[cursor:]

    def feed(self, value):
        self.buffer += value
        output = []
        while self.buffer:
            boundary = _BOUNDARY.search(self.buffer)
            if boundary and boundary.end() <= 512:
                end = boundary.end()
            elif len(self.buffer) >= 512:
                # Prefer a clause boundary to splitting a word or a save claim.
                cut = max(self.buffer.rfind(mark, 0, 512) for mark in '，,；;：:')
                end = cut + 1 if cut >= 128 else 384
                if cut < 128:
                    # The 128-character lookahead includes any bounded subject/verb
                    # expression. Never publish a prefix of a match at the soft cut.
                    for start in range(end - 96, end):
                        claim = (_CLAIM.match(self.buffer, start) or _PENDING_CLAIM.match(self.buffer, start)
                                 or (self.no_record_nudge and _PENDING_ASK.match(self.buffer, start)))
                        if claim and claim.end() >= end:
                            end = start
                            break
            else:
                break
            output.append(self._sentence(self.buffer[:end]))
            self.buffer = self.buffer[end:]
        return ''.join(output)

    def finish(self):
        text = self._sentence(self.buffer)
        self.buffer = ''
        return text
