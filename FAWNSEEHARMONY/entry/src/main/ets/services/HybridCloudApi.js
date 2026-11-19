// Trusted native-side adapter. Only these named member operations may reach cloud RPC.
const expansion = {
  "expansion.bootstrap": "get_bootstrap",
  "expansion.settings.get": "get_settings",
  "expansion.settings.update": "update_settings",
  "expansion.records.list": "list_records",
  "expansion.records.get": "get_record",
  "expansion.records.update": "update_record",
  "expansion.records.delete": "delete_record",
  "expansion.records.revisions": "list_record_revisions",
  "expansion.foods.list": "list_foods",
  "expansion.foods.get": "get_food",
  "expansion.foods.create": "create_food",
  "expansion.foods.favorite": "set_food_favorite",
  "expansion.foods.favorites": "list_food_favorites",
  "expansion.meals.list": "list_meals",
  "expansion.meals.get": "get_meal",
  "expansion.meals.create": "create_meal",
  "expansion.meals.update": "update_meal",
  "expansion.meals.delete": "delete_meal",
  "expansion.analyses.create": "create_analysis",
  "expansion.analyses.get": "get_analysis",
  "expansion.analyses.list": "list_analyses",
  "expansion.analyses.context": "get_analysis_context",
  "expansion.analyses.confirm": "confirm_analysis",
  "expansion.analyses.dismiss": "dismiss_analysis",
  "expansion.observations.list": "list_observations",
  "expansion.observations.get": "get_observation",
  "expansion.observations.create": "create_observation",
  "expansion.observations.update": "update_observation",
  "expansion.observations.delete": "delete_observation",
  "expansion.goals.list": "list_goals",
  "expansion.goals.get": "get_goal",
  "expansion.goals.create": "create_goal",
  "expansion.goals.update": "update_goal",
  "expansion.goals.close": "close_goal",
  "expansion.goals.delete": "delete_goal",
  "expansion.goals.checkin": "checkin_goal",
  "expansion.goals.uncheckin": "uncheckin_goal",
  "expansion.growth.summary": "get_growth",
  "expansion.points.ledger": "list_points",
  "expansion.invites.get": "get_invite_code",
  "expansion.invites.create": "create_invite_code",
  "expansion.invites.accept": "accept_invite",
  "expansion.content.list": "list_content",
  "expansion.content.get": "get_content",
  "expansion.content.progress": "get_content_progress",
  "expansion.content.progress.update": "update_content_progress",
  "expansion.ai.scenes": "list_ai_scenes"
}
const fixed = {
  'health.dashboard':['health','get_dashboard'], 'health.records.list':['health','list_life_records','*'],
  'health.records.add':['health','add_life_record','*'], 'health.records.delete':['health','delete_life_records','record_ids'],
  'health.statistics':['health','get_record_statistics','*'], 'health.service-summary':['health','get_service_summary'],
  'health.profile.get':['health','get_profile'], 'health.profile.save':['health','save_profile','$'],
  'health.reminders.list':['health','list_reminders'], 'health.reminders.add':['health','add_reminder','*'],
  'health.reminders.update':['health','update_reminder','reminder_id','*'], 'health.reminders.toggle':['health','toggle_reminder','reminder_id','enabled'],
  'health.reminders.delete':['health','delete_reminders','reminder_ids'],
  'chat.conversations.list':['chat','list_conversations'], 'chat.conversations.create':['chat','create_conversation','title'],
  'chat.conversations.rename':['chat','rename_conversation','conversation_id','title'], 'chat.conversations.delete':['chat','delete_conversations','conversation_ids'],
  'chat.message.attachments':['chat','list_message_attachments','message_id'], 'chat.greeting.ensure':['chat','ensure_greeting','*'],
  'preferences.get':['preferences','get_preferences'], 'preferences.update':['preferences','update_preferences','$'],
  'service.appointments':['service_management','list_appointments'], 'service.appointments.request':['service_management','request_appointment','*'],
  'service.history':['service_management','list_visit_records','*'], 'service.tasks':['service_management','list_visit_tasks','*'],
  'ai.config':['ai','get_ai_config'],
  'shopping.products':['commerce','list_products','*'], 'shopping.cart':['commerce','list_cart'],
  'shopping.cart.add':['commerce','add_to_cart','product_id','quantity'], 'shopping.cart.set':['commerce','set_cart_quantity','product_id','quantity'],
  'shopping.favorites':['commerce','list_favorites'], 'shopping.favorite.set':['commerce','set_favorite','product_id','favorite'],
  'shopping.checkout':['commerce','checkout_cart'], 'shopping.orders':['commerce','list_orders'], 'shopping.recommendations':['commerce','list_recommendations'],
  'shopping.recommendation.view':['commerce','record_product_view','recommendation_id'],
  'shopping.recommendation.interested':['commerce','mark_recommendation_interested','recommendation_id'],
  'shopping.product.interested':['commerce','mark_product_interested','product_id'],
  'forum.topics':['forum','list_topics','query'], 'forum.posts':['forum','list_posts','topic','limit'],
  'forum.posts.comments':['forum','list_comments','post_id','limit'], 'forum.posts.like':['forum','toggle_like','post_id'],
  'files.get':['files','get_file','file_id']
}
const rest = {
  'member.sync':['GET','/v1/member/sync'], 'member.capabilities':['GET','/v1/member/capabilities'],
  'onboarding.get':['GET','/v1/member/onboarding'], 'onboarding.save':['PATCH','/v1/member/onboarding'],
  'onboarding.suggest':['POST','/v1/member/onboarding/suggest'], 'onboarding.complete':['POST','/v1/member/onboarding/complete'],
  'onboarding.tutorial.complete':['POST','/v1/member/onboarding/tutorial-complete'],
  'account.metadata.get':['GET','/v1/member/account-metadata'], 'account.usage.heartbeat':['POST','/v1/member/usage/heartbeat'],
  'reading.catalog':['GET','/v1/member/reading/catalog'], 'reading.bookmarks':['GET','/v1/member/reading/bookmarks'],
  'reading.comments':['GET','/v1/member/reading/comments'], 'reading.recommendations':['GET','/v1/member/reading/recommendations'],
  'reading.get':['GET','/v1/member/reading/articles/:article_id'], 'reading.asset':['GET','/v1/member/reading/assets/:asset_id'],
  'reading.social.get':['GET','/v1/member/reading/social/:article_id'],
  'forum.discovery.search':['GET','/v1/member/forum/discovery'], 'forum.member.profile':['GET','/v1/member/forum/profile/:post_id'],
  'forum.image.get':['GET','/v1/member/forum/images/:file_id'], 'chat.image.get':['GET','/v1/member/chat/images/:attachment_id'],
  'forum.detail.get':['GET','/v1/member/forum/detail/:post_id'], 'forum.detail.comments':['GET','/v1/member/forum/detail/:post_id/comments'],
  'forum.detail.like':['PUT','/v1/member/forum/detail/:post_id/like'], 'forum.detail.bookmark':['PUT','/v1/member/forum/detail/:post_id/bookmark'],
  'forum.detail.follow':['PUT','/v1/member/forum/detail/:post_id/follow'], 'forum.detail.comment':['POST','/v1/member/forum/detail/:post_id/comments'],
  'member.appointments.update':['PATCH','/v1/member/appointments/:appointment_id'],
  'chat.tools.proposals':['GET','/v1/ai/tools/proposals'], 'chat.tools.confirm':['POST','/v1/ai/tools/confirm'],
  'chat.tools.cancel':['POST','/v1/ai/tools/cancel'], 'chat.record_tasks.list':['GET','/v1/ai/record-tasks'],
  'chat.record_tasks.focus':['POST','/v1/ai/record-tasks/:task_id/focus'],
  'member.ai-profile.get':['GET','/v1/member/memory/ai-profile'], 'member.ai-profile.save':['PUT','/v1/member/memory/ai-profile'],
  'member.ai-profile.categories.get':['GET','/v1/member/memory/ai-profile/categories'], 'member.ai-profile.categories.save':['PUT','/v1/member/memory/ai-profile/categories'],
  'member.ai-profile.categories.refresh':['POST','/v1/member/memory/ai-profile/categories/:domain/refresh'],
  'member.memory.settings.get':['GET','/v1/member/memory/settings'], 'member.memory.conversation.status':['GET','/v1/member/memory/conversations/status'],
  'member.memory.controls.update':['PATCH','/v1/member/memory/controls'], 'member.memory.forget':['POST','/v1/member/memory/forget'],
  'member.memory.events.append':['POST','/v1/member/memory/events'], 'member.memory.overview':['GET','/v1/member/memory/overview'],
  'member.memory.days':['GET','/v1/member/memory/days'], 'member.memory.preferences.list':['GET','/v1/member/memory/preferences'],
  'member.memory.preferences.summarize':['POST','/v1/member/memory/preferences/summarize'],
  'member.memory.preference.feedback':['POST','/v1/member/memory/preferences/:key/feedback'],
  'ai.operations.status':['GET','/v1/ai/operations/:operation_id'], 'ai.operations.cancel':['POST','/v1/ai/operations/:operation_id/cancel'],
  'library.catalog':['GET','/v1/library/catalog/:kind'], 'library.search':['GET','/v1/library/search'], 'library.card':['GET','/v1/library/card/:kind/:document_id'],
  'library.source':['GET','/v1/library/source/:document_id']
}
export function failure(code, message) {
  const e=new Error(message); e.code=code; return e
}
export function memberBody(payload, actor, requireActor=true) {
  if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw failure('validation','请求内容无效。')
  for(const key of ['actor_id','actor_user_id','user_id','member_user_id','member_id','base_url','api_key','path','__proto__','constructor','prototype']) {
    if (Object.hasOwnProperty.call(payload,key)) throw failure('permission','请求不能指定账号或连接地址。')
  }
  if ((requireActor || '_fawnsee_actor_id' in payload) && payload._fawnsee_actor_id!==actor) throw failure('account_changed','账号已改变，请重新打开页面。')
  const b={...payload}; delete b._fawnsee_actor_id; return b
}
function pathValue(value) {
  if(typeof value!=='string' && typeof value!=='number')throw failure('validation','缺少资源编号。')
  const s=String(value); if(!s || s==='.' || s==='..' || /[\\/\x00-\x1f]/.test(s))throw failure('validation','资源编号无效。')
  return encodeURIComponent(s)
}
function query(body) {
  return Object.entries(body).filter(([,v])=>v!==undefined && v!==null).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(String(v))).join('&')
}
export async function runMember(action, input, owner, op, rpc, cloud) {
  const b={...input}
  if (Object.hasOwnProperty.call(expansion,action)) {
    if(action==='expansion.analyses.create' && b.kind!=='nutrition_check')throw failure('permission','手机端仅开放饮食教育整理。')
    delete b.request_id
    return rpc('expansion',expansion[action],[owner,b],{},op)
  }
  if (Object.hasOwnProperty.call(fixed,action)) {
    const [service,method,...fields]=fixed[action], args=[owner]
    let kwargs={}
    if(action==='chat.conversations.create') b.title ??= '新对话'
    if(action==='shopping.cart.add') b.quantity ??= 1
    if(action==='forum.topics') b.query ??= ''
    if(action==='forum.posts') b.limit ??= 50
    if(action==='forum.posts.comments') b.limit ??= 100
    for(const field of fields) {
      if(field==='*') kwargs=b
      else if(field==='$') args.push(b)
      else {if(b[field]===undefined)throw failure('validation','请求缺少必要信息。');args.push(b[field]);delete b[field]}
    }
    const value=await rpc(service,method,args,kwargs,op)
    if(action==='files.get' && typeof value.content==='string')return {...value, content:{encoding:'base64',value:value.content}}
    return value
  }
  if(Object.hasOwnProperty.call(rest,action)) {
    let [method,path]=rest[action]
    if(action.startsWith('library.') && action!=='library.source') b.kind ??='knowledge'
    if(action==='library.search' && b.query!==undefined) {b.q=b.query;delete b.query}
    if(action==='chat.tools.proposals') Object.assign(b,{record_dialogue:'try1.2-dialogue-v3',record_photo_attachment:'try1.2-photo-v1'})
    path=path.replace(/:(\w+)/g,(_,key)=>{const val=pathValue(b[key]);delete b[key];return val})
    const suffix=action==='library.source'?(b.anchors||[]).slice(0,20).map(a=>'anchor='+encodeURIComponent(String(a).slice(0,160))).join('&'):method==='GET'?query(b):''
    const value=await cloud(path+(suffix?'?'+suffix:''),method,method==='GET'?undefined:b,op)
    if(action==='library.catalog')return value.documents
    if(action==='library.search')return value.hits
    return value
  }
  if(action==='forum.posts.create' || action==='forum.posts.comment'){
    const post=action==='forum.posts.create'; const args=[owner,post?b.topic:b.post_id,b.content||'']
    if(b.attachment_ids?.length)args.push(b.attachment_ids)
    return rpc('forum',post?'create_post':'add_comment',args,{},op)
  }
  if(action==='chat.files.list') {
    const rows=await rpc('files','list_files',[owner],{},op)
    return rows.slice(0,200).map(({id,original_name,media_type,size_bytes,created_at})=>({id,original_name,media_type,size_bytes,created_at}))
  }
  if(action==='files.upload') {
    const content=String(b.content||'').replace(/^data:[^,]{1,160};base64,/i,'')
    if (!content || content.length>20971520 || !/^[A-Za-z0-9+/]+={0,2}$/.test(content) || content.length%4!==0)throw failure('validation','文件内容或大小无效。')
    return rpc('files','upload_bytes',[owner,b.original_name,{$__type:'bytes',value:content}],{},op)
  }
  if(action==='chat.record_tasks.capability') {
    const row=await cloud('/v1/ai/tools/capabilities','GET',undefined,op);return {record_tasks:row.record_tasks==='try1.2-tasks-v1'}
  }
  if(['reading.share','reading.social.set','reading.comment.add','reading.comment.like'].includes(action)){
    const account=await cloud('/v1/member/local-sync/account-state','GET',undefined,op)
    if(!account.data_generation)throw failure('account_changed','无法核对账号，请重新登录。')
    const state=await cloud('/v1/member/local-sync/state?resource=reading_social&data_generation='+encodeURIComponent(account.data_generation),'GET',undefined,op)
    const receipt=await cloud('/v1/member/local-sync/operations','POST',{action,operation_id:op,base_version:state.version,data_generation:account.data_generation,payload:b},op)
    if(!['applied','duplicate'].includes(receipt.status))throw failure('conflict',receipt.message||'保存结果需重新核对。')
    return receipt.result
  }
  throw failure('feature_unavailable','此操作当前未在鸿蒙端开放。')
}

