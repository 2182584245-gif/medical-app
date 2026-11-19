import assert from 'node:assert/strict'
import test from 'node:test'
import {readFile} from 'node:fs/promises'
import {stripTypeScriptTypes} from 'node:module'
import {randomUUID} from 'node:crypto'
import {dispatchBusiness} from '../entry/src/main/ets/services/HybridBusinessApi.js'

async function source(name){
  const text=await readFile(new URL('../entry/src/main/ets/services/'+name,import.meta.url),'utf8')
  return stripTypeScriptTypes(text.replace(/^import .*$/gm,'').replace(/^export /gm,''),{mode:'transform'})
}
const choices=await source('PrivacyChoices.ets'),consent=await source('HybridConsent.ets')
function access(){
  const stored=new Map([['accepted_version','2026-11-18'],['2026-11-18_ai_37',false],['2026-11-18_health_37',false]])
  const preferences={getPreferencesSync:()=>({getSync:(key,fallback)=>stored.has(key)?stored.get(key):fallback,
    put:async(key,value)=>stored.set(key,value),flush:async()=>{}})}
  const state=Function('preferences',choices+'\nreturn {readPrivacy,optionalAllowed,privacyAccepted,setOptional}')(preferences)
  state.readPrivacy({})
  let prompts=0
  const request=Function('actorId','optionalAllowed','privacyAccepted','promptObserved',consent+
    '\nif(typeof registerHybridConsent==="function")registerHybridConsent(()=>{promptObserved();return Promise.resolve(false)});return requestHybridConsent')
  const ask=request(()=>37,state.optionalAllowed,state.privacyAccepted,()=>{prompts++})
  // The old callback would block the feature; the current flow must never need it.
  return {...state,ask,prompts:()=>prompts}
}

test('已有用户和新账号可直接用AI与记录，不继承旧用途拒绝开关；天气仍默认关闭',async()=>{
  const a=access()
  for(const owner of [0,37,38])for(const feature of ['ai','health'])assert.equal(a.optionalAllowed(feature,owner),true)
  assert.equal(await a.ask('ai'),true)
  assert.equal(await a.ask('health'),true)
  assert.equal(a.prompts(),0)
  assert.equal(a.optionalAllowed('weather',37),false)
  await a.setOptional('weather',37,true)
  assert.equal(a.optionalAllowed('weather',37),true)
  assert.equal(a.optionalAllowed('weather',38),false)
})

test('真实业务桥在旧用途为拒绝时仍能读写本人记录和聊天，仍拦截跨账号请求',async()=>{
  const a=access(),calls=[]
  const n={id:randomUUID,owner:()=>37,user:()=>({id:37}),scope:()=>'',resetJournal:async()=>'',
    restore:async()=>{},guestCleanupPending:async()=>false,allowed:use=>a.optionalAllowed(use,37),consent:a.ask,
    recordCache:async()=>'',networkError:()=>false,reminders:async()=>{},
    rpc:async(service,method,args,kwargs)=>{calls.push({service,method,args,kwargs});return method==='list_records'?{items:[{id:7}]}:77},
    chat:async()=>({assistant_message:{id:23,content:'今天的情况我理解了。'}})}
  const request=(action,payload)=>dispatchBusiness('member.request',{action,payload:{...payload,_fawnsee_actor_id:37},request_id:randomUUID()},n)
  const listed=await request('expansion.records.list',{})
  assert.equal(listed.items[0].id,7)
  await request('health.records.add',{category:'water',occurred_at:'2026-11-19T08:00:00+08:00',content:'饮水300毫升',details:{amount_ml:300}})
  assert.equal(calls[1].method,'add_life_record')
  assert.deepEqual(calls[1].args,[37])
  assert.equal((await request('chat.send',{conversation_id:5,content:'今天喝了温水'})).assistant_message.id,23)
  assert.equal(a.prompts(),0)
  await assert.rejects(dispatchBusiness('member.request',{action:'health.records.add',payload:{_fawnsee_actor_id:38}},n),{code:'account_changed'})
})
