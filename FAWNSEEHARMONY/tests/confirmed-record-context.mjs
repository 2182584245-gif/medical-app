import test from 'node:test'
import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import {stripTypeScriptTypes} from 'node:module'
import {receiptCandidate, confirmedLocator, guardConfirmedDraft} from '../entry/src/main/ets/services/HybridRecordContext.js'

const text='请修改刚才那条备注为「模拟餐27」的早餐旧记录，热量改成420千卡，再补充一句「饭后散步十分钟」。原照片和其他已核对的内容保留。'
const meal={id:451,category:'diet',occurred_at:'2026-11-18T00:00:00Z',version:'verified',details:{meal_type:'breakfast',notes:'模拟餐27',photo_file_ids:[8],calories_kcal:350}}
const original={id:'draft',category:'diet',operation:'create',occurred_at:'2026-11-18T08:00:00+08:00'}
const card={role:'assistant',record_drafts_ready:true,record_drafts:[original],record_receipts:[{id:'draft',status:'saved',record_id:451}]}
const page={conversation:{id:45},messages:[card]}
function evidence(input=text,packet=page,row=meal){
  return confirmedLocator(input,receiptCandidate(input,packet,55,45),row)
}

test('实名下同一会话的已确认回执与当前云端条目一致才提供原日期时间、匹配词及目标编号',()=>{
  const info=evidence()
  assert.deepEqual(info.guard,{id:451,category:'diet',version:'verified',append:true})
  assert.match(info.annex,/"date":"2026-11-18","time":"08:00","contains":"模拟餐27"/)
  assert.match(info.annex,/"record_id":451/)
  for(const row of [{...meal,id:452},{...meal,occurred_at:'2026-11-19T00:00:00Z'},{...meal,category:'sleep'},
    {...meal,details:{...meal.details,notes:'另一条'}},{...meal,details:{...meal.details,meal_type:'lunch'}}])assert.equal(evidence(text,page,row),null)
})
test('换会话、未保存、多卡片、后续轮次、矛盾或无法验证的定位词均交还原有问答',()=>{
  assert.equal(evidence(text,{...page,conversation:{id:46}}),null)
  for(const last of [{...card,record_drafts_ready:false},{...card,record_drafts:[original,original]},
    {...card,record_receipts:[{id:'draft',status:'cancelled',record_id:451}]},{role:'assistant',content:'下一回合'}]){
    assert.equal(evidence(text,{...page,messages:[card,last]}),null)
  }
  for(const request of [text.replace('刚才那条','昨天那条'),text.replace('早餐','晚餐'),text.replace('早餐','牛奶早餐'),
    text.replace('早餐','饮水'),text.replace('旧记录','草稿'),text.replace('旧记录','旧记录和另一条')])assert.equal(evidence(request),null)
})
test('同一条草稿在展示和确认时均保留原备注及原图，明确替换、删除时保留模型提议的字段',()=>{
  const draft={category:'diet',operation:'update',target:{id:451,version:'verified',before:meal},details:{notes:'饭后散步十分钟',photo_file_ids:[8],calories_kcal:420}}
  const guard=evidence().guard
  const safe=guardConfirmedDraft(draft,guard)
  assert.equal(safe.details.notes,'模拟餐27；饭后散步十分钟')
  assert.deepEqual(safe.details.photo_file_ids,[8])
  assert.deepEqual(guardConfirmedDraft(safe,guard),safe)
  assert.equal(guardConfirmedDraft({...draft,details:{notes:''}},guard).blocked,true)
  assert.equal(guardConfirmedDraft({...draft,details:{notes:meal.details.notes}},guard).blocked,true)
  const replace=evidence(text.replace('再补充一句','把备注改成')).guard
  assert.equal(replace.append,false)
  assert.equal(guardConfirmedDraft(draft,replace).details.notes,'饭后散步十分钟')
  assert.equal(guardConfirmedDraft({...draft,details:{notes:''}},evidence(text.replace('再补充一句「饭后散步十分钟」','删除备注')).guard).details.notes,'')
  for(const bad of [{...draft,operation:'create'},{...draft,category:'activity'},{...draft,target:{...draft.target,id:88}},
    {...draft,target:{...draft.target,version:'new'}}])assert.equal(guardConfirmedDraft(bad,guard).blocked,true)
})

test('真实发送函数保留用户可见原话，仅获授权的模型请求附回执；关掉本轮资料或选中草稿不读取',async()=>{
  const file=await readFile(new URL('../entry/src/main/ets/pages/AssistantService.ets',import.meta.url),'utf8')
  const start=file.indexOf('export async function sendChat('), end=file.indexOf('async function restoreAnswer(',start)
  const source=stripTypeScriptTypes(file.slice(start,end).replace('export async','async'),{mode:'transform'})
  const controls={life_records:{enabled_for_ai:true},ai_records:{enabled_for_ai:true}}
  for(const [enabled,task,expected] of [[true,'',true],[false,'',false],[true,'a-selected-task',false]]){
    let shown, posted, consulted=0, remembered
    const io={
      cloud:async()=>({memory_enabled:true,member_user_id:55,memory_revision:'revision',memory_controls:controls}),
      child:(o,k)=>o[k]||{},stringValue:(o,k,f='')=>String(o[k]??f),array:(o,k)=>o[k]||[],
      rpc:async(_service,method,args)=>{if(method==='context_for_provider')return [];assert.equal(method,'begin_message');shown=args[1];return {assistant_message:{id:717}}},
      childUuid:async()=>'op-begin',assistantStream:async body=>{posted=body},restoreAnswer:async()=>{}
    }
    const send=Function(...Object.keys(io), source+';return sendChat;')(...Object.values(io))
    const op={actor:55,conversation:45,id:'op',input:text,image:'',additionalFiles:[],task,guard(){},
      async lookupReceipt(){consulted++;return evidence()},async storeReceiptGuard(id,g){remembered={id,guard:g}}}
    await send(op,{mobile_ai_personal_enabled:enabled,mobile_ai_library_enabled:false},['prior-draft'],()=>{})
    assert.equal(shown,text)
    assert.equal(posted.messages.at(-1).content,expected?text+evidence().annex:text)
    assert.equal(consulted,expected?1:0)
    assert.equal(remembered?.id,expected?717:undefined)
    assert.deepEqual(posted.decided_record_draft_ids,['prior-draft'])
    assert.equal(posted.record_task_id,task||undefined)
  }
})
