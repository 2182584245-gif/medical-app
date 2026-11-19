import assert from 'node:assert/strict'
import { randomUUID, createHash } from 'node:crypto'
import test from 'node:test'
import { dispatchBusiness } from '../entry/src/main/ets/services/HybridBusinessApi.js'

function host(actor=37) {
  const calls=[], files=new Map(), guestId=randomUUID()
  return {
    calls,files,actor,owner(){return this.actor},user(){return this.actor?{id:this.actor,role_code:'member'}:null},
    scope(){return 'a'.repeat(64)},id:randomUUID,digest:async v=>createHash('sha256').update(v).digest('hex'),guestIdentity(){return guestId},
    async restore(){},async resetJournal(){return ''},async guestCleanupPending(){return false},async recordCache(){return ''},networkError(){return false}, async consent(){return true},allowed(){return true},async reminders(){},
    async scopedFile(key,mode,value){if(mode==='set')files.set(key,value);if(mode==='delete')files.delete(key);return files.get(key)||''},
    async rpc(service,method,args,kwargs,id){calls.push({service,method,args,kwargs,id});return method==='upload_bytes'?95:17},
    async rest(path,method,body,id){calls.push({path,method,body,id});return {ok:true}},
    async guest(action,body,id){calls.push({action,body,id});return []}
  }
}
function request(n,action,b,op=randomUUID()){return dispatchBusiness('member.request',{action,payload:{...b,_fawnsee_actor_id:n.actor},request_id:op},n)}

test('保存智能设置只发送偏好字段，外层继续沿用同一操作编号',async()=>{
  const n=host(),op=randomUUID()
  const changes={ai_complexity:'standard',ai_tone:'concise',ai_context_consent:false,
    ai_context_consent_decided:true,ai_personal_context_consent:false,ai_thinking_enabled:false,
    ai_reasoning_effort:'low',ai_web_enabled:true,ai_tools_enabled:false}
  await request(n,'preferences.update',{...changes,request_id:op},op)
  assert.deepEqual(n.calls[0],{service:'preferences',method:'update_preferences',id:op,kwargs:{},
    args:[37,{ai_complexity:'standard',ai_tone:'concise',mobile_ai_library_enabled:false,
      ai_context_consent_decided:true,mobile_ai_personal_enabled:false,mobile_ai_thinking_enabled:false,
      mobile_ai_reasoning_effort:'low',mobile_ai_web_enabled:true,mobile_ai_tools_enabled:false}]})
  await assert.rejects(request(n,'preferences.update',{...changes,request_id:randomUUID()},op),{code:'conflict'})
  assert.equal(n.calls.length,1)
})

test('用户停止后只采信同一账号同一次对话操作的云端终态，下一条使用新编号',async()=>{
  const n=host(),conv=73
  for(const mode of ['before-first-chunk','during-stream','complete','running','wrong-id','wrong-conversation','owner-changed','unrequested']){
    const op=randomUUID(), started=Promise.withResolvers(), deferred=Promise.withResolvers()
    const original=Object.assign(Error('数据已有变化'),{status:409})
    let statusReads=0
    n.chat=async()=>{started.resolve();return deferred.promise}
    n.rest=async(path,method)=>{
      if(method==='POST'){assert.equal(path,'/v1/ai/operations/'+op+'/cancel');return {}}
      assert.equal(path,'/v1/ai/operations/'+op+'?kind=chat.send')
      statusReads++
      return {operation_id:mode==='wrong-id'?randomUUID():op,kind:'chat.send',
        conversation_id:mode==='before-first-chunk'?null:mode==='wrong-conversation'?99:conv,
        state:['complete','running'].includes(mode)?mode:'cancelled'}
    }
    const sending=request(n,'chat.send',{conversation_id:conv,content:'试发'},op)
    await started.promise
    if(mode!=='unrequested')await dispatchBusiness('native.cancelChat',{operation_id:op},n)
    if(mode==='owner-changed')n.actor=39
    deferred.reject(original)
    if(['before-first-chunk','during-stream'].includes(mode))await assert.rejects(sending,{code:'cancelled'})
    else await assert.rejects(sending,e=>e===original)
    assert.equal(statusReads,['unrequested','owner-changed'].includes(mode)?0:1)
    n.actor=37
    await assert.rejects(dispatchBusiness('native.cancelChat',{operation_id:op},n),{code:'not_found'})
    const next=randomUUID()
    n.chat=async(_data,id)=>{assert.equal(id,next);return {ok:true}}
    assert.deepEqual(await request(n,'chat.send',{conversation_id:conv,content:'另一条'},next),{ok:true})
  }
})

test('迟按停止与正常完成交错，仍交付完成的答复',async()=>{
  const n=host(),op=randomUUID(),started=Promise.withResolvers(),done=Promise.withResolvers()
  n.chat=async()=>{started.resolve();return done.promise}
  const answer=request(n,'chat.send',{conversation_id:73,content:'试发'},op)
  await started.promise
  await dispatchBusiness('native.cancelChat',{operation_id:op},n)
  done.resolve({assistant_message:{id:221,status:'complete',content:'已完成的答复'}})
  assert.deepEqual((await answer).assistant_message,{id:221,status:'complete',content:'已完成的答复'})
  assert.equal(n.calls.length,1)
})

test('会员启动、游客隔离和固定的云端操作范围',async()=>{
  const n=host(),boot=await dispatchBusiness('app.bootstrap',{},n)
  assert.equal(boot.user.id,37);assert.equal(boot.guest,null)
  await assert.rejects(dispatchBusiness('member.request',{action:'health.records.add',payload:{_fawnsee_actor_id:99}},n),{code:'account_changed'})
  await assert.rejects(request(n,'health.records.add',{user_id:99}),{code:'permission'})
  await assert.rejects(request(n,'admin.any',{}),{code:'feature_unavailable'})
  assert.equal(n.calls.length,0)
  const guest=host(0),g=(await dispatchBusiness('bootstrap',{},guest)).guest
  await assert.rejects(dispatchBusiness('guest.request',{guest_id:randomUUID(),action:'chat.files.list'},guest),{code:'account_changed'})
  await assert.rejects(dispatchBusiness('guest.request',{guest_id:g.id,action:'health.profile.get'},guest),{code:'login_required'})
  await dispatchBusiness('guest.request',{guest_id:g.id,action:'chat.files.list'},guest)
  assert.equal(guest.calls[0].action,'chat.files.list')
})

test('六类生活记录和身体指标均保留所属会员、表单字段及操作编号',async()=>{
  const n=host()
  for(const category of ['diet','water','activity','sleep','environment','medical']){
    const op=randomUUID(),row={category,occurred_at:'2026-11-18T20:00:00+08:00',content:category,details:{notes:'口语核对'}}
    await request(n,'health.records.add',row,op)
    const sent=n.calls.at(-1);assert.equal(sent.method,'add_life_record');assert.equal(sent.id,op)
    assert.deepEqual(sent.args,[37]);assert.deepEqual(sent.kwargs,row)
  }
  await request(n,'expansion.observations.create',{kind:'weight',value:70,unit:'kg'})
  assert.deepEqual(n.calls.at(-1).args,[37,{kind:'weight',value:70,unit:'kg'}])
  await request(n,'expansion.records.update',{record_id:18,base_version:3,changes:{content:'修正'},reason:'口语补充'})
  assert.deepEqual(n.calls.at(-1).args,[37,{record_id:18,base_version:3,changes:{content:'修正'},reason:'口语补充'}])
})

test('图片先以本机引用预览，保存/重试转为同一个服务端照片编号',async()=>{
  const n=host(),owner=n.actor
  const base='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/2n0AAAAASUVORK5CYII='
  const img={name:'test.png',media_type:'image/png',size_bytes:Buffer.from(base,'base64').length,data_base64:base}
  n.checkPhoto=async (v,m,size)=>{const bytes=Buffer.from(v,'base64');assert.equal(bytes.length,size);assert.equal(m,'image/png');assert.equal(bytes.readUInt32BE(0),0x89504e47)}
  const staged=await dispatchBusiness('record.photo.stage',{_fawnsee_actor_id:owner,image:img},n)
  assert.ok(staged.file_id<0);assert.equal(n.calls.length,0)
  assert.deepEqual(await dispatchBusiness('record.photo.get',{_fawnsee_actor_id:owner,file_id:staged.file_id},n),staged)
  const op=randomUUID()
  for(let i=0;i<2;i++)await request(n,'health.records.add',{category:'diet',content:'晚餐',details:{food:'米饭',photo_file_ids:[staged.file_id]}},op)
  assert.equal(n.calls.filter(c=>c.method==='upload_bytes').length,1)
  for(const c of n.calls.filter(c=>c.method==='add_life_record')){assert.deepEqual(c.kwargs.details.photo_file_ids,[95]);assert.equal(c.kwargs.details.food,'米饭');assert.equal(c.id,op)}
})

test('未同意健康用途不发送写入，已填写的语音候选必须再次确认',async()=>{
  const n=host();n.allowed=()=>false;n.consent=async()=>false
  await assert.rejects(request(n,'health.records.add',{category:'water',content:'饮水'}),{code:'consent_required'})
  assert.equal(n.calls.length,0)
  n.allowed=()=>true;n.consent=async()=>true
  n.voiceRows=async text=>[{category:'water',occurred_at:'2026-11-18T20:00:00+08:00',content:text,details:{amount_ml:250}}]
  const draft=await request(n,'voice.records.propose',{text:'喝了250毫升水'})
  assert.equal(draft.requires_confirmation,true);assert.equal(draft.saved,false);assert.equal(n.calls.length,0)
  const op=randomUUID();n.childId=async()=> 'bb1d8da6-115e-49f3-8fc5-7705b7034982'
  const receipt=await request(n,'voice.records.confirm',{records:draft.records,operation_id:op},op)
  assert.equal(receipt.complete,true);assert.equal(receipt.saved.length,1);assert.equal(n.calls[0].method,'add_life_record')
})

test('离线新增再修改在重连时按同一操作号顺序提交，缓存不会写入其他会员',async()=>{
  const n=host();const caches=new Map(),remote=new Map(),applied=[],nextId=113
  let online=true
  n.recordCache=async(k,mode,value)=>{if(mode==='set')caches.set(k,value);return caches.get(k)||''}
  n.networkError=e=>e.code==='offline_error'
  n.rpc=async(service,method,args,data,id)=>{
    if(!online)throw Object.assign(Error('network down'),{code:'offline_error'})
    if(args[0]!==37)throw Error('wrong actor')
    if(method==='list_records')return {items:[...remote.values()],total:remote.size,limit:100,offset:0}
    if(method==='add_life_record'){remote.set(nextId,{...data,id:nextId,version:'v1'});applied.push(id);return nextId}
    if(method==='get_record')return remote.get(args[1].record_id)
    if(method==='update_record'){const b=args[1];assert.equal(b.record_id,nextId);assert.equal(b.base_version,'v1');const row={...remote.get(nextId),...b.changes,version:'v2'};remote.set(nextId,row);applied.push(id);return row}
    throw Error('unexpected '+method)
  }
  const filters={limit:100,offset:0}
  await request(n,'expansion.records.list',filters)
  online=false
  const op=randomUUID(),edit=randomUUID(),local=await request(n,'health.records.add',{category:'water',occurred_at:'2026-11-18T20:00:00+08:00',content:'250毫升',details:{amount_ml:250}},op)
  assert.equal(local.local_only,true);assert.ok(local.id<0);assert.deepEqual(applied,[])
  await request(n,'expansion.records.update',{record_id:local.id,base_version:local.version,reason:'本人补充',changes:{content:'300毫升',details:{amount_ml:300}}},edit)
  const list=await request(n,'expansion.records.list',filters)
  assert.equal(list.items[0].details.amount_ml,300);assert.equal((await request(n,'local149.pending',{})).length,2)
  online=true
  await request(n,'local149.sync',{})
  assert.deepEqual(applied,[op,edit]);assert.equal(remote.size,1);assert.equal(remote.get(nextId).details.amount_ml,300)
  assert.deepEqual(await request(n,'local149.pending',{}),[])
  // Replaying the original tapped submit must not create a second record.
  assert.equal(await request(n,'health.records.add',{category:'water',occurred_at:'2026-11-18T20:00:00+08:00',content:'250毫升',details:{amount_ml:250}},op),nextId)
  assert.deepEqual(applied,[op,edit])
})

test('清空中断后锁住资料，原账号重认证，沿用原清空编号后才清理本机',async()=>{
  const n=host();let journal='',calls=[],authOK=false,purged=false
  n.user=()=>({id:37,username:'qa-demo'})
  n.resetJournal=async(value)=>{if(value!==undefined)journal=value;return journal}
  n.rest=async(path,method,b,id)=>{calls.push(id);if(!authOK)throw Object.assign(Error('expired'),{status:401});return {reset:true,request_id:id,local_purge_required:true,session:{access_token:'not-for-web'}}}
  n.reauthorize=async(path,data,owner)=>{assert.equal(owner,37);if(data.username!=='qa-demo')throw Error('wrong original user');authOK=true}
  n.purgeLocal=async()=>{purged=true};n.clear=async()=>{n.actor=0}
  await assert.rejects(dispatchBusiness('account.reset',{_fawnsee_actor_id:37,confirmation:'清空账号内全部数据'},n),{code:'account_reset_reauthentication_required'})
  const marker=await dispatchBusiness('app.bootstrap',{},n)
  assert.equal(marker.account_data_reset_pending,true);assert.equal(marker.account_data_reset_reauthentication_required,true)
  await assert.rejects(request(n,'shopping.orders',{}),{code:'account_reset_pending'})
  await assert.rejects(dispatchBusiness('account.reset.reauthorize',{_fawnsee_actor_id:37,username:'other',password:'unused'},n))
  assert.equal(purged,false);assert.equal(calls.length,1)
  const complete=await dispatchBusiness('account.reset.reauthorize',{_fawnsee_actor_id:37,username:'qa-demo',password:'unused'},n)
  assert.equal(complete.local_purged,true);assert.equal(complete.reauthentication_required,true);assert.equal(complete.session,undefined)
  assert.equal(calls[0],calls[1]);assert.equal(journal,'');assert.equal(purged,true)
})


