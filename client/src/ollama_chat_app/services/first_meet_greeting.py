"""Real persisted welcome turns and lossless folding of unanswered openings."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime
from uuid import UUID
from ..data.database import message_from_row, timestamp_to_db, utc_now

WELCOME_PROVIDER = 'fawnsee-welcome'
BOOT_MODEL_PREFIX = 'fawnsee-boot:'
SUFFIX = '想聊饮食、睡眠，或者让我帮你整理记录，直接说就行。'


def opening_text(name: str, hour: int) -> str:
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError('请使用有效的本机时间。')
    name = str(name or '您好').strip()[:40] or '您好'
    if 5 <= hour < 7: phrase = '早上好，昨晚睡的如何？'
    elif 7 <= hour < 10: phrase = '上午好，上午喝水了没？'
    elif 10 <= hour < 13: phrase = '吃饭了吗，拍照给我帮你记录下？'
    elif 13 <= hour < 17: phrase = '最近在做什么？'
    elif 17 <= hour < 20: phrase = '晚上吃的饭，我帮您记录下？'
    elif 20 <= hour < 23: phrase = '准备休息了，要不要看看今天的总结？'
    else: phrase = '夜深了，不要熬夜'
    return name + '，' + phrase + '\n\n' + SUFFIX


def valid_boot_id(value):
    if value is None:return None
    try:
        if type(value) is not str or str(UUID(value)) != value:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise ValueError('这次启动标识无效，请重新打开应用。') from None
    return value


def _plain_message(row):
    if row is None:return None
    return {key:value.isoformat() if isinstance(value,datetime) else value
            for key,value in asdict(message_from_row(row)).items()}


def welcome_groups_from_rows(rows, *, selected_ids=None, trigger_user_message_id=None, fold_pending=False):
    """Derive durable groups from saved turns; never mutate/delete history.

    A new opening folds the earlier unanswered openings. A user reply closes
    the interval without changing its grouping; the answered opening stays
    visible. ``fold_pending`` prepares the old tail before inserting a new
    opening in the same transaction, so the new opening is never included.
    """
    pending=[];groups=[]
    def append_group(folded):
        if not folded:return
        identifiers=[item['id']for item in folded]
        visible=identifiers if selected_ids is None else [item for item in identifiers if item in selected_ids]
        if visible:
            groups.append({'group_id':f'welcome:{identifiers[0]}:{identifiers[-1]}',
                'kind':'welcome','title':'','collapsed':True,'message_ids':visible,
                'total_count':len(identifiers),'trigger_user_message_id':None,'fold_stage':'startup'})
    for raw in sorted(rows,key=lambda row:row['sequence_no']):
        row=dict(raw)
        if row['role']=='assistant' and row.get('provider')==WELCOME_PROVIDER and row.get('status','complete')=='complete':
            pending.append(row)
        elif row['role']=='user':
            if len(pending)>1 and (trigger_user_message_id is None or row['id']==trigger_user_message_id):
                append_group(pending[:-1])
            pending=[]
    if trigger_user_message_id is None:
        append_group(pending if fold_pending else pending[:-1])
    return groups


def welcome_groups(connection, conversation_id, *, selected_ids=None, trigger_user_message_id=None, fold_pending=False):
    rows=connection.execute("SELECT id,sequence_no,role,provider,status FROM messages WHERE conversation_id=? "
        "AND (role='user' OR provider=?) ORDER BY sequence_no",(conversation_id,WELCOME_PROVIDER)).fetchall()
    return welcome_groups_from_rows(rows,selected_ids=selected_ids,trigger_user_message_id=trigger_user_message_id,fold_pending=fold_pending)


def annotate_welcome_rows(rows, groups):
    by_message={identifier:group for group in groups for identifier in group['message_ids']}
    result=[]
    for row in rows:
        item=dict(row)
        # Never trust stale local metadata to fold an actual question/answer.
        item.pop('welcome_fold',None)
        if item.get('provider')==WELCOME_PROVIDER and item.get('role')=='assistant' and item['id']in by_message:
            item['welcome_fold']=dict(by_message[item['id']])
        result.append(item)
    return result


def cleanup_for_groups(groups):
    # These groups were prepared at startup, not by the answer now streaming.
    # Retain the legacy count for old receipts without the startup marker.
    return {'checked':True,'folded_count':sum(group['total_count']for group in groups if group.get('fold_stage')!='startup')}


def ensure_greeting(chat, user_id, conversation_id, local_hour, boot_id=None):
    from .chat import MessageValidationError
    if type(conversation_id) is not int or conversation_id<=0 or type(local_hour) is not int or not 0<=local_hour<=23:
        raise MessageValidationError('请使用有效的对话和本机时间。')
    try:boot_id=valid_boot_id(boot_id)
    except ValueError as error:raise MessageValidationError(str(error))from None
    marker=BOOT_MODEL_PREFIX+boot_id if boot_id else None
    with chat.database.transaction() as connection:
        chat._resolve_conversation(connection,user_id,conversation_id)
        connection.execute('UPDATE conversations SET updated_at=updated_at WHERE id=? AND user_id=?',(conversation_id,user_id))
        if marker:
            # One actual boot belongs to one current conversation. Retrying
            # with another conversation must not create a second opening.
            row=connection.execute('SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id '
                'WHERE c.user_id=? AND m.provider=? AND m.model=? ORDER BY m.id LIMIT 1',(user_id,WELCOME_PROVIDER,marker)).fetchone()
            if row is not None and row['conversation_id']!=conversation_id:
                raise MessageValidationError('这次启动已在另一段对话中显示欢迎语。')
        else:
            row=connection.execute('SELECT * FROM messages WHERE conversation_id=? AND provider=? ORDER BY sequence_no LIMIT 1',(conversation_id,WELCOME_PROVIDER)).fetchone()
        if row is not None:
            result={'message':_plain_message(row),'created':False,'welcome_groups':welcome_groups(connection,conversation_id)}
            if boot_id:result['boot_id']=boot_id
            return result
        if boot_id:
            sequence=connection.execute('SELECT COALESCE(MAX(sequence_no),0)+1 FROM messages WHERE conversation_id=?',(conversation_id,)).fetchone()[0]
        else:
            sequence=0
            if connection.execute('SELECT 1 FROM messages WHERE conversation_id=? AND sequence_no=0',(conversation_id,)).fetchone()is not None:
                raise MessageValidationError('这段对话暂时无法添加欢迎语，请新建对话重试。')
        # Prepare folds from the existing history before creating this boot's
        # message. Both are committed together; neither rewrites saved turns.
        groups=welcome_groups(connection,conversation_id,fold_pending=True)
        profile=connection.execute('SELECT display_name,ai_preferred_name FROM member_profiles WHERE user_id=?',(user_id,)).fetchone()
        name=(profile['ai_preferred_name']or profile['display_name'])if profile is not None else'您好'
        now=timestamp_to_db(utc_now())
        cursor=connection.execute("INSERT INTO messages(conversation_id,sequence_no,role,content,status,error_message,provider,model,created_at,updated_at) VALUES(?,?,'assistant',?,'complete',NULL,?,?,?,?)",(conversation_id,sequence,opening_text(name,local_hour),WELCOME_PROVIDER,marker,now,now))
        connection.execute('UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?',(now,conversation_id,user_id))
        row=connection.execute('SELECT * FROM messages WHERE id=? AND conversation_id=?',(cursor.lastrowid,conversation_id)).fetchone()
        result={'message':_plain_message(row),'created':True,'welcome_groups':groups}
        if boot_id:result['boot_id']=boot_id
        return result


def include_greeting_context(chat, user_id, conversation_id, turns):
    conversation=chat.get_conversation(user_id,conversation_id)
    with chat.database.connect()as connection:
        row=connection.execute('SELECT content FROM messages WHERE conversation_id=? AND provider=? AND status=? ORDER BY sequence_no DESC LIMIT 1',(conversation.id,WELCOME_PROVIDER,'complete')).fetchone()
    if row is None:return turns
    opening={'role':'assistant','content':str(row['content'])}
    # Normal context queries exclude all welcome rows. Keep a single latest
    # relevant opening at the end, just before the new user's current input.
    return [*[turn for turn in turns if turn!=opening],opening]
