import { failure, memberBody, runMember } from './HybridCloudApi.js'
import { journalAction, runJournal } from './HybridJournalApi.js'
import { pendingReset, resetAction, resetBootstrap } from './HybridResetApi.js'
import { assistantAction } from './HybridAssistantApi.js'

let guestEpoch=''
const activeChats=new Map()
const inFlightPhotos=new Map()
const prefNames={ai_context_consent:'mobile_ai_library_enabled',ai_personal_context_consent:'mobile_ai_personal_enabled',ai_thinking_enabled:'mobile_ai_thinking_enabled',ai_reasoning_effort:'mobile_ai_reasoning_effort',ai_web_enabled:'mobile_ai_web_enabled',ai_tools_enabled:'mobile_ai_tools_enabled'}
const guestAllowed=new Set(['chat.greeting.ensure','preferences.get','preferences.update','chat.conversations.list','chat.conversations.create','chat.conversations.rename','chat.conversations.delete','chat.messages.list','chat.message.attachments','files.upload','files.get','files.delete','chat.files.list','chat.image.get','chat.send','chat.tools.proposals','ai.operations.status','ai.operations.cancel','guest.snapshot.refresh','guest.memory.get','guest.memory.update','library.search','library.card','library.document','library.source'])
const privacyError=()=>failure('consent_required','使用此功能需要在隐私设置中同意相应用途。')
// Startup prepares these pages together. Return a locked projection until the member opens a protected page.
function privatePrefetch(action,b,owner){
  if(action==='health.profile.get')return {user_id:owner,consent_required:true}
  if(action==='health.reminders.list')return []
  if(['expansion.records.list','expansion.observations.list','expansion.goals.list','expansion.meals.list'].includes(action))return {items:[],total:0,offset:b.offset||0,limit:b.limit||100,consent_required:true}
  return undefined
}
const cachedStartup=new Set(['health.profile.get','health.reminders.list','expansion.goals.list','expansion.bootstrap','expansion.settings.get','reading.catalog','reading.recommendations','shopping.products','shopping.cart','shopping.favorites','shopping.orders','shopping.recommendations','service.appointments','member.capabilities','onboarding.get'])
async function withStartupCache(a,b,n,run){
  if(!cachedStartup.has(a)||!n.scope())return run()
  const key='startup:'+a+':'+JSON.stringify(b)
  try{
    const result=await run()
    await n.recordCache(key,'set',JSON.stringify(result)).catch(()=>{})
    return result
  }catch(e){
    if(!n.networkError(e)&&![502,503,504].includes(e.status))throw e
    const raw=await n.recordCache(key,'get','')
    if(raw)return JSON.parse(raw)
    throw e
  }
}
function opId(id,n){if(id===undefined||id===null||id==='')return n.id();if(typeof id!=='string'||!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(id))throw failure('validation','操作编号无效。');return id}
function descriptor(n) {return {enabled:true,id:n.guestIdentity(false),session_epoch:guestEpoch||(guestEpoch=n.id()),cleanup_pending:false}}
function bootstrap(n){const user=n.user();return {app:{name:'凡思',transport:'arkweb'},cloud:{configured:true,authenticated:!!user,endpoint:'https://39.106.166.15/aliyun'},user,guest:user?null:descriptor(n),data_generation:user?n.scope():''}}
function requireActor(n){if(!n.owner())throw failure('not_authenticated','请先登录会员账号。');return n.owner()}
function preferencesView(value) {
  const out={...value}
  for(const [web,native] of Object.entries(prefNames)) {out[web]=out[native]??(web==='ai_reasoning_effort'?'high':true);delete out[native]}
  delete out.ai_library_enabled;return out
}
async function authorize(action,n) {
  if(['mobile.weather.current','weather.current'].includes(action))return
  const ai=action==='chat.send'||action==='mobile.image.analyze'||action==='ai.statistics.explain'||action.startsWith('voice.')||action==='expansion.analyses.run'||action==='member.ai-profile.categories.refresh'||action==='member.memory.preferences.summarize'
  if(ai && !(await n.consent('ai')))throw privacyError()
  if(action==='files.upload' && !n.allowed('health') && !n.allowed('ai') && !(await n.consent('health')))throw privacyError()
  const health=/^(health\.(records|profile|reminders|statistics|dashboard)|expansion\.(records|observations|meals|goals|analyses)|chat\.records\.)/.test(action)||action.startsWith('record.photo.')
  if(health && !n.allowed('health')) {
    if(action!=='record.photo.get' && /(\.get|\.list|\.statistics|\.dashboard|\.revisions)$/.test(action))throw privacyError()
    if(!(await n.consent('health')))throw privacyError()
  }
}
async function saveLocal(n,key,value){await n.scopedFile(key,'set',JSON.stringify(value));return value}
async function readLocal(n,key,fallback){const raw=await n.scopedFile(key,'get','');return raw?JSON.parse(raw):fallback}
async function draft(action,b,n) {
  if(typeof b.key!=='string'||!b.key||b.key.length>500)throw failure('validation','草稿标识无效。')
  const attachment=action.startsWith('local149.draft-attachment.'), key=(attachment?'attachment:':'draft:')+b.key
  const empty=attachment?null:{text:''}
  if(action.endsWith('.get'))return readLocal(n,key,empty)
    if(action.endsWith('.delete')) {await n.scopedFile(key,'delete','');return {discarded:true}}
  if(action.endsWith('.save')){
    if(!attachment && (typeof b.text!=='string'||b.text.length>65536))throw failure('validation','草稿文字过长。')
    return saveLocal(n,key,attachment?b.attachment:{text:b.text})
  }
  throw failure('permission','未开放此本机操作。')
}
function imagePreview(id,image){return {file_id:id,media_type:image.media_type,size_bytes:image.size_bytes,src:'data:'+image.media_type+';base64,'+image.data_base64}}
async function photo(action,b,n,owner) {
  if(action==='record.photo.stage'){
    const i=b.image
    if(!i||!['image/jpeg','image/png','image/webp'].includes(i.media_type)||!Number.isInteger(i.size_bytes)||i.size_bytes<1||i.size_bytes>2*1024*1024||typeof i.data_base64!=='string'||i.data_base64.length!==Math.ceil(i.size_bytes/3)*4||!/^[A-Za-z0-9+/]+={0,2}$/.test(i.data_base64))throw failure('validation','请选择不超过 2 MB 的有效照片。')
    await n.checkPhoto(i.data_base64,i.media_type,i.size_bytes)
    const nonce=n.id(),id=-(parseInt(nonce.replace(/-/g,'').slice(0,13),16)||1)
    await saveLocal(n,'photo:'+id,{image:i,operation:nonce})
    return imagePreview(id,i)
  }
  const id=b.file_id
  if(!Number.isSafeInteger(id)||!id)throw failure('validation','照片编号无效。')
  if(id<0){
    const key='photo:'+id
    if(action==='record.photo.discard'){await n.scopedFile(key,'delete','');return {discarded:true}}
    const local=await readLocal(n,key,null);if(!local)throw failure('not_found','本机没有这张照片。')
    return imagePreview(id,local.image)
  }
  if(action==='record.photo.discard')return {discarded:false}
  try{
    const row=await n.rpc('files','get_file',[owner,id],{},n.id())
    if(row.id!==id||row.user_id!==owner||!['image/jpeg','image/png','image/webp'].includes(row.media_type))throw failure('not_found','照片无法读取。')
    const result=imagePreview(id,{...row,data_base64:row.content})
    if(n.scope())await n.recordCache('photo:'+id,'set',JSON.stringify(result)).catch(()=>{})
    return result
  }catch(e){
    if(!n.networkError(e)||!n.scope())throw e
    const raw=await n.recordCache('photo:'+id,'get','');if(!raw)throw e;return JSON.parse(raw)
  }
}
async function cloudPhoto(id,owner,n){
  if(Number.isSafeInteger(id)&&id>0)return id
  if(!Number.isSafeInteger(id)||id>=0)throw failure('validation','照片编号无效。')
  const key=owner+':'+id
  if(inFlightPhotos.has(key))return inFlightPhotos.get(key)
  const pending=(async()=>{
    const local=await readLocal(n,'photo:'+id,null)
    if(!local)throw failure('not_found','待保存的照片未找到。')
    if(local.remote)return local.remote
    const i=local.image, name='凡思记录照片-'+local.operation+'.'+({'image/png':'png','image/jpeg':'jpg','image/webp':'webp'}[i.media_type])
    const remote=await n.rpc('files','upload_bytes',[owner,name,{$__type:'bytes',value:i.data_base64}],{},local.operation)
    if(n.owner()!==owner||!Number.isSafeInteger(remote)||remote<1)throw failure('account_changed','无法核对照片归属。')
    await saveLocal(n,'photo:'+id,{...local,remote})
    if(n.scope())await n.recordCache('photo:'+remote,'set',JSON.stringify(imagePreview(remote,i))).catch(()=>{})
    return remote
  })().finally(()=>inFlightPhotos.delete(key))
  inFlightPhotos.set(key,pending);return pending
}
async function mapPhotos(b,owner,n) {
  const visit=async v=>{
    if(!v||typeof v!=='object')return
    for(const [k,item] of Object.entries(v)){
      if(k==='photo_file_ids' && Array.isArray(item))v[k]=await Promise.all(item.map(id=>cloudPhoto(id,owner,n)))
      else if(typeof item==='object')await visit(item)
    }
  }
  await visit(b)
}
async function chatAction(a,b,n,op,scope) {
  if(a==='chat.send'){
    if(activeChats.has(scope))throw failure('busy','当前对话仍在回答，请稍后。')
    const activity={id:op,stopRequested:false}
    activeChats.set(scope,activity)
    try{return await n.chat(b,op)}
    catch(error){
      const currentScope=()=>n.owner()||n.guestIdentity(false)
      if(activity.stopRequested&&currentScope()===scope){
        let state
        try{
          state=n.owner()?await n.rest('/v1/ai/operations/'+op+'?kind=chat.send','GET',undefined,n.id()):
            await n.guest('ai.operations.status',{operation_id:op,kind:'chat.send'},n.id())
        }catch{/* Keep the original failure when the cloud outcome is unknown. */}
        if(currentScope()===scope&&state?.operation_id===op&&state.kind==='chat.send'&&state.state==='cancelled'&&
          (state.conversation_id==null||Number(state.conversation_id)===Number(b.conversation_id))){
          throw failure('cancelled','已停止本次回答。')
        }
      }
      throw error
    }finally{if(activeChats.get(scope)===activity)activeChats.delete(scope)}
  }
  if(a==='chat.messages.list')return n.history(b.conversation_id||0,b.before_sequence_no||0,b.limit??40)
  const save=a==='chat.records.confirm'
  if(!save&&a!=='chat.records.cancel')throw failure('permission','核对操作无效。')
  const receipt=await n.decide(b.assistant_message_id,opId(b.draft_id,n),save)
  if(receipt.status!==(save?'saved':'cancelled'))throw failure('conflict','草稿已处理，请刷新查看原操作结果。')
  return receipt
}
async function finishLogin(n){
  await n.guestCleanupPending(true)
  try{
    await n.retryGuestCleanup();await n.purgeLocal(0)
    n.guestIdentity(true);guestEpoch='';await n.guestCleanupPending(false)
  }catch(e){throw failure('guest_cleanup_pending','登录已验证，体验资料清理尚未完成。请继续清理。')}
}
async function auth(action,p,n) {
  if(action==='auth.logout'){await n.logout();n.guestIdentity(true);guestEpoch='';return {logged_out:true}}
  if(action==='auth.login'||action==='auth.register') {
    if(typeof p.username!=='string'||typeof p.password!=='string'||p.password.length<1||p.password.length>256)throw failure('validation','请填写账号和密码。')
    if(n.owner())await n.clear()
    if(action==='auth.register')await n.register(p.username,p.password)
    try{await n.login('/v1/auth/login',{username:p.username,password:p.password},p.auto_login===true)}
    catch(e){if(action==='auth.register')throw failure('registration_login_required','账号已创建，请使用账号密码登录。');throw e}
    await finishLogin(n);return n.user()
  }
  if(action==='auth.phone.send'){
    const b={phone:p.phone,purpose:p.purpose}
    if(['bind','change'].includes(p.purpose)){requireActor(n);return n.rest('/v1/auth/phone/send','POST',b,n.id())}
    return n.sms(b)
  }
  if(action==='auth.phone.quick-entry'||action==='auth.phone.entry'||action==='auth.phone.login'){
    if(n.owner())await n.clear()
    const out=await n.phoneLogin({phone:p.phone,code:p.code,challenge_id:p.challenge_id},p.auto_login===true,action)
    if(out.outcome==='register')return out
    await finishLogin(n);return action==='auth.phone.login'?n.user():out
  }
  if(action==='auth.register.complete'||action==='auth.register.verified'){
    if(n.owner())await n.clear()
    const verified=action==='auth.register.verified'
    const body=verified?{username:p.username,password:p.password,registration_token:p.registration_token}:{phone:p.phone,code:p.code,challenge_id:p.challenge_id,username:p.username,password:p.password}
    await n.login(verified?'/v1/auth/register/verified':'/v1/auth/register/complete',body,p.auto_login===true)
    await finishLogin(n);return n.user()
  }
  requireActor(n)
  if(action==='auth.me')return n.user()
  if(action==='auth.credentials.get')return n.rest('/v1/auth/credentials','GET',undefined,n.id())
  if(action==='auth.credentials.complete'){await n.credentials({username:p.username,password:p.password,request_id:p.request_id});return n.rest('/v1/auth/credentials','GET',undefined,n.id())}
  if(action==='auth.phone.get')return n.rest('/v1/auth/phone','GET',undefined,n.id())
  if(action==='auth.phone.bind')return n.rest('/v1/auth/phone/bind','POST',{phone:p.phone,code:p.code,challenge_id:p.challenge_id,current_password:p.current_password},n.id())
  if(action==='auth.change-password')return n.rest('/v1/auth/change-password','POST',{current_password:p.current_password,new_password:p.new_password},n.id())
  throw failure('permission','未开放此登录操作。')
}
export async function dispatchBusiness(action,p,n) {
  if(!p||Array.isArray(p)||typeof p!=='object')throw failure('validation','请求内容无效。')
  await n.restore()
  const reset=await pendingReset(n)
  const awaitingCleanup=await n.guestCleanupPending()
  if(['app.bootstrap','bootstrap','auth.state'].includes(action)){
    if(reset)return resetBootstrap(reset,n.user())
    if(awaitingCleanup)return {user:null,guest:{...descriptor(n),cleanup_pending:true},app:{name:'凡思',transport:'arkweb'},cloud:{configured:true,authenticated:false}}
    return bootstrap(n)
  }
  if(['account.reset','account.reset.reauthorize','account.reset.phone.send'].includes(action))return resetAction(action,p,n,reset)
  if(reset)throw failure('account_reset_pending','请先完成此账号的数据清理。')
  if(action==='guest.cleanup.retry'){if(awaitingCleanup)await finishLogin(n);return {completed:true}}
  if(awaitingCleanup)throw failure('guest_cleanup_pending','登录已验证，请先继续完成体验资料清理。')
  if(action.startsWith('auth.'))return auth(action,p,n)
  if(action==='native.cancelChat'){
    const owner=n.owner(), scope=owner||n.guestIdentity(false), op=opId(p.operation_id,n)
    const activity=activeChats.get(scope)
    if(activity?.id!==op)throw failure('not_found','本次回答已结束。')
    activity.stopRequested=true
    const body={operation_id:op,kind:'chat.send'}
    return owner?n.rest('/v1/ai/operations/'+op+'/cancel','POST',{kind:'chat.send'},n.id()):n.guest('ai.operations.cancel',body,n.id())
  }
  if(action==='guest.request'){
    const scope=n.guestIdentity(false)
    if(n.owner()||scope!==p.guest_id)throw failure('account_changed','体验身份已改变。')
    const a=p.action,b=memberBody(p.payload||{},scope,false),op=opId(p.request_id,n)
    if(a.startsWith('local149.draft'))return draft(a,b,n)
    if(!guestAllowed.has(a))throw failure('login_required','此功能需要登录后使用。')
    await authorize(a,n)
    if(n.owner()||scope!==n.guestIdentity(false))throw failure('account_changed','体验身份已改变。')
    if(a==='chat.send')return chatAction(a,b,n,op,scope)
    const value=a==='guest.snapshot.refresh'?{refreshed:await n.refreshedGuest()}:await n.guest(a,b,op)
    if(n.owner()||scope!==n.guestIdentity(false))throw failure('account_changed','体验身份已改变。')
    if(a==='chat.messages.list')value.next_cursor=value.next_cursor??value.next_before_sequence_no
    return value
  }
  const owner=requireActor(n)
  if(action.startsWith('record.photo.')) {
    if(!['record.photo.stage','record.photo.get','record.photo.discard'].includes(action))throw failure('permission','未开放此照片操作。')
    const b=memberBody(p,owner);await authorize(action,n);if(n.owner()!==owner)throw failure('account_changed','账号已改变。')
    return photo(action,b,n,owner)
  }
  if(action==='avatar.local.get'||action==='avatar.local.save') {
    const b=memberBody(p,owner)
    if(action.endsWith('.get'))return readLocal(n,'avatar',{avatar_data:'',sync_pending:false})
    if(typeof b.avatar_data!=='string'||b.avatar_data.length>3600000||b.avatar_data&&!/^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/]+=*$/.test(b.avatar_data)||typeof b.sync_pending!=='boolean')throw failure('validation','头像格式无效。')
    return saveLocal(n,'avatar',{avatar_data:b.avatar_data,sync_pending:b.sync_pending})
  }
  if(action==='account.erase'){
    if(p.confirmation!=='删除账号及全部数据'||!p.current_password)throw failure('validation','请核对密码和注销确认文字。')
    const op=opId(p.request_id,n),r=await n.rest('/v1/auth/account/erase','POST',{...p,request_id:op},op)
    if(r.erased===true&&r.local_purge_required===true){await n.purgeLocal(owner);await n.clear()}
    return {erased:r.erased===true,local_purge_required:r.local_purge_required===true,local_purged:r.erased===true&&r.local_purge_required===true}
  }
  if(action==='member.capabilities'||action==='getcapabilities')return n.rest('/v1/member/capabilities','GET',undefined,n.id())
  if(!['member.request','request'].includes(action))throw failure('permission','未开放此操作。')
  const a=p.action
  if(typeof a!=='string')throw failure('validation','操作名无效。')
  const b=memberBody(p.payload||{},owner),op=opId(p.request_id||b.request_id,n)
  if(b.request_id && b.request_id!==op)throw failure('conflict','操作编号不一致。')
  if(!n.allowed('health')){
    const locked=privatePrefetch(a,b,owner)
    if(locked!==undefined)return locked
  }
  await authorize(a,n)
  if(owner!==n.owner())throw failure('account_changed','账号已改变。')
  if(a.startsWith('local149.draft'))return draft(a,b,n)
  if(['chat.send','chat.messages.list','chat.records.confirm','chat.records.cancel'].includes(a)){
    const value=await chatAction(a,b,n,op,owner);if(a==='chat.records.confirm')await n.reminders();return value
  }
  if(['ai.statistics.explain','mobile.image.analyze','expansion.analyses.run','voice.polish','voice.records.propose','voice.smart-record','voice.records.confirm'].includes(a)){
    const value=await assistantAction(a,b,n,op,owner);if(a==='voice.records.confirm')await n.reminders();return value
  }
  if(a==='auth.me'||a==='auth.change-password')return auth(a,b,n)
  if(a==='preferences.get'||a==='preferences.update'){
    const patch={}
    for(const [k,v] of Object.entries(b)) {
      if(k==='request_id')continue
      if(Object.values(prefNames).includes(k))throw failure('validation','设置字段无效。')
      patch[prefNames[k]||k]=v
    }
    const get=a.endsWith('.get'),key='startup:preferences'
    try{
      const result=preferencesView(await n.rpc('preferences',get?'get_preferences':'update_preferences',get?[owner]:[owner,patch],{},op))
      if(n.scope())await n.recordCache(key,'set',JSON.stringify(result)).catch(()=>{})
      return result
    }catch(e){
      if(!get||(!n.networkError(e)&&![502,503,504].includes(e.status))||!n.scope())throw e
      const raw=await n.recordCache(key,'get','');if(!raw)throw e;return JSON.parse(raw)
    }
  }
  if(a==='mobile.weather.current'||a==='weather.current') {
    if(!n.allowed('weather'))return {available:false,location_available:false}
    return n.weatherPacket()
  }
  if(a==='health.membership.activate')throw failure('feature_unavailable','云端未开放会员自助开通，请联系服务人员。')
  const execute=async(name,payload,id)=>{
    const data=JSON.parse(JSON.stringify(payload))
    if(['health.records.add','expansion.records.update','expansion.meals.update','expansion.meals.create'].includes(name))await mapPhotos(data,owner,n)
    return runMember(name,data,owner,id,(...args)=>n.rpc(...args),(...args)=>n.rest(...args))
  }
  if(journalAction(a))return runJournal(a,b,op,n,owner,execute)
  const result=await withStartupCache(a,b,n,()=>execute(a,b,op))
  if(owner!==n.owner())throw failure('account_changed','账号已改变。')
  if(/^(health\.reminders\.(add|update|delete|toggle)|expansion\.goals\.(create|update|close|checkin|uncheckin|delete)|health\.records\.delete|chat\.tools\.confirm)$/.test(a))await n.reminders()
  return result
}
