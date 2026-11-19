import json
from datetime import datetime
import pytest
from test_record_assistant_try12 import Tools, DATE, VISION, PHOTO, change, different_photo, record, run_turn

CAP=record.DIALOGUE_PHOTOS_CAPABILITY
MSG='请把这张补拍照片也补充到2026年1月3日上午八点的那条玉米面饼早餐旧记录，其他已核对的信息和原来的照片都保留。'
STAMP=DATE+'T00:00:00.000000Z'
def setup(meal='breakfast',food='玉米面饼',actor=7):
    tools=Tools()
    tools.add_photo_file(32);tools.add_photo_file(33,payload=different_photo())
    tools.add_old(100,'diet',{'food':food,'meal_type':meal,'calories_kcal':420,'notes':'保留原备注','photo_file_ids':[32]},actor=actor,stamp=STAMP)
    return tools
def turn(tools,msg=MSG,*,personal=True):
    parts=[{'type':'text','text':msg},{'type':'image_url','image_url':{'url':different_photo()}}]
    return run_turn(tools,parts,[],model=VISION,personal=personal,capability=CAP)

def photo_plan(tools,existing,current):
    return record._plan_record_photos(tools,tools.principal,{'category':'diet','operation':'supplement'},
        {'details':{'photo_file_ids':existing}},None,MSG,current,extended=True,multiple=False)

def test_identical_owned_bytes_at_another_id_keep_original_without_reappending():
    tools=setup();tools.add_photo_file(35)
    assert photo_plan(tools,[32],[35])==([32],False,'')
    assert photo_plan(tools,[32,35],[33])==([32,35,33],True,'')
    tools.add_photo_file(75,actor=8)
    assert photo_plan(tools,[32],[75])[2]
    tools.db.conn.execute("UPDATE user_file_contents SET sha256='bad' WHERE file_id=35")
    assert photo_plan(tools,[32],[35])[2]

def test_duplicate_only_attachment_explains_that_the_image_is_already_saved():
    tools=setup()
    raw=[{'type':'text','text':MSG+'如果已是同一张不用重复存。'},{'type':'image_url','image_url':{'url':PHOTO}}]
    ds,reply,_=run_turn(tools,raw,[],model=VISION,personal=True,capability=CAP)
    assert not ds and '无需重复保存' in reply
    old=json.loads(tools.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0])
    assert old['photo_file_ids']==[32]

def test_duplicate_pruned_before_photo_cap_and_existing_duplicates_untouched():
    tools=setup()
    for ident in [34,35,36]:tools.add_photo_file(ident)
    assert photo_plan(tools,[32,34,35,36],[32])==([32,34,35,36],False,'')
    assert photo_plan(tools,[32,34,35,36],[33])[2].startswith('这条记录最多')
def test_existing_meal_image_only_when_specialist_returned_no_fields():
    tools=setup();rows,answer,_=turn(tools);r,=rows
    assert r['operation']=='supplement' and r['target']['id']==100 and r['photo_file_ids']==[32,33]
    assert not r['missing_required'] and not r['blocked'] and r['details']['calories_kcal']==420 and r['details']['notes']=='保留原备注'
    stored=tools.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0]
    assert json.loads(stored)['photo_file_ids']==[32] and '确认' in answer
def test_live_specialist_joined_food_and_meal_clue_matches_separate_fields():
    tools=setup()
    parts=[{'type':'text','text':MSG},{'type':'image_url','image_url':{'url':different_photo()}}]
    provider=[change('diet',{},operation='supplement',stamp=None,selector={'date':DATE,'time':'08:00','contains':'玉米面饼早餐'})]
    rows,_,_=run_turn(tools,parts,provider,model=VISION,personal=True,capability=CAP)
    assert len(rows)==1 and rows[0]['target']['id']==100 and rows[0]['photo_file_ids']==[32,33]
@pytest.mark.parametrize('meal,food,owner',[('lunch','玉米面饼',7),('breakfast','玉米面饼',8),('breakfast','另一食物',7)])
def test_recovery_never_uses_wrong_meal_wrong_food_or_another_owner(meal,food,owner):
    rows,_,_=turn(setup(meal,food,owner));assert len(rows)==1 and rows[0]['target'] is None and rows[0]['missing_required']
def test_ambiguous_target_or_closed_consent_never_yields_savable_card():
    tools=setup();tools.add_old(101,'diet',{'food':'玉米面饼','meal_type':'breakfast'},stamp=STAMP)
    rows,msg,_=turn(tools);assert rows[0]['target'] is None and '多条' in msg
    rows,_,_=turn(setup(),personal=False);assert rows[0]['blocked'] and rows[0]['target'] is None
def test_no_silent_downgrade_or_omitted_qualifier():
    ref=datetime(2026,1,4,12,tzinfo=record.BEIJING)
    assert record._photo_meal_selector(MSG,ref)==({'date':DATE,'time':'08:00','contains':'玉米面饼'},'早餐','玉米面饼')
    for s in [MSG.replace('请把','不要把'),MSG.replace('上午八点','上午八点或者九点'),MSG.replace('的那条','在深圳的那条'),MSG.replace('旧记录，','旧记录，再把热量改成500，'),MSG.replace('玉米面饼','玉米面饼或者花卷')]:
        assert record._photo_meal_selector(s,ref) is None
    assert not record.explicit_photo_retention('不用把这张补拍照片补充到原记录。')
def test_explicit_choice_after_acknowledging_wrong_prior_time():
    options={'field':'record_time','before':'15:00','after':'16:00'}
    assert record._resolve_fact_conflict(options,'之前三点记错了，确定下午四点喝的，三百毫升。',{})=='after'
    assert record._resolve_fact_conflict(options,'之前三点说错了，确定下午三点。',{})=='before'
    assert record._resolve_fact_conflict(options,'之前三点记错了，确定下午五点。',{}) is None
    assert record._resolve_fact_conflict(options,'三点还是四点？',{}) is None


def test_future_filling_and_future_drinking_are_distinct():
    tools=Tools()
    statement='请新建一份饮水记录，昨天上午八点喝白水，备注是蓝风铃杯，容量等会补。'
    draft,answer,_=run_turn(tools,statement,[change('water',{'notes':'蓝风铃杯'},stamp=STAMP)],capability=CAP)
    assert len(draft)==1 and any(f['field']=='amount_ml' for f in draft[0]['missing_required'])
    for future in ('请记录饮水，我等会喝白水，容量等会补。','请记录，我还没喝水，水量等会填。'):
        out=record.make_drafts([change('water',{'amount_ml':500})],tools=tools,principal=tools.principal,personal=False,prior={},latest=future,dialogue_enabled=True)
        assert not out[0]


def test_sleep_qualitative_input_is_never_a_numeric_score():
    raw={'changes':[change('sleep',{'quality':'良好','duration_hours':7,'notes':'自述'},stamp=STAMP)]}
    data,=record.changes(raw,dialogue_text='昨晚睡了七小时，睡眠质量良好。')
    assert data['details']=={'duration_hours':7,'notes':'自述；睡眠质量良好'}
    other,=record.changes(raw,dialogue_text='只知道睡了七小时。')
    assert other['details']=={'duration_hours':7,'notes':'自述'}
    with pytest.raises(record.ToolError):record.changes(raw)
    bad={'changes':[change('sleep',{'duration_hours':'七','quality':'一般'})]}
    with pytest.raises(record.ToolError):record.changes(bad,dialogue_text='睡眠质量一般。')


def test_date_of_waking_is_not_confused_with_start_of_last_night():
    text='请给我记录睡眠。2026年1月3日早上七点醒来，昨晚共睡了七小时，睡眠质量良好。'
    tools=Tools();rows,_,_=run_turn(tools,text,[change('sleep',{'duration_hours':7,'quality':'良好'},stamp=None)],capability=CAP)
    assert len(rows)==1 and rows[0]['occurred_at']==DATE+'T07:00:00+08:00'
    assert not rows[0]['missing_required'] and rows[0]['details']['notes']=='睡眠质量良好'


@pytest.mark.parametrize('operation,field', [('supplement',{'quality':'良好','duration_hours':2}),('supplement',{'notes':'睡眠质量良好'}),('update',{'notes':'睡眠质量良好'})])
def test_old_sleep_subjective_add_preserves_score_duration_and_unrelated_note(operation,field):
    z=Tools();before={'duration_hours':7,'quality':4,'notes':'开了窗；床头灯已关'}
    z.add_old(100,'sleep',before)
    msg=f'请在{DATE}晚上八点的旧睡眠记录里补充一下，睡眠质量良好。请保留原备注和数字评分。'
    ds,_,_=run_turn(z,msg,[change('sleep',field,operation=operation,stamp=None,selector={'date':DATE,'time':'20:00'})],personal=True,capability=CAP)
    d,=ds;assert d['target']['id']==100 and not d['missing_required'] and not d['blocked']
    assert d['operation']=='update' and d['details']=={**before,'notes':'开了窗；床头灯已关；睡眠质量良好'}
    assert d['content']=='本人旧记录' and json.loads(z.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0])==before


@pytest.mark.parametrize('patch',[{'quality':'一般'},{'notes':'睡眠质量一般'}])
def test_old_sleep_explicit_edit_replaces_only_single_qualitative_clause(patch):
    z=Tools();initial={'duration_hours':7,'quality':4,'notes':'开了窗；睡眠质量良好；床头灯已关'}
    z.add_old(100,'sleep',initial)
    msg=f'请修改{DATE}晚上八点那条睡眠记录，把睡眠质量改成一般。数字评分和其他说明都保留。'
    ds,_,_=run_turn(z,msg,[change('sleep',patch,operation='update',stamp=None,selector={'date':DATE,'time':'20:00'})],personal=True,capability=CAP)
    d,=ds;assert d['target']['id']==100 and not d['missing_required'] and d['details']=={**initial,'notes':'开了窗；睡眠质量一般；床头灯已关'}


def test_conflicting_unqualified_added_sleep_description_is_not_auto_rewritten():
    before='开了窗；睡眠质量良好'
    assert record._sleep_notes_patch(before,'睡眠质量一般','再补充，睡眠质量一般。') is False
    assert record._sleep_notes_patch(before,'睡眠质量一般','睡眠质量改成一般吗？') is None


def test_model_understands_unlisted_colloquial_feeling_and_must_quote_the_users_words():
    z=Tools();original={'duration_hours':7,'quality':4,'notes':'开了窗；床头灯已关'}
    z.add_old(100,'sleep',original);phrase='这一觉基本没醒过，挺踏实'
    msg=f'请在{DATE}晚上八点的原睡眠记录里补一句：{phrase}。'
    raw={**change('sleep',{},operation='supplement',stamp=None,selector={'date':DATE,'time':'20:00'}),'sleep_experience':phrase}
    ds,_,_=run_turn(z,msg,[raw],personal=True,capability=CAP);d,=ds
    assert d['operation']=='update' and d['details']=={**original,'notes':'开了窗；床头灯已关；睡眠感受：「'+phrase+'」'}
    assert '_sleep_experience_quote' not in d
    invented=record.changes({'changes':[{**raw,'sleep_experience':'另外编造的感受'}]},dialogue_text=msg)
    assert '_sleep_experience_quote' not in invented[0]
    replaced=record._sleep_notes_patch(d['details']['notes'],'原话','请更正原感受为翻了一宿身。','翻了一宿身','update')
    assert replaced=='开了窗；床头灯已关；睡眠感受：「翻了一宿身」'


def test_historical_unquoted_free_subjective_cannot_consume_an_unrelated_fact_after_comma():
    original='睡眠感受：挺踏实，开了窗'
    assert record._sleep_notes_patch(original,'睡眠质量一般','把睡眠质量改成一般。') is False
    z=Tools()
    old={'duration_hours':7,'quality':4,'notes':original};z.add_old(100,'sleep',old)
    msg=f'请修改{DATE}晚上八点的睡眠记录，把睡眠质量改成一般。'
    rows,answer,_=run_turn(z,msg,[change('sleep',{'notes':'睡眠质量一般'},operation='update',stamp=None,selector={'date':DATE})],personal=True,capability=CAP)
    assert rows==[] and '哪一段' in answer
    assert json.loads(z.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0])==old


def test_quoted_free_subjective_with_internal_comma_preserves_all_adjacent_facts():
    original='关了灯，睡眠感受：「前半夜醒过，后来挺稳」，开了窗'
    assert record._sleep_notes_patch(original,'睡眠质量一般','把睡眠质量改成一般。')=='关了灯，睡眠质量一般，开了窗'
    assert record._sleep_notes_patch('睡眠质量良好，开了窗','睡眠质量一般','把睡眠质量改成一般。')=='睡眠质量一般，开了窗'


SLEEP_CORRECTION=f'请修改{DATE}早上06:31那条睡眠记录，把睡眠质量改成一般。数字评分和其他说明都保留。'

@pytest.mark.parametrize('malformed',[{'unknown':'bad-shape'},{'changes':[{'operation':'update','target':222}]},
    {'changes':[{**change('sleep',{}),'unexpected_field':'unusable-output'}]}])
def test_bad_post_inference_schema_preserves_unrelated_data_and_only_locates_own_exact_sleep(malformed):
    z=Tools();before={'duration_hours':7,'quality':4,'notes':'开了窗；床头灯已关；睡眠质量良好'}
    for ident,owner in [(100,7),(222,8)]:
        z.add_old(ident,'sleep',before,actor=owner,stamp=DATE+'T06:31:00+08:00')
    rows,_,_=run_turn(z,SLEEP_CORRECTION,[],personal=True,capability=CAP,raw_extracted=json.dumps(malformed))
    d,=rows;assert d['target']['id']==100 and not d['missing_required'] and not d['blocked']
    assert d['content']=='本人旧记录' and d['details']=={**before,'notes':'开了窗；床头灯已关；睡眠质量一般'}
    assert json.loads(z.db.conn.execute('SELECT details_json FROM life_records WHERE id=100').fetchone()[0])==before


def test_bad_schema_never_picks_between_two_sleep_records_accesses_without_consent_or_guesses_unrelated_edits():
    z=Tools();old={'duration_hours':7,'quality':4,'notes':'开了窗；睡眠质量良好'}
    for ident in [100,101]:z.add_old(ident,'sleep',old,stamp=DATE+'T06:31:00+08:00')
    bad=json.dumps({'unexpected':True})
    rows,answer,_=run_turn(z,SLEEP_CORRECTION,[],personal=True,capability=CAP,raw_extracted=bad)
    assert '多条' in answer and not any(row.get('target') for row in rows)
    rows,_,_=run_turn(z,SLEEP_CORRECTION,[],personal=False,capability=CAP,raw_extracted=bad)
    assert not any(row.get('target') for row in rows)
    for other in ('请把那条质量改成一般','一般还是良好？','昨晚睡了七小时，质量一般。',
                  SLEEP_CORRECTION+'另把时长改成五小时。',SLEEP_CORRECTION.replace('早上06:31','')):
        assert record.recover_explicit_sleep_edit(other) is None


def test_actual_meal_camera_shortcut_keeps_source_image_and_asks_only_missing_facts():
    tools=Tools();tools.add_photo_file(32)
    raw=[{'type':'text','text':record._REVIEW_MEAL_PHOTO},{'type':'image_url','image_url':{'url':PHOTO}}]
    assert record.extraction_context(raw,{},dialogue_enabled=True)['review_meal_photo_shortcut'] is True
    rows,ask,_=run_turn(tools,raw,[],model=VISION,personal=True,capability=CAP)
    row,=rows
    assert row['operation']=='create' and row['category']=='diet' and row['photo_file_ids']==[32]
    assert {f['field'] for f in row['missing_required']}=={'record_date','record_time','food'}
    assert '吃了什么' in ask and '类别' not in ask
    msg='昨天早上八点吃了洋芋小饼，320千卡，这张原图请一起留在饮食记录里。'
    update=change('diet',{'food':'洋芋小饼','calories_kcal':320},stamp=None)
    ds,_,_=run_turn(tools,msg,[update],personal=True,capability=CAP)
    card,=ds;assert card['supersedes_draft_id']==row['id'] and card['photo_file_ids']==[32]
    assert not card['missing_required'] and card['details']['food']=='洋芋小饼'


def test_meal_shortcut_does_not_invent_selected_meal_time_or_force_text_only_cases():
    tools=Tools();tools.add_photo_file(32)
    raw=[{'type':'text','text':record._REVIEW_MEAL_PHOTO},{'type':'image_url','image_url':{'url':PHOTO}}]
    rows,_,_=run_turn(tools,raw,[change('diet',{'food':'香蕉'},stamp=STAMP)],model=VISION,personal=True,capability=CAP)
    row,=rows;assert row['details']['food']=='香蕉' and row['occurred_at'] is None and row['photo_file_ids']==[32]
    assert not record._review_meal_photo(record._REVIEW_MEAL_PHOTO)
    assert not record._review_meal_photo([{'type':'text','text':'只分析这张餐照，不用记录。'},raw[1]])
