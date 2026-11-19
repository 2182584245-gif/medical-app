"""Historical chat self-reports and formal owner-scoped health rows are distinct."""
import pytest
from server.ai_persistence_truth import PersistenceTextGuard, formal_record_capacity, share_category
from ollama_chat_app.services.member_memory_prompt import MEMORY_FACT_RULES
from test_record_assistant_try12 import Tools

WORDS='我的早餐是两个煮鸡蛋和一杯豆浆，七点半吃的。'
ORIGINAL='你之前也提到过一样的早餐搭配（2026年11月19日两次记录都是两个煮鸡蛋加一杯豆浆、七点半）。'
OBSERVED='你之前也提到过一样的早餐搭配。'


def render(text,topic='diet',capacity=0,width=7):
    guard=PersistenceTextGuard(lambda:False,WORDS)
    guard.share_topic=topic
    guard.formal_cap=capacity
    return ''.join(guard.feed(text[start:start+width]) for start in range(0,len(text),width))+guard.finish()


@pytest.mark.parametrize('n',[1,2,9,128])
def test_exact_real_model_output_drops_only_false_count(n):
    suffix='想不想要我把今天这条整理成一条早餐记录草稿？'
    assert render(ORIGINAL+suffix,width=n)==OBSERVED+suffix


@pytest.mark.parametrize('text',[
    '你之前也提到过一样的早餐（两条聊天记录中都提过）。',
    '你之前也提到过一样的早餐（对话记录里也有两条）。',
    '我可以教你查看聊天记录。','你在聊天里两次提到早餐。',
    '这是你引用的内容：“2026年11月19日两次记录都是同样的早餐。”',
    '如果有两条正式饮食记录，可以在记录页查看。','你的正式饮食记录里还没有两条。',
    '你记得这是之前的早餐。','今天你说吃了两个煮鸡蛋和一杯豆浆。',
    '之前你有两条睡眠记录。', # a breakfast share cannot veto independent sleep history
])
def test_do_not_global_replace_record_in_history_quote_or_guidance(text):
    assert render(text)==text


def test_independent_formal_source_can_be_reported_when_available():
    explicit='你也提到过这餐（正式饮食记录中有两条）。'
    assert render(explicit,capacity=2)==explicit
    assert '两条' not in render(explicit,capacity=1)
    standalone='你的正式饮食记录里有两条。'
    assert render(standalone,capacity=2)==standalone
    assert '两条' not in render(standalone,capacity=0)
    assert render('你说过早餐。'+standalone,capacity=2)=='你说过早餐。'+standalone
    # A category-wide count alone is not proof that two rows both contain eggs.
    assert '都是' not in render('你之前也提到过这餐（两条正式饮食记录都是两个煮鸡蛋）。',capacity=2)


def test_collection_authorization_and_category_limit():
    t=Tools()
    t.add_old(1,'water',{'amount_ml':100})
    t.add_old(2,'diet',{'food':'鸡蛋'},actor=8)
    assert share_category(WORDS)=='diet'
    assert share_category('请查询之前有几条早餐记录？') is None
    assert share_category('如何删除聊天记录？') is None
    assert formal_record_capacity(t,t.principal,{'personal_context':True},'diet')==0
    assert formal_record_capacity(t,t.principal,{'personal_context':True},'water')==1
    t.add_old(3,'diet',{'food':'鸡蛋'})
    assert formal_record_capacity(t,t.principal,{'personal_context':True},'diet')==1
    t.settings['controls'][0]['enabled_for_ai']=False
    t.db.connect=lambda: (_ for _ in ()).throw(AssertionError('no database read'))
    assert formal_record_capacity(t,t.principal,{'personal_context':True},'diet')==0
    assert formal_record_capacity(t,t.principal,{'personal_context':False},'diet')==0
    assert '不能把提及次数算作餐次' in MEMORY_FACT_RULES and '聊天记录' in MEMORY_FACT_RULES
