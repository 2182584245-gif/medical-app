import { AssistantAnswer } from './AssistantAnswer'
import { AppUpdatePanel } from './AppUpdatePanel'
import { common } from '@kit.AbilityKit'
import { syncDeviceReminders } from '../services/DeviceReminders'
import { pasteboard } from '@kit.BasicServicesKit'
import { AssistantFile,chooseAssistantFile,prepareAssistantFile } from './AssistantDocument'
import { actorId, cloud, rpc, pickPhotos,purgeGuest,guestAction } from '../services/Cloud'
import { capturePhoto } from '../services/Camera'
import { startVoice, finishVoice, cancelVoice } from '../services/Voice'
import { Data, mergeData, array, asDataUri, child, checkAnswer, ChatOperation, decideRecord, decisionStore, loadMessages, messageImage, noteError, sendChat, stringValue, uuid } from './AssistantService'
import { categoryNames,draftFields,FieldRow } from './AssistantFields'
import { RECORD_ICONS } from './RecordIcons'

const ink='#28473E', action='#287C77', paper='#FCFAF6', line='#DBE5DB', muted='#708278'
const quickLabels=['整理这一天','分析这一周','记录一顿饭','看一下作息']
const quickPrompts=['请帮我整理这一天的生活记录。','请分析最近一周的生活记录和变化趋势。','请把这张餐照和饮食内容一起记录到饮食记录里。','请帮我查看和梳理最近的作息记录。']
const paths:Map<string,string>=new Map([
 ['plus','M12 5v14M5 12h14'],['history','M5 3h13a1 1 0 0 1 1 1v17H6a2 2 0 0 1-2-2V5a2 2 0 0 1 1-2m2 5h8m-8 4h8m-9 5h12'],
 ['settings','M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z M15 12a3 3 0 1 1-6 0 3 3 0 1 1 6 0'],
 ['update','M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4'],['photo','M3 4h18v16H3z M3 16l6-6 4 4 3-3 5 5 M16 8h.1'],
 ['send','M12 16V4m-4 4 4-4 4 4M4 14v6h16v-6'], ['list','M6 6h12M6 12h12M6 18h9']
])
@Component
export struct AssistantPage {
  @Prop @Watch('routeChanged') route:string='ai'
  onNavigate:(route:string)=>void=()=>{}
  @State messages:Data[]=[]
  @State sessions:Data[]=[]
  @State tasks:Data[]=[]
  @State settings:Data={}
  @State input:string=''
  @State photo:string=''
  @State file?:AssistantFile=undefined
  @State purgeConfirm:boolean=false
  @State liveText:string=''
  @State liveStep:string=''
  @State cancellable:boolean=false
  @State recognition:string=''
  @State corrected:string=''
  @State images:Map<string,string>=new Map()
  @State conversation:number=0
  @State search:string=''
  @State error:string=''
  @State pending:boolean=false
  @State recoveryId:string=''
  @State loading:boolean=false
  @State extra:boolean=false
  @State listening:boolean=false
  @State taskCount:number=0
  @State taskId:string=''
  @State taskName:string=''
  @State cursor:string=''
  @State older:number=0
  @State batch:boolean=false
  @State selected:number[]=[]
  @State renameId:number=0
  @State renameText:string=''
  @State focusDraft:Data={}
  @State focusMessage:number=0
  @State receipts:Map<string,string>=new Map()
  @State cloudFiles:Data[]=[]
  private op?:ChatOperation
  private voiceReady?:Promise<void>
  private serial:number=0
  private owner:number=0
  private sessionLoad:number=0
  private inputQuery:number=0
  private scroller:Scroller=new Scroller()
  private ctx():common.UIAbilityContext{return this.getUIContext().getHostContext() as common.UIAbilityContext}
  aboutToAppear(){this.owner=actorId();this.init()}
  aboutToDisappear(){this.serial++;this.sessionLoad++;this.inputQuery++;if(this.listening){this.listening=false;cancelVoice().catch(()=>{})}}
  private async init():Promise<void>{
    const ticket=++this.serial
    this.loading=true
    try{
      this.settings=await rpc<Data>('preferences','get_preferences',[this.owner]);
      if(!this.owner){const aliases:Record<string,string>={'library_enabled':'context_consent','personal_enabled':'personal_context_consent','thinking_enabled':'thinking_enabled','reasoning_effort':'reasoning_effort','web_enabled':'web_enabled','tools_enabled':'tools_enabled'};for (const key of Object.keys(aliases)) this.settings['mobile_ai_'+key]=this.settings['ai_'+aliases[key]]}
      this.sessions=await rpc<Data[]>('chat','list_conversations',[this.owner])
      if(ticket!==this.serial || this.owner!==actorId())return
      const preferred=this.owner?Number(decisionStore(this.ctx()).getSync('lastConversation',0)):0
      let session=this.sessions.find((r:Data)=>r['id']===preferred)||this.sessions[0]
      if(!session)session=await rpc<Data>('chat','create_conversation',[this.owner,'新对话'])
      if(ticket!==this.serial)return
      await this.openChat(Number(session['id']),false)
      if(this.owner&&this.route==='ai/tasks')await this.loadTasks()
    }catch(e){if(ticket===this.serial)this.error=noteError(e as Error)}
    finally{if(ticket===this.serial)this.loading=false}
  }
  private async openChat(id:number,go:boolean=true):Promise<void>{
    const ticket=++this.sessionLoad
    if(this.conversation&&this.conversation!==id){this.saveInput()}
    this.conversation=id;this.taskId='';this.taskName='';this.focusDraft={};this.photo='';this.file=undefined;this.op=undefined;this.error=''
    const store=decisionStore(this.ctx())
    if(this.owner){store.putSync('lastConversation',id);store.flush().catch(()=>{})}
    this.input=this.owner?String(store.getSync('draft_'+id,'')):''
    this.recoveryId=this.owner?String(store.getSync('pending_'+id,'')):''
    this.messages=[];this.images=new Map();this.receipts=new Map()
    await rpc<Object>('chat','ensure_greeting',[this.owner],{'conversation_id':id,'local_hour':new Date().getHours()} as Record<string, Object>)
    if(ticket!==this.sessionLoad)return
    if(this.recoveryId)await this.restorePending(false)
    await this.read(false)
    if(ticket===this.sessionLoad && go)this.onNavigate('ai')
  }
  private saveInput(){if(this.owner&&this.conversation&&this.owner===actorId()){const s=decisionStore(this.ctx());s.putSync('draft_'+this.conversation,this.input);s.flush().catch(()=>{})}}
  private async restorePending(stop:boolean):Promise<void>{
    const id=this.recoveryId, conv=this.conversation, ticket=this.sessionLoad
    if(!id || !this.owner)return
    try {
      const state=stop?stringValue(await cloud<Data>('/v1/ai/operations/'+id+'/cancel','POST',{'kind':'chat.send'} as Data),'state'):await checkAnswer(id,conv)
      if(ticket!==this.sessionLoad)return
      if(state==='running'){this.error='上一条回答正在处理，请稍后查看。';return}
      if(stop&&state==='complete')await checkAnswer(id,conv)
      this.op=undefined;this.recoveryId='';this.error=''
      const store=decisionStore(this.ctx());store.deleteSync('pending_'+conv)
      if(state==='complete'){this.input='';this.photo='';this.file=undefined;store.deleteSync('draft_'+conv)}
      await store.flush();await this.read(false)
    }catch(e){if(ticket===this.sessionLoad)this.error=noteError(e as Error)}
  }
  private savedTarget(d:Data):string{return String(decisionStore(this.ctx()).getSync('target_'+stringValue(d,'id'),''))}
  private routeChanged(){
    this.search='';this.extra=false;this.batch=false;this.selected=[];this.purgeConfirm=false;this.renameId=0;this.error=''
    if(this.route==='ai/history')this.refreshSessions()
    if(this.owner&&this.route==='ai/tasks' && this.conversation){this.focusDraft={};this.loadTasks()}
    if(this.route==='ai/files')this.listFiles()
  }
  private async refreshSessions():Promise<void>{try{this.sessions=await rpc<Data[]>('chat','list_conversations',[this.owner])}catch(e){this.error=noteError(e as Error)}}
  private async listFiles():Promise<void>{try{this.cloudFiles=await rpc<Data[]>('files','list_files',[this.owner])}catch(e){this.error=noteError(e as Error)}}
  private async taskSummary():Promise<void>{if(!this.owner)return;const id=this.conversation;try{const res=await cloud<Data>('/v1/ai/record-tasks?conversation_id='+id+'&limit=1');if(id===this.conversation)this.taskCount=Number(res['total'])}catch(_){}}
  private async read(append:boolean):Promise<void>{
    const id=this.conversation, ticket=this.sessionLoad
    const packet=await loadMessages(id,append?this.older:0)
    if(id!==this.conversation || ticket!==this.sessionLoad)return
    this.older=packet['has_more']===true?Number(packet['next_cursor']):0
    const rows=array(packet,'messages')
    this.messages=append?rows.concat(this.messages):rows
    const result=new Map<string,string>()
    const store=decisionStore(this.ctx())
    for(const row of this.messages){
      for(const d of array(row,'record_drafts')){
        const saved=store.getSync(stringValue(d,'id'),'') as string
        if(saved)result.set(stringValue(d,'id'),saved)
        if(d['supersedes_draft_id'])result.set(stringValue(d,'supersedes_draft_id'),'superseded')
        if(d['superseded'])result.set(stringValue(d,'id'),'superseded')
        this.draftPhotos(d)
      }
    }
    this.receipts=result;this.taskSummary()
    rows.filter((r:Data)=>r['role']==='user').slice(-20).forEach((r:Data)=>this.loadMessagePhoto(Number(r['id']),ticket))
    if(!append && this.messages.length>1)setTimeout(():void=>{this.scroller.scrollEdge(Edge.Bottom)},100)
  }
  private async loadMessagePhoto(id:number, ticket:number):Promise<void>{
    if(this.images.has('u'+id))return
    try{const src=await messageImage(id);if(src && ticket===this.sessionLoad){const cache=new Map(this.images);cache.set('u'+id,src);this.images=cache}}catch(_){}
  }
  private draftPhotos(d:Data){const ids=d['photo_file_ids'] as number[];if(ids)ids.forEach((id:number)=>this.ownedPhoto(id))}
  private async ownedPhoto(id:number):Promise<void>{const ticket=this.sessionLoad;if(this.images.get('f'+id))return;try{const src=asDataUri(await rpc<Data>('files','get_file',[this.owner,id]));if(ticket===this.sessionLoad){const cache=new Map(this.images);cache.set('f'+id,src);this.images=cache}}catch(_){}}
  private async purge():Promise<void>{if(this.pending||this.owner)return;this.pending=true;try{await purgeGuest();this.onNavigate('login')}catch(e){this.error=noteError(e as Error)}finally{this.pending=false}}
  private async chooseFile(fromCloud?:Data):Promise<void>{
    const ticket=this.sessionLoad
    try{
      let f:AssistantFile|undefined
      if(fromCloud){const row=await rpc<Data>('files','get_file',[this.owner,fromCloud['id']]);f=await prepareAssistantFile(this.ctx(),stringValue(row,'original_name'),stringValue(row,'content'),Number(fromCloud['id']))}
      else f=await chooseAssistantFile(this.ctx())
      if(!f||ticket!==this.sessionLoad)return
      this.file=f;this.photo=f.mime.startsWith('image/')?'data:'+f.mime+';base64,'+f.content:'';this.onNavigate('ai')
    }catch(e){this.error=noteError(e as Error)}
  }
  private async pick():Promise<void>{if(this.pending)return;try{const chosen=await pickPhotos(1);if(chosen.length){this.file=undefined;this.photo=chosen[0]}}catch(e){this.error=noteError(e as Error)}}
  private async stopAnswer():Promise<void>{
    const op=this.op;if(!op||!this.cancellable)return
    this.cancellable=false
    try{if(this.owner)await cloud<Data>('/v1/ai/operations/'+op.id+'/cancel','POST',{'kind':'chat.send'} as Data)
      else await guestAction<Data>('ai.operations.cancel',{'operation_id':op.id,'kind':'chat.send'} as Data)
      this.liveStep='已请求停止回答。'
    }catch(e){this.error=noteError(e as Error);this.cancellable=true}
  }
  private async send(recognize:boolean=false):Promise<void>{
    if(this.pending||!this.conversation||!this.input.trim()&&!this.photo&&!this.file)return
    if(this.recoveryId){await this.restorePending(false);return}
    const content=recognize?(this.file&&!this.photo?'请阅读附件中实际可提取的文字，概括主要内容。若为报告，请列出实际项目、结果、单位与参考范围；不可读取的内容明确说明，不编造、不诊断。':'请识别这张图片实际可见的内容。若为餐饮，请描述食物和可判断的份量；若为体检报告，请列出实际文字、项目、结果、单位与参考范围。看不清或不能可靠确定的内容明确写未确认，不诊断、不编造数值。'):this.input.trim()||(this.file&&!this.photo?'请阅读这个文件并概括主要内容。':'请帮我看看这张照片。')
    if(!this.owner && /记录|保存|记一下/.test(content))this.getUIContext().getPromptAction().showToast({message:'可在体验对话中咨询，保存正式生活记录需要登录。'})
    if(!this.op ||this.op.input!==content||this.op.image!==this.photo||this.op.task!==this.taskId||this.op.file!==this.file)this.op=new ChatOperation(content,this.photo,this.taskId,this.conversation,this.file)
    this.pending=true;this.error='';this.extra=false;this.liveText='';this.liveStep='正在处理…';this.cancellable=false
    try{
      const ticket=this.sessionLoad,attempt=this.op,store=decisionStore(this.ctx())
      if(this.owner){store.putSync('pending_'+attempt.conversation,attempt.id);await store.flush()}
      let preview='',lastPaint=0
      try{await sendChat(attempt,this.settings,this.owner?store.getSync('recent',[]) as string[]:[],(event:Data):void=>{
        if(ticket!==this.sessionLoad||this.owner!==actorId())return
        this.cancellable=true
        if(typeof event['delta']==='string'){preview+=String(event['delta']);if(Date.now()-lastPaint>60||preview.length<5){this.liveText=preview;lastPaint=Date.now();this.scroller.scrollEdge(Edge.Bottom)}}
        else if(event['done']===true||event['type']==='result'){this.liveText=preview;this.cancellable=false}
        else if(event['type']==='thinking')this.liveStep='正在思考…'
        else if(event['type']==='tool_start')this.liveStep='✧  '+stringValue(event,'label','正在使用工具')
        else if(event['type']==='record_drafts')this.liveStep='✧  记录助手 · 整理生活记录'
      })}
      catch(e){if(this.owner){if(attempt.attempted && ticket===this.sessionLoad)this.recoveryId=attempt.id;else if(!attempt.attempted){store.deleteSync('pending_'+attempt.conversation);await store.flush()}}throw e as Error}
      if(this.owner){store.deleteSync('pending_'+attempt.conversation);await store.flush()}
      if(ticket!==this.sessionLoad)return
      this.op=undefined
      if(!recognize){this.input='';this.photo='';this.file=undefined;this.taskId='';this.taskName='';this.focusDraft={};this.saveInput()}
      await this.read(false)
      if(recognize&&ticket===this.sessionLoad){
        const mid=Number(child(attempt.exchange||{},'assistant_message')['id'])
        const result=mid?this.messages.find((m:Data)=>Number(m['id'])===mid):this.messages.slice().reverse().find((m:Data)=>m['role']==='assistant')
        if(!result||!stringValue(result,'content'))throw new Error('识别结果待核对，请查看对话。')
        this.recognition=stringValue(result,'content');this.corrected=this.recognition;this.onNavigate('ai/photo/result')
      }
    }catch(e){this.error=noteError(e as Error)}
    finally{this.pending=false;this.liveText='';this.liveStep='';this.cancellable=false}
  }
  private async act(draft:Data,mid:number,save:boolean):Promise<void>{
    if(this.pending)return
    this.pending=true;this.error=''
    try{await decideRecord(mid,stringValue(draft,'id'),save,this.ctx());const copy=new Map(this.receipts);copy.set(stringValue(draft,'id'),save?'saved':'cancelled');this.receipts=copy;this.taskSummary()
      if(save)syncDeviceReminders(this.ctx(),false).catch(()=>{})
    }
    catch(e){this.error=noteError(e as Error)}
    finally{this.pending=false}
  }
  private async proposal(p:Data,save:boolean):Promise<void>{
    if(this.pending)return
    this.pending=true;this.error=''
    try{const id=stringValue(p,'id'),store=decisionStore(this.ctx()),key='proposal_'+id+save;let opid=store.getSync(key,'') as string;if(!opid){opid=uuid();store.putSync(key,opid);await store.flush()}
      await cloud<Object>('/v1/ai/tools/'+(save?'confirm':'cancel'),'POST',{'proposal_token':p['token'],'request_id':opid} as Record<string, Object>);await this.read(false)}
    catch(e){this.error=noteError(e as Error)}finally{this.pending=false}
  }
  private async loadTasks(more:boolean=false):Promise<void>{
    const ticket=++this.inputQuery, id=this.conversation
    try{const res=await cloud<Data>('/v1/ai/record-tasks?conversation_id='+id+'&limit=20&query='+encodeURIComponent(this.search)+(more&&this.cursor?'&cursor='+encodeURIComponent(this.cursor):''));if(ticket!==this.inputQuery||id!==this.conversation)return
      this.tasks=more?this.tasks.concat(array(res,'tasks')):array(res,'tasks');this.cursor=stringValue(res,'next_cursor')}
    catch(e){if(ticket===this.inputQuery)this.error=noteError(e as Error)}
  }
  private async focusTask(task:Data):Promise<void>{
    try{const res=await cloud<Data>('/v1/ai/record-tasks/'+task['id']+'/focus','POST',{'conversation_id':this.conversation} as Record<string, Object>),d=child(res,'draft'),t=child(res,'task');if(t['id']!==task['id']||t['draft_id']!==d['id'])throw new Error('草稿已变更。')
      this.taskId=String(t['id']);this.taskName=stringValue(t,'title');this.focusDraft=d;this.focusMessage=Number(t['assistant_message_id']);this.draftPhotos(d)}
    catch(e){this.error=noteError(e as Error)}
  }
  private async newSession():Promise<void>{try{const c=await rpc<Data>('chat','create_conversation',[this.owner,'新对话']);await this.openChat(Number(c['id']))}catch(e){this.error=noteError(e as Error)}}
  private chooseSession(id:number):void{this.selected=this.selected.includes(id)?this.selected.filter((v:number)=>v!==id):this.selected.concat([id])}
  private async deleteSelected():Promise<void>{
    const ids=this.selected.slice()
    if(!ids.length||this.pending)return;this.pending=true
    try{await rpc<number>('chat','delete_conversations',[this.owner,ids]);await this.refreshSessions();this.batch=false;this.selected=[]
      if(ids.includes(this.conversation)){if(this.sessions.length)await this.openChat(Number(this.sessions[0]['id']),false);else await this.newSession()}
    }catch(e){this.error=noteError(e as Error)}finally{this.pending=false}
  }
  private async editSession(id:number,remove:boolean):Promise<void>{
    try{if(remove)await rpc<number>('chat','delete_conversations',[this.owner,[id]])
      else {if(!this.renameText.trim())return;await rpc<Data>('chat','rename_conversation',[this.owner,id,this.renameText.trim()])}
      this.renameId=0;await this.refreshSessions()
      if(remove&&this.conversation===id){if(this.sessions.length)await this.openChat(Number(this.sessions[0]['id']),false);else await this.newSession()}}
    catch(e){this.error=noteError(e as Error)}
  }
  private async startMic():Promise<void>{if(this.pending||this.listening)return;this.listening=true;this.error='';this.voiceReady=startVoice(this.ctx());try{await this.voiceReady}catch(e){this.error=noteError(e as Error);this.listening=false}}
  private async stopMic():Promise<void>{if(!this.listening)return;this.listening=false;try{await this.voiceReady;const words=await finishVoice();if(words){this.input=(this.input+' '+words).trim();this.saveInput()}}catch(e){this.error=noteError(e as Error)}}
  private async quick(i:number):Promise<void>{if(i===2){try{const r=await capturePhoto(this.ctx());if(!r.length)return;this.file=undefined;this.photo=r[0]}catch(e){this.error=noteError(e as Error);return}}this.input=quickPrompts[i];await this.send()}
  private setOpt(key:string,v:Object):void{const d=mergeData(this.settings);d[key]=v;this.settings=d}
  private async saveSettings():Promise<void>{try{const patch:Data={};['ai_complexity','ai_tone','mobile_ai_library_enabled','mobile_ai_personal_enabled','mobile_ai_thinking_enabled','mobile_ai_reasoning_effort','mobile_ai_web_enabled','mobile_ai_tools_enabled'].forEach((key:string)=>{if(this.settings[key]!==undefined)patch[key]=this.settings[key]});if(!this.owner){const guestPatch:Data={};const fields:Record<string,string>={'ai_complexity':'ai_complexity','ai_tone':'ai_tone','mobile_ai_library_enabled':'ai_context_consent','mobile_ai_personal_enabled':'ai_personal_context_consent','mobile_ai_thinking_enabled':'ai_thinking_enabled','mobile_ai_reasoning_effort':'ai_reasoning_effort','mobile_ai_web_enabled':'ai_web_enabled','mobile_ai_tools_enabled':'ai_tools_enabled'};for(const k of Object.keys(patch))guestPatch[fields[k]]=patch[k];await rpc<Data>('preferences','update_preferences',[this.owner,guestPatch]);this.settings=mergeData(this.settings,guestPatch)}else await rpc<Data>('preferences','update_preferences',[this.owner,patch]);this.onNavigate('ai')}catch(e){this.error=noteError(e as Error)}}
  @Builder Icon(name:string,color:string,size:number=22){
    Shape(){Path({commands:paths.get(name)||RECORD_ICONS.get(name)||''}).fill(Color.Transparent).stroke(color).strokeWidth('1.8px').strokeLineCap(LineCapStyle.Round).strokeLineJoin(LineJoinStyle.Round)}.width(size).height(size).viewPort({x:0,y:0,width:'24px',height:'24px'}).accessibilityLevel('no')
  }
  @Builder IconButton(name:string,label:string,click:()=>void,solid:boolean=false){
    Row(){this.Icon(name,solid?'#FFFFFF':ink)}.width(44).height(44).justifyContent(FlexAlign.Center).borderRadius(solid?14:22).backgroundColor(solid?action:Color.Transparent).id('ai-icon-'+name).accessibilityText(label).onClick(click)
  }
  @Builder Btn(label:string,click:()=>void,primary:boolean=false,identifier:string=""){
    Button(label,{type:ButtonType.Normal}).id(identifier||("ai-action-"+label)).fontSize(16).fontColor(primary?'#FFFFFF':ink).backgroundColor(primary?action:'#FFFDF9').border({width:1,color:line}).borderRadius(13).constraintSize({minHeight:42}).onClick(click)
  }
  private title():string{return this.route==='ai/update'?'应用更新':this.route==='ai/history'?'对话记录':this.route==='ai/settings'?'智能设置':this.route==='ai/tasks'?'待完成记录':this.route==='ai/files'?'选择我的文件':this.route==='ai/cloud'?'智慧云功能':this.route==='ai/voice'?'语音输入':this.route.startsWith('ai/photo')?'照片识别':'智能助手'}
  @Builder Header(){
    Row({space:4}){
      if(this.route==='ai'){Image($r('app.media.records_logo')).width(40).height(40).objectFit(ImageFit.Contain)}
      else{this.IconButton('back','返回',()=>this.onNavigate('ai'))}
      Text(this.title()).fontSize(19).fontWeight(FontWeight.Bold).fontColor(ink).layoutWeight(1)
      if(this.route==='ai'){
        this.IconButton('update','检查应用更新',()=>this.onNavigate('ai/update'))
        this.IconButton('history','对话记录',()=>this.onNavigate('ai/history'))
        this.IconButton('settings','智能设置',()=>this.onNavigate('ai/settings'))
      }
    }.width('100%').height(68).padding({left:16,right:14}).backgroundColor(paper)
  }
  @Builder Draft(d:Data,mid:number){
    Column({space:8}){
      Row(){Text((categoryNames.get(stringValue(d,'category'))||'生活')+'记录').fontSize(21).fontWeight(FontWeight.Bold).fontColor(ink).layoutWeight(1)
        Text(this.receipts.get(stringValue(d,'id'))==='saved'?'已记录':this.receipts.get(stringValue(d,'id'))==='cancelled'?'已取消':this.receipts.get(stringValue(d,'id'))==='superseded'?'已有新版':array(d,'missing_required').length?'待补充':d['blocked']?'未保存':'待确认').fontSize(14).fontColor(action)}
      ForEach(draftFields(d),(f:FieldRow)=>{Row(){Text(f.label).fontColor(muted).fontSize(15).width('31%');Column(){if(f.before){Text('原记录　'+f.before).fontSize(13).fontColor(muted)}Text((f.before?'确认后　':'')+f.value).fontSize(16).fontColor(ink)}.alignItems(HorizontalAlign.Start).layoutWeight(1)}.width('100%').padding({top:4,bottom:4});Divider().color(line)},(f:FieldRow)=>f.label+f.value+f.before)
      if((d['photo_file_ids'] as number[])?.length){
        Text(this.receipts.get(stringValue(d,'id'))==='saved'?'已随记录保存的照片':'将随记录保存的照片').fontSize(14).fontColor(muted)
        ForEach(d['photo_file_ids'] as number[],(id:number)=>{if(this.images.get('f'+id)){Image(this.images.get('f'+id)).id('ai-draft-photo-'+mid+'-'+id).accessibilityText('草稿所附原图').width('100%').height(166).objectFit(ImageFit.Contain).borderRadius(12)}else{this.Btn('读取照片',()=>this.ownedPhoto(id))}},(id:number)=>String(id)+!!this.images.get('f'+id))
      }
      if(array(d,'missing_required').length){Text('还缺'+array(d,'missing_required').map((r:Data)=>stringValue(r,'label')).join('、')+'，请在对话里告诉我。').fontSize(15).fontColor(ink)}
      else if(array(d,'missing_optional').length&&!this.receipts.has(stringValue(d,'id'))){Text('也可以补充'+array(d,'missing_optional').map((r:Data)=>stringValue(r,'label')).join('、')+'。').fontSize(14).fontColor(muted)}
      if(this.receipts.get(stringValue(d,'id'))==='saved'&&this.savedTarget(d)){this.Btn('查看已保存记录',()=>this.onNavigate('detail/'+this.savedTarget(d)),false,'ai-open-record-'+mid)}
      if(!this.receipts.has(stringValue(d,'id'))&&!d['blocked']){
        Row({space:8}){if(!array(d,'missing_required').length){this.Btn(d['operation']==='supplement'?'确认补充':d['operation']==='update'?'确认修改':'确认记录',()=>this.act(d,mid,true),true,'ai-confirm-'+mid)}this.Btn('取消',()=>this.act(d,mid,false))}.enabled(!this.pending)
      }
    }.alignItems(HorizontalAlign.Start).width('100%').padding(14).backgroundColor('#FFFDF9').border({width:1,color:line}).borderRadius(18)
  }
  @Builder Quick(i:number){Row({space:5}){this.Icon(['water','activity','diet','environment'][i],ink,19);Text(quickLabels[i]).fontSize(16).fontColor(ink)}.layoutWeight(1).height(44).justifyContent(FlexAlign.Center).borderRadius(12).backgroundColor('#FFFDF9').border({width:1,color:line}).shadow({radius:1,offsetY:2,color:'#CFE0D6'}).onClick(()=>this.quick(i))}
  @Builder Message(row:Data){
    Column({space:6}){
      if(row['role']==='assistant'){Image($r('app.media.assistant_avatar')).width(38).height(38).objectFit(ImageFit.Cover).borderRadius(19)}
      if(array(row,'record_drafts').length){Text('✧  记录助手 · 整理生活记录').fontSize(13).fontColor('#79847E')}
      Column({space:8}){
        if(row['role']==='assistant'&&stringValue(row,'content')){AssistantAnswer({source:stringValue(row,'content')})}
        else{Text(stringValue(row,'content')||(row['status']==='pending'?'正在处理…':stringValue(row,'error_message'))).fontSize(21).lineHeight(30).fontColor(ink).copyOption(CopyOptions.LocalDevice)}
        if(this.images.has('u'+row['id'])){Image(this.images.get('u'+row['id'])).height(160).width('100%').objectFit(ImageFit.Contain)}
        if(row['role']==='assistant'&&row['provider']==='fawnsee-welcome'){Text('温馨提示：以上仅供参考，不替代医生诊断。').fontSize(13).fontColor(muted)}
      }.alignItems(HorizontalAlign.Start).width(row['role']==='assistant'?'100%':'88%').padding(13).backgroundColor(row['role']==='assistant'?'#FAFBFC':'#DCEEE7').borderRadius(19).border({width:1,color:'#FFFFFF'})
      ForEach(array(row,'record_drafts'),(d:Data)=>{this.Draft(d,Number(row['id']))},(d:Data)=>stringValue(d,'id'))
      ForEach(array(row,'proposals'),(p:Data)=>{Column({space:6}){Text(stringValue(p,'label',stringValue(p,'title','待确认操作'))).fontSize(17);Text(stringValue(p,'summary')).fontSize(15);if(p['status']==='pending'){Row({space:8}){this.Btn('确认',()=>this.proposal(p,true),true);this.Btn('取消',()=>this.proposal(p,false))}}else{Text(p['status']==='saved'?'已保存':'已取消')}}.padding(12).backgroundColor('#FFFDF9').borderRadius(12)},(p:Data)=>stringValue(p,'id')+p['status'])
      if(row['role']==='assistant'&&row['provider']!=='fawnsee-welcome'){Row({space:8}){this.Btn('查看生活记录',()=>this.onNavigate('records'));this.Btn('查看趋势',()=>this.onNavigate('trends'));this.Btn('复制',()=>{pasteboard.getSystemPasteboard().setData(pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN,stringValue(row,'content'))).catch(()=>{})})}}
    }.width('100%').alignItems(row['role']==='assistant'?HorizontalAlign.Start:HorizontalAlign.End)
  }
  @Builder Chat(){
    Stack({alignContent:Alignment.Top}){
      Image($r('app.media.records_petal')).width('100%').height('100%').objectFit(ImageFit.Cover).opacity(.16)
      Column(){
        Scroll(this.scroller){Column({space:9}){
          if(this.older){this.Btn('加载更早对话',()=>this.read(true).catch((e:Error)=>{this.error=noteError(e)}))}
          ForEach(this.messages,(row:Data)=>{this.Message(row)},(row:Data)=>String(row['id'])+row['status']+JSON.stringify(row['record_drafts'])+JSON.stringify(row['proposals']))
          if(this.messages.length<=1){Row({space:6}){this.Quick(0);this.Quick(1)};Row({space:6}){this.Quick(2);this.Quick(3)}}
          if(this.pending){Text(this.liveStep||'正在处理…').fontSize(14).fontColor(muted);if(this.liveText){Column(){AssistantAnswer({source:this.liveText,live:true})}.id('ai-streamed-answer').width('100%').padding(13).backgroundColor('#FAFBFC').borderRadius(19)}if(this.cancellable){this.Btn('停止回答',()=>this.stopAnswer())}}
        }.width('100%').padding(12).alignItems(HorizontalAlign.Start)}.layoutWeight(1).width('100%').scrollBar(BarState.Off)
        if(this.file&&!this.photo){Row({space:8}){Text(this.file.name).fontSize(15).fontColor(ink);this.Btn('移除文件',()=>{this.file=undefined})}}
        if(this.photo){Row({space:8}){Image(this.photo).width(54).height(54).objectFit(ImageFit.Cover).borderRadius(8);this.Btn('移除照片',()=>{this.photo='';this.file=undefined})}}
        if(this.recoveryId){Row({space:8}){this.Btn('查看回答进度',()=>this.restorePending(false));this.Btn('停止等待',()=>this.restorePending(true))}}
        if(this.owner&&(this.taskCount||this.taskId)){Text('☷  待完成记录 '+this.taskCount+(this.taskId?'　正在补 '+this.taskName:'')).fontSize(14).fontColor(muted).onClick(()=>this.onNavigate('ai/tasks'))}
        Row({space:8}){this.Icon('mic','#FFFFFF');Text(this.listening?'松开结束':'按住说话').fontColor('#FFFFFF').fontSize(18).fontWeight(FontWeight.Bold)}.width(176).height(52).justifyContent(FlexAlign.Center).borderRadius(26).backgroundColor('#926CB0').shadow({radius:1,offsetY:4,color:'#68438A'}).margin({top:8,bottom:15}).onTouch((e:TouchEvent)=>{if(e.type===TouchType.Down)this.startMic();if(e.type===TouchType.Up)this.stopMic();if(e.type===TouchType.Cancel){this.listening=false;cancelVoice().catch(()=>{})}})
        if(this.extra){Column({space:5}){Row({space:8}){this.Btn('文件',()=>this.onNavigate('ai/files'));this.Btn('智慧云功能',()=>this.onNavigate('ai/cloud'));this.Btn('照片识别',()=>this.onNavigate('ai/photo'))}
          Row({space:8}){this.Btn('拍照',async()=>{try{const r=await capturePhoto(this.ctx());if(r.length){this.file=undefined;this.photo=r[0]}this.extra=false}catch(e){this.error=noteError(e as Error)}});if(this.owner){this.Btn('待完成记录',()=>this.onNavigate('ai/tasks'))}}}}
        Row({space:6}){this.IconButton('plus','更多功能',()=>{this.extra=!this.extra},true);this.IconButton('photo','上传照片',()=>this.pick(),true)
          TextInput({text:this.input}).id('ai-input').accessibilityText('输入消息').onChange((v:string)=>{this.input=v;this.saveInput()}).onSubmit(()=>this.send()).maxLength(4000).fontSize(20).backgroundColor(Color.Transparent).padding(2).layoutWeight(1).enabled(!this.pending)
          this.IconButton('send','发送消息',()=>this.send(),true)
        }.width('91%').height(58).padding({left:7,right:7}).borderRadius(21).backgroundColor('#FFFDF9').border({color:line,width:1}).margin({bottom:8})
      }.height('100%')
    }.width('100%').layoutWeight(1)
  }
  @Builder Setting(key:string,title:string,labels:string[],values:string[]){
    Column({space:8}){Text(title).fontSize(19).fontWeight(FontWeight.Bold).fontColor(ink);Row({space:5}){ForEach(labels,(name:string,i:number)=>{this.Btn(name,()=>{const v=values[i];this.setOpt(key,v==='true'?true:v==='false'?false:v)},String(this.settings[key]??(values.includes('true')?'true':values[0]))===values[i])})}}.width('100%').padding(14).alignItems(HorizontalAlign.Start).backgroundColor('#FFFDF9').borderRadius(14)
  }
  @Builder Secondary(){
    Scroll(){Column({space:12}){
      if(this.route==='ai/history'){
        TextInput({text:this.search,placeholder:'搜索对话记录'}).onChange((v:string)=>this.search=v);Row({space:6}){this.Btn('新建对话',()=>this.newSession(),true);this.Btn(this.batch?'退出选择':'批量管理',()=>{this.batch=!this.batch;this.selected=[]});if(this.batch&&this.selected.length){this.Btn('删除 '+this.selected.length+' 段',()=>this.getUIContext().showAlertDialog({title:'删除所选对话？',message:'所选对话删除后无法恢复。',primaryButton:{value:'保留',action:()=>{}},secondaryButton:{value:'删除',action:()=>this.deleteSelected()}}))}}
        ForEach(this.sessions.filter((r:Data)=>stringValue(r,'title').includes(this.search)),(r:Data)=>{Column({space:7}){Text(stringValue(r,'title')).fontSize(20).fontColor(ink).onClick(()=>this.openChat(Number(r['id'])).catch((e:Error)=>{this.error=noteError(e)}))
          if(this.batch){this.Btn(this.selected.includes(Number(r['id']))?'已选中':'选择对话',()=>this.chooseSession(Number(r['id'])),this.selected.includes(Number(r['id'])))}
          else if(this.renameId===Number(r['id'])){TextInput({text:this.renameText}).onChange((v:string)=>this.renameText=v);this.Btn('保存名称',()=>this.editSession(Number(r['id']),false))}
          else{Row({space:7}){this.Btn('打开',()=>this.openChat(Number(r['id'])).catch((e:Error)=>{this.error=noteError(e)}),false,'ai-open-chat-'+r['id']);this.Btn('重命名',()=>{this.renameId=Number(r['id']);this.renameText=String(r['title'])});this.Btn('删除',()=>this.getUIContext().showAlertDialog({title:'删除这段对话？',message:'删除后将无法恢复。',primaryButton:{value:'取消',action:()=>{}},secondaryButton:{value:'删除',action:()=>this.editSession(Number(r['id']),true)}}))}}
        }.width('100%').alignItems(HorizontalAlign.Start).padding(12).backgroundColor('#FFFDF9').borderRadius(14)},(r:Data)=>String(r['id'])+r['title'])
      }else if(this.route==='ai/settings'){
        this.Setting('ai_complexity','模型生成复杂度',['简洁','标准','详细'],['simple','standard','detailed'])
        this.Setting('ai_tone','对话语气',['温和陪伴','简洁直接','清楚正式'],['warm','concise','formal'])
        this.Setting('mobile_ai_thinking_enabled','深度思考',['开启','关闭'],['true','false'])
        this.Setting('mobile_ai_reasoning_effort','思考强度选择',['低','高','最高'],['low','high','max'])
        this.Setting('mobile_ai_tools_enabled','受控工具使用',['开启','关闭'],['true','false'])
        this.Setting('mobile_ai_library_enabled','使用资料卡',['开启','关闭'],['true','false'])
        this.Setting('mobile_ai_personal_enabled','使用我的生活资料',['开启','关闭'],['true','false'])
        this.Btn('保存设置',()=>this.saveSettings(),true)
      }else if(this.route==='ai/tasks'){
        if(this.focusDraft['id']){this.Draft(this.focusDraft,this.focusMessage);this.Btn('返回对话',()=>this.onNavigate('ai'),true)}
        else{TextInput({text:this.search,placeholder:'搜索日期、时间或内容'}).onChange((v:string)=>{this.search=v;this.loadTasks()})
          ForEach(this.tasks,(t:Data)=>{Column({space:5}){Text(stringValue(t,'title',(categoryNames.get(stringValue(t,'category'))||'生活')+'记录')).fontSize(18).fontWeight(FontWeight.Bold);Text([stringValue(t,'record_date'),stringValue(t,'record_time'),stringValue(t,'preview')].filter((s:string)=>!!s).join(' · ')).fontSize(15);Text('来源消息 #'+t['assistant_message_id']+' · '+String(t['id']).slice(-8)).fontSize(12).fontColor(muted)}.width('100%').alignItems(HorizontalAlign.Start).padding(12).backgroundColor('#FFFDF9').borderRadius(12).onClick(()=>this.focusTask(t))},(t:Data)=>stringValue(t,'id'))
          if(this.cursor){this.Btn('加载更多',()=>this.loadTasks(true))}if(!this.tasks.length){Text('没有找到符合的记录。').fontColor(muted)}}
      }else if(this.route==='ai/files'){
        ForEach(this.cloudFiles,(r:Data)=>{this.Btn(stringValue(r,'original_name')+'　'+Math.max(1,Math.ceil(Number(r['size_bytes'])/1024))+' KB',()=>this.chooseFile(r))},(r:Data)=>String(r['id']))
        this.Btn('从手机上传文件',()=>this.chooseFile(),true)
        if(!this.cloudFiles.length){Text('暂时没有云端文件').fontColor(muted)}
      }else if(this.route==='ai/cloud'){Text('百度健康').fontSize(19).fontColor(ink);Text('敬请期待').fontSize(15).fontColor(muted)
      }else if(this.route==='ai/voice'){this.Btn(this.listening?'结束录音':'开始录音',()=>{if(this.listening)this.stopMic();else this.startMic()},true);TextArea({text:this.input,placeholder:'转写文字（可修改）'}).onChange((v:string)=>this.input=v);this.Btn('使用这段文字',()=>this.onNavigate('ai'))}
      else if(this.route==='ai/photo/result'){Text('智能识别 · 待核对').fontSize(17).fontColor(action);if(this.photo){Image(this.photo).width('100%').height(180).objectFit(ImageFit.Contain)}TextArea({text:this.corrected}).height(200).fontSize(17).onChange((v:string)=>this.corrected=v);this.Btn('核对并返回对话',()=>{if(this.corrected!==this.recognition)this.input='我核对照片后的内容：'+this.corrected;this.photo='';this.file=undefined;this.saveInput();this.onNavigate('ai')},true)}
      else{if(this.photo){Image(this.photo).width('100%').height(220).objectFit(ImageFit.Contain)}this.Btn('拍照上传',async()=>{try{const r=await capturePhoto(this.ctx());if(r.length){this.file=undefined;this.photo=r[0]}}catch(e){this.error=noteError(e as Error)}});this.Btn('从相册选择',()=>this.pick());if(this.file&&!this.photo){Text(this.file.name).fontColor(ink)}if(this.file||this.photo){this.Btn('开始识别',()=>this.send(true),true)}this.Btn('用于当前对话',()=>this.onNavigate('ai'))}
      if(!this.owner&&(this.route==='ai/history'||this.route==='ai/settings')){
        if(this.purgeConfirm){Text('清除后，本次体验的对话和附件将被删除。确定要继续吗？').fontSize(16).fontColor(ink);Row({space:8}){this.Btn('保留数据',()=>{this.purgeConfirm=false});this.Btn('确认清除',()=>this.purge(),true,'ai-guest-purge-confirm')}}
        else{this.Btn('清除体验数据',()=>{this.purgeConfirm=true},false,'ai-guest-purge')}
      }
    }.width('100%').alignItems(HorizontalAlign.Start).padding(16)}.width('100%').layoutWeight(1)
  }
  build(){Column(){this.Header();if(this.route==='ai/update'){AppUpdatePanel()}else{if(this.error){Row(){Text(this.error).fontSize(14).fontColor('#8A483F').layoutWeight(1);Text('重试').fontColor(action).onClick(()=>{if(this.op)this.send();else this.init()})}.width('100%').padding(8).backgroundColor('#FFF1EA')}if(this.loading){LoadingProgress().width('100%').height(3)}if(this.route==='ai'){this.Chat()}else{this.Secondary()}}}.width('100%').height('100%').backgroundColor(paper)}
}
