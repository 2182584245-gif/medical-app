import { common } from '@kit.AbilityKit'
import { promptAction } from '@kit.ArkUI'
import { preferences } from '@kit.ArkData'
import { fileIo as fs } from '@kit.CoreFileKit'
import { cryptoFramework } from '@kit.CryptoArchitectureKit'
import { util } from '@kit.ArkTS'
import { image } from '@kit.ImageKit'
import { actorId, currentMember, recordCacheScope, restoreSession, cloud, rpc, guestAction, purgeGuest, signIn, signInInfo, stagedPhoneEntry, registerAccount, clearSession, signOut, completeCredentials, sendCodeInfo, operationId, CloudError, CloudNetworkError } from './Cloud'
import { requestHybridConsent } from './HybridConsent'
import { optionalAllowed, OptionalUse } from './PrivacyChoices'
import { syncDeviceReminders, clearDeviceReminders } from './DeviceReminders'
import { todayWeather } from '../pages/TodayWeather'
import { ChatOperation, Data, sendChat, loadMessages, decisionStore, decideRecord, guardedRecordCards, array, stringValue } from '../pages/AssistantService'
import { refersToConfirmed, receiptCandidate, confirmedLocator } from './HybridRecordContext.js'
import { AssistantFile, prepareAssistantFile } from '../pages/AssistantDocument'
import { VoiceRecord, proposeVoiceRows } from '../pages/RecordsVoice'
import { removeMemberDrafts } from './PrivateData'
import { clearHybridPrivate, hybridPrivate } from './HybridPrivateCloud'
import { readRecordSnapshot, writeRecordSnapshot, recordCacheEpoch, clearRecordSnapshots } from './RecordsCacheStore'
import { dispatchBusiness } from './HybridBusinessApi.js'

type BusinessEmit = (event:string, value:ESObject)=>void
type Body = Record<string,Object>
let boot:Promise<boolean>|undefined
let activeReads=0
let cooldownUntil=0
const readQueue:(()=>void)[]=[]
function grantReads():void{
  const limit=Date.now()<cooldownUntil?1:2
  while(activeReads<limit&&readQueue.length){
    const next=readQueue.shift()
    if(next){activeReads++;next()}
  }
}
async function limitedRead<T>(work:()=>Promise<T>):Promise<T>{
  const owner=actorId()
  for(let attempt=0;attempt<3;attempt++){
    await new Promise<void>((resolve:()=>void)=>{readQueue.push(resolve);grantReads()})
    try{
      if(owner!==actorId())throw Error('账号状态已改变。')
      return await work()
    }catch(e){
      if(!(e instanceof CloudError)||![502,503,504].includes(e.status)||attempt===2)throw e as Error
      cooldownUntil=Date.now()+4000
    }finally{activeReads--;grantReads()}
    await new Promise<void>((resolve:()=>void)=>setTimeout(resolve,Math.round((attempt+1)*480+Math.random()*180)))
  }
  throw Error('云端读取暂不可用。')
}

class NativeApi {
  private context:common.UIAbilityContext
  private emit?:BusinessEmit
  constructor(context:common.UIAbilityContext, emit?:BusinessEmit) {this.context=context; this.emit=emit}
  owner():number{return actorId()}
  user():Object|null{
    const user=currentMember()
    if(!user)return null
    return {'id':user.id,'username':user.username,'display_name':user.display_name,'role_code':user.role_code,'account_status':user.account_status} as Body
  }
  scope():string{return recordCacheScope(actorId()).split('|')[1]||''}
  async purgeLocal(owner:number):Promise<void>{
    await removeMemberDrafts(owner)
    await clearHybridPrivate(this.context,owner)
    const folder=this.context.filesDir+'/hybrid_user_'+owner
    if(fs.accessSync(folder)){
      for(const entry of fs.listFileSync(folder))fs.unlinkSync(folder+'/'+entry)
      fs.rmdirSync(folder)
    }
  }
  id():string{return operationId()}
  networkError(error:Error):boolean{return error instanceof CloudNetworkError}
  async recordCache(key:string,mode:string,value:string):Promise<string>{
    const owner=actorId(),scope=recordCacheScope(owner),ticket=recordCacheEpoch(owner)
    if(!owner||!scope)return ''
    if(mode==='clear'){await clearRecordSnapshots(this.context,owner);return ''}
    const name=key.startsWith('photo:')?'photo:hybrid:'+key.slice(6):'hybrid:'+key
    if(mode==='set'){await writeRecordSnapshot(this.context,owner,scope,name,value,ticket);return value}
    const found=await readRecordSnapshot(this.context,owner,scope,name,ticket)
    if(owner!==actorId()||scope!==recordCacheScope(owner))throw Error('账号状态已改变。')
    return found?.data||''
  }
  async resetJournal(value?:string):Promise<string>{
    const prefs=preferences.getPreferencesSync(this.context,{name:'hybrid_account_reset_receipt'})
    if(value!==undefined){if(value)prefs.putSync('pending',value);else prefs.deleteSync('pending');await prefs.flush()}
    return prefs.getSync('pending','') as string
  }
  async reauthorize(path:string,b:Body,owner:number):Promise<void>{await signInInfo(path,b,false,owner)}
  async restore():Promise<void>{
    if(!boot)boot=actorId()?Promise.resolve(true):restoreSession().catch((err:Error)=>{boot=undefined;throw err})
    await boot
  }
  async consent(use:OptionalUse):Promise<boolean>{return requestHybridConsent(use)}
  allowed(use:OptionalUse):boolean{return optionalAllowed(use,actorId())}
  async rest(path:string,method:string,body:Body|undefined,op:string):Promise<Object>{
    const work=():Promise<Object>=>cloud<Object>(path,method,body,op)
    if(method!=='GET')return work()
    const owner=actorId(),result=await limitedRead(work)
    if(owner!==actorId())throw Error('账号状态已改变。')
    if(path.split('?')[0]==='/v1/ai/tools/proposals' && owner){
      const packet=result as Data
      if(packet['assistant_message_id'] && Array.isArray(packet['record_drafts'])){
        packet['record_drafts']=guardedRecordCards(array(packet,'record_drafts'),Number(packet['assistant_message_id']),this.context)
      }
    }
    return result
  }
  async rpc(service:string,method:string,args:(Object|null)[],kwargs:Body,op:string):Promise<Object>{
    const work=():Promise<Object>=>rpc<Object>(service,method,args,kwargs,op)
    return /^(list_|get_|context_for_provider$|storage_usage$)/.test(method)?limitedRead(work):work()
  }
  localNotice():void{try{promptAction.showToast({message:'已保存在本机，联网后将同步到账号。',duration:2500})}catch(_){}}
  async guest(action:string,b:Body,op:string):Promise<Object>{return guestAction<Object>(action,b,op)}
  async register(username:string,password:string):Promise<void>{await registerAccount(username,password)}
  async login(path:string,b:Body,auto:boolean):Promise<void>{await signIn(path,b,auto)}
  async logout():Promise<void>{await signOut(); await clearDeviceReminders(this.context)}
  async clear():Promise<void>{await clearSession(); await clearDeviceReminders(this.context)}
  async sms(body:Body):Promise<Object>{return sendCodeInfo(String(body['phone']),String(body['purpose']))}
  async phoneLogin(body:Body,auto:boolean,action:string):Promise<Object>{
    if(action==='auth.phone.entry'){
      const info=await stagedPhoneEntry(body,auto) as Body
      if(info['outcome']==='login')info['user']=this.user() as Body
      return info
    }
    const info=await signInInfo(action==='auth.phone.login'?'/v1/auth/phone/login':'/v1/auth/phone/quick-entry',body,auto)
    return {'outcome':info.outcome,'created':info.created,'credentials_set':info.credentials_set,'user':this.user()} as Body
  }
  async retryGuestCleanup():Promise<void>{await purgeGuest()}
  async refreshedGuest():Promise<boolean>{
    await guestAction<Object>('chat.conversations.list')
    return true
  }
  async voiceRows(text:string):Promise<Data[]>{
    return (await proposeVoiceRows(text)).map((r:VoiceRecord):Data=>({'category':r.category,'occurred_at':r.stamp,'content':r.content,'details':r.details} as Data))
  }
  async childId(parent:string,index:number):Promise<string>{
    const digest=cryptoFramework.createMd('SHA256')
    await digest.update({data:new util.TextEncoder().encodeInto(parent+'|voice:'+index)})
    const b=(await digest.digest()).data.slice(0,16)
    b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128
    const hex=Array.from(b).map((v:number)=>v.toString(16).padStart(2,'0')).join('')
    return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-')
  }
  async digest(text:string):Promise<string>{
    const digest=cryptoFramework.createMd('SHA256')
    await digest.update({data:new util.TextEncoder().encodeInto(text)})
    return Array.from((await digest.digest()).data).map((b:number)=>b.toString(16).padStart(2,'0')).join('')
  }
  async checkPhoto(encoded:string,mime:string,size:number):Promise<void>{
    const bytes=new util.Base64Helper().decodeSync(encoded)
    if(bytes.length!==size||size>2097152)throw Error('照片大小与原文件不一致。')
    const source=image.createImageSource(bytes.buffer as ArrayBuffer)
    try{
      const info=await source.getImageInfo()
      if(info.mimeType!==mime||info.size.width<1||info.size.height<1||info.size.width*info.size.height>50000000)throw Error('照片格式或尺寸无效。')
    }finally{await source.release()}
  }
  async credentials(b:Body):Promise<void>{await completeCredentials(b)}
  async reminders():Promise<void>{
    try{await syncDeviceReminders(this.context,false)}
    catch(e){try{promptAction.showToast({message:'提醒已保存到账号。本机暂未排程，请检查通知设置。',duration:3500})}catch(_){}}
  }
  async weatherPacket():Promise<Body>{
    const parts=(await todayWeather()).split(' · ')
    const temperature=parts.length===3?Number(parts[2].replace('°C','')):NaN
    const valid=parts.length===3&&Number.isFinite(temperature)
    const hasCity=parts.length>1&&parts[0]!=='位置暂不可用'
    return {'available':valid,'location_available':hasCity,'city':hasCity?parts[0]:'',
      'location_label':hasCity?parts[0]:'','condition':valid?parts[1]:'','temperature_c':valid?temperature:null,
      'source':'ipwho.is + MET Norway','attribution':'Weather data: MET Norway (CC BY 4.0); location: ipwho.is',
      'source_url':'https://api.met.no/','license_url':'https://creativecommons.org/licenses/by/4.0/','data_kind':'forecast'} as Body
  }
  guestIdentity(reset:boolean):string{
    const store=preferences.getPreferencesSync(this.context,{name:'hybrid_guest_descriptor'})
    let id=store.getSync('id','') as string
    if(!id||reset){id=operationId();store.putSync('id',id);store.flushSync()}
    return id
  }
  async guestCleanupPending(change?:boolean):Promise<boolean>{
    const store=preferences.getPreferencesSync(this.context,{name:'hybrid_guest_descriptor'})
    if(change!==undefined){store.putSync('pending_cleanup',change);await store.flush()}
    return store.getSync('pending_cleanup',false) as boolean
  }
  async scopedFile(key:string,mode:string,value:string):Promise<string>{
    const owner=actorId()
    const scope=owner?recordCacheScope(owner):this.guestIdentity(false)
    return hybridPrivate(this.context,scope,key,mode,value,()=>this.guestIdentity(false))
  }
  async history(conversation:number,before:number,limit:number=40):Promise<Data>{
    const owner=actorId()
    if(!Number.isSafeInteger(limit)||limit<1||limit>200)throw Error('对话读取数量无效。')
    if(!owner)return loadMessages(conversation,before)
    const body:Data={'limit':limit} as Data
    if(conversation)body['conversation_id']=conversation
    if(before)body['before_sequence_no']=before
    const page=await rpc<Data>('chat','list_messages_page',[owner],body)
    const rows=array(page,'messages'), convId=Number((page['conversation'] as Data)['id'])
    const ids=rows.filter((r:Data)=>r['role']==='assistant').map((r:Data)=>Number(r['id']))
    if(ids.length){
      const log=await rpc<Data[]>('chat','list_activity',[owner,convId],{'assistant_message_ids':ids} as Data)
      for(const item of log){const row=rows.find((r:Data)=>r['id']===item['assistant_message_id']);if(row)row['activity']=item['activity']}
      try{
        const meta=await cloud<Data>('/v1/ai/tools/proposals?conversation_id='+convId+'&assistant_message_ids='+ids.join(',')+'&record_dialogue=try1.2-dialogue-v3&record_photo_attachment=try1.2-photo-v1')
        const store=decisionStore(this.context)
        for(const item of array(meta,'messages')){
          const row=rows.find((r:Data)=>r['id']===item['assistant_message_id'])
          if(!row)continue
          for(const field of ['proposals','record_drafts','record_drafts_ready','sources','tool_steps']){
            if(item[field]!==undefined)row[field]=item[field]
          }
          row['record_drafts']=guardedRecordCards(array(item,'record_drafts'),Number(item['assistant_message_id']),this.context)
          const receipts:Data[]=[]
          for(const draft of array(item,'record_drafts')){
            const id=String(draft['id']),status=store.getSync(id,'') as string
            if(status)receipts.push(this.receipt(id,status,String(draft['category']||'')))
          }
          row['record_receipts']=receipts
        }
      }catch(e){if(owner!==actorId())throw e as Error}
    }
    if(owner!==actorId())throw Error('账号状态已改变。')
    return page
  }
  private receipt(id:string,status:string,category:string):Data{
    const target=decisionStore(this.context).getSync('target_'+id,'') as string
    return {'id':id,'status':status,'category':category,'record_id':target?Number(target.slice(1)):0} as Data
  }
  async decide(message:number,id:string,save:boolean):Promise<Data>{
    await decideRecord(message,id,save,this.context)
    const status=decisionStore(this.context).getSync(id,'') as string
    return this.receipt(id,status,'')
  }
  async chat(body:Data,id:string):Promise<Data>{
    const owner=actorId(),conversation=Number(body['conversation_id'])
    let photo='', selected:AssistantFile|undefined
    const ids=(body['attachments'] as number[])||[]
    if(ids.length>4||new Set(ids).size!==ids.length||ids.some((id:number)=>!Number.isSafeInteger(id)||id<=0))throw Error('每条消息最多支持四个不同的已上传附件。')
    const extras:AssistantFile[]=[]
    for(const fileId of ids){
      const row=owner?await rpc<Data>('files','get_file',[owner,fileId]):await guestAction<Data>('files.get',{'file_id':fileId} as Body)
      if(owner!==actorId())throw Error('账号状态已改变。')
      const encoded=typeof row['content']==='string'?String(row['content']):stringValue(row['content'] as Data,'value')
      const file=await prepareAssistantFile(this.context,stringValue(row,'original_name'),encoded,fileId)
      if(!selected){selected=file;if(file.mime.startsWith('image/'))photo='data:'+file.mime+';base64,'+file.content}
      else extras.push(file)
    }
    const op=new ChatOperation(stringValue(body,'content'),photo,stringValue(body,'record_task_id'),conversation,selected)
    if(owner && !op.task && refersToConfirmed(op.input)){
      op.lookupReceipt=async():Promise<Data>=>{
        if(!optionalAllowed('health',owner) || !optionalAllowed('ai',owner))return {}
        try{
          const page=await limitedRead(()=>this.history(conversation,0,8))
          const candidate=receiptCandidate(op.input,page,owner,conversation) as Data
          if(!candidate)return {}
          const metric=candidate['category']==='metric'
          const query:Data={}
          query[metric?'observation_id':'record_id']=candidate['id']
          const entity=await limitedRead(()=>rpc<Data>('expansion',metric?'get_observation':'get_record',[owner,query]))
          op.guard()
          if(!optionalAllowed('health',owner) || !optionalAllowed('ai',owner))return {}
          return (confirmedLocator(op.input,candidate,entity) as Data)||{}
        }catch(e){
          op.guard()
          if(e instanceof CloudNetworkError || (e instanceof CloudError && [404,502,503,504].includes(e.status)))return {}
          throw e as Error
        }
      }
      op.storeReceiptGuard=async(message:number,guard:Data):Promise<void>=>{
        op.guard()
        const store=decisionStore(this.context)
        store.putSync('guard_'+message,JSON.stringify(guard))
        await store.flush()
      }
    }
    op.additionalFiles=extras
    op.id=id
    const prefs=owner?await rpc<Data>('preferences','get_preferences',[owner]):await guestAction<Data>('preferences.get')
    for(const key of ['thinking_enabled','tools_enabled','web_search','share_context']){
      if(body[key]!==undefined && typeof body[key]!=='boolean')throw Error('本次 AI 设置无效。')
    }
    if(body['share_context']===false || !optionalAllowed('health',owner)){
      prefs['mobile_ai_personal_enabled']=false
      prefs['ai_personal_context_consent']=false
    }
    for(const key of ['thinking_enabled','tools_enabled','web_search']){
      if(body[key]===undefined)continue
      const preference=key==='web_search'?'web_enabled':key
      prefs['mobile_ai_'+preference]=body[key]
      prefs['ai_'+preference]=body[key]
    }
    if(body['reasoning_effort']!==undefined){
      const effort=String(body['reasoning_effort'])
      if(!['low','high','max'].includes(effort))throw Error('本次思考强度无效。')
      prefs['mobile_ai_reasoning_effort']=effort
      prefs['ai_reasoning_effort']=effort
    }
    const decisions=decisionStore(this.context).getSync('recent',[]) as string[]
    let sequence=0
    await sendChat(op,prefs,decisions,(event:Data)=>{
      if(owner!==actorId()||!this.emit)return
      const raw=event['stream'] as Data||event
      const type=typeof raw['delta']==='string'?'answer_delta':raw['done']===true||raw['type']==='result'?'done':String(raw['type']||'')
      if(!type)return
      const view:Data={}
      for(const key of ['id','assistant_message_id','user_message_id','delta','tool','label','detail','status','activity','sources','proposal','drafts','welcome_groups']){
        if(raw[key]!==undefined)view[key]=raw[key]
      }
      if(Array.isArray(view['drafts']) && op.exchange){
        view['drafts']=guardedRecordCards(array(view,'drafts'),Number((op.exchange['assistant_message'] as Data)['id']),this.context)
      }
      view['type']=type;view['seq']=sequence++;view['conversation_id']=conversation
      this.emit('chatStream',{'operation_id':id,'stream':view} as Body)
    })
    if(owner!==actorId())throw Error('账号状态已改变。')
    const packet=await this.history(conversation,0), messages=array(packet,'messages')
    const assistantId=op.exchange?Number((op.exchange['assistant_message'] as Data)['id']):0
    const assistant=assistantId?messages.find((r:Data)=>Number(r['id'])===assistantId):messages.slice().reverse().find((r:Data)=>r['role']==='assistant')
    if(!assistant)throw Error('回答已提交，尚未核对到消息，请重新打开对话。')
    return {'conversation':packet['conversation'],'assistant_message':assistant,'welcome_groups':packet['welcome_groups'],'user_message':op.exchange?op.exchange['user_message']:messages.slice().reverse().find((r:Data)=>r['role']==='user')} as Data
  }
}

export async function hybridBusiness(action:string, params:ESObject, context:common.UIAbilityContext, emit?:BusinessEmit):Promise<ESObject>{
  const native=new NativeApi(context,emit)
  try{return await dispatchBusiness(action,params,native)}
  catch(e){
    if(e instanceof CloudError){
      const codes:Record<string,string>={'400':'validation','401':'not_authenticated','403':'permission','404':'not_found','409':'conflict','422':'validation','429':'rate_limited','502':'cloud_unavailable','503':'cloud_unavailable','504':'timeout'}
      throw new HybridFailure(codes[String(e.status)]||'cloud_error',e.message)
    }
    if(e instanceof CloudNetworkError)throw new HybridFailure('network',e.message)
    throw e as Error
  }
}
class HybridFailure extends Error {
  code:string
  constructor(code:string,message:string){super(message);this.code=code}
}

