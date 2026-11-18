import { pasteboard } from '@kit.BasicServicesKit'
import { common } from '@kit.AbilityKit'
import { shareText } from '../services/TextShare'
import { safeTarget } from './AssistantAnswerFormat'
import { actorId, cloud, CloudError, rpc } from '../services/Cloud'
import { Data, mergeData, array, asDataUri, child, noteError, stringValue, uuid } from './AssistantService'

const ink='#26483C',teal='#317C73',muted='#758479',line='#D9E5DA'
const nutrients=['energy_kcal','protein_g','fat_g','carbohydrate_g','available_carbohydrate_g','fiber_g','sodium_mg','gi']
const nutritionNames=['热量（千卡）','蛋白质（克）','脂肪（克）','碳水化合物（克）','可利用碳水（克）','膳食纤维（克）','钠（毫克）','血糖生成指数']
const questions=['蔬菜和水果','日常饮水','用餐规律']
const options:string[][]=[['多数餐次有蔬菜或水果','只有部分餐次有','很少有','暂不确定'],['按自己的日常节奏饮水','忙起来容易忘记','主要喝含糖饮料','暂不确定'],['大多数时候规律','偶尔错过一餐','经常时间不固定','暂不确定']]
function blockText(b:Data):string{
  if(b['text'])return String(b['text'])
  if(b['tag']==='br')return '\n'
  if(b['blocks'])return array(b,'blocks').map((r:Data)=>blockText(r)).join('\n')
  if(b['items'])return (b['items'] as Data[][]).map((items:Data[])=>'• '+items.map((r:Data)=>blockText(r)).join('')).join('\n')
  if(b['rows'])return array(b,'rows').map((r:Data)=>array(r,'cells').map((c:Data)=>blockText(c)).join('　')).join('\n')
  return array(b,'children').map((c:Data)=>blockText(c)).join('')
}
@Component
export struct CommunityKnowledge{
  @Prop @Watch('routeChanged') route:string='community/knowledge'
  onNavigate:(path:string)=>void=()=>{}
  @State theme:string='全部'
  @State q:string=''
  @State entries:Data[]=[]
  @State article:Data={}
  @State social:Data={}
  @State comments:Data[]=[]
  @State replies:Data[]=[]
  @State rootNext:number=-1
  @State replyNext:number=-1
  @State thread:string=''
  @State replying:boolean=false
  @State imageCache:Map<string,string>=new Map()
  @State total:number=0
  @State busy:boolean=false
  @State error:string=''
  @State bodyError:string=''
  @State socialError:string=''
  @State commentsError:string=''
  @State socialReady:boolean=false
  @State form:Data={}
  @State review:Data={}
  @State comment:string=''
  @State favorites:boolean=false
  @State agreed:boolean=false
  private op:string=''
  private lastSocialKey:string=''
  private timer:number=0
  private ticket:number=0
  private sharing:boolean=false
  aboutToAppear(){this.load()}
  private routeChanged():void{this.article={};this.social={};this.comments=[];this.replies=[];this.thread='';this.replying=false;this.rootNext=-1;this.replyNext=-1;this.comment='';this.load(false)}
  aboutToDisappear(){this.ticket++;clearTimeout(this.timer)}
  private field(k:string,v:Object):void{this.op='';const value=mergeData(this.form);value[k]=v;this.form=value}
  private contentId():string{return decodeURIComponent(this.route.split('/')[this.food()?3:2]||'')}
  private food():boolean{return this.route.startsWith('community/knowledge/food/')}
  private catalogue():boolean{return this.route==='community/knowledge'}
  private education():boolean{return this.route.startsWith('community/knowledge/nutrition')}
  private async expansion(method:string,payload:Data,op?:string):Promise<Data>{return rpc<Data>('expansion',method,[actorId(),payload],undefined,op)}
  private async load(more:boolean=false):Promise<void>{
    const t=++this.ticket;this.error=''
    if(this.route.endsWith('/new')){this.form={};this.op='';return}
    if(this.education())return
    this.busy=true
    try{
      if(this.catalogue()){
        const offset=more?this.entries.length:0
        let packet:Data={}
        if(this.theme==='食物资料'){packet=await this.expansion('list_foods',{'query':this.q,'favorites_only':this.favorites,'limit':30,'offset':offset} as Record<string, Object>)}
        else if(this.theme==='已收藏'){packet=await cloud<Data>('/v1/member/reading/bookmarks?limit=30&offset='+offset)}
        else{packet=await cloud<Data>('/v1/member/reading/catalog?scope=all&query='+encodeURIComponent(this.q)+'&limit=30&offset='+offset)}
        if(t!==this.ticket)return;const rows=array(packet,'items');this.entries=more?this.entries.concat(rows):rows;this.total=Number(packet['total']??0)
        rows.slice(0,8).forEach((a:Data)=>this.asset(stringValue(a,'cover_asset')))
      }else if(this.food()){const food=await this.expansion('get_food',{'food_id':this.contentId()} as Record<string, Object>);if(t===this.ticket)this.article=food}
      else{
        this.article={};this.social={};this.socialReady=false;this.comments=[];this.replies=[];this.rootNext=-1;this.replyNext=-1
        this.bodyError='';this.socialError='';this.commentsError=''
        await Promise.all([this.loadArticlePart(false,t),this.loadArticlePart(true,t),this.readComments('')])
        if(t===this.ticket&&this.thread)await this.readComments(this.thread)
      }
    }catch(e){if(t===this.ticket)this.error=noteError(e as Error)}finally{if(t===this.ticket)this.busy=false}
  }
  private denyArticleAccess(err:Error):void{
    this.ticket++;this.article={};this.social={};this.socialReady=false;this.comments=[];this.replies=[]
    this.rootNext=-1;this.replyNext=-1;this.thread='';this.replying=false;this.comment='';this.imageCache=new Map()
    this.bodyError='';this.socialError='';this.commentsError='';this.busy=false;this.error=noteError(err)
  }
  private async loadArticlePart(social:boolean,ticket:number):Promise<void>{
    const owner=actorId(), id=encodeURIComponent(this.contentId())
    if(social)this.socialError=''
    else this.bodyError=''
    try{
      const data=await cloud<Data>('/v1/member/reading/'+(social?'social/':'articles/')+id)
      if(ticket!==this.ticket)return
      if(owner!==actorId())throw new CloudError(401,'登录状态已改变。')
      if(social){this.social=data;this.socialReady=true}
      else{
        this.article=data
        this.asset(stringValue(data,'cover_asset'))
        array(data,'blocks').filter((b:Data)=>b['asset_id']!==undefined).slice(0,24).forEach((b:Data)=>this.asset(stringValue(b,'asset_id')))
      }
    }catch(e){
      if(ticket!==this.ticket)return
      const err=e as Error
      if(owner!==actorId()||err instanceof CloudError&&[401,403].includes(err.status)){this.denyArticleAccess(err);return}
      if(social){this.social={};this.socialReady=false;this.socialError=noteError(err)}
      else{this.article={};this.bodyError=noteError(err)}
    }
  }
  private async asset(id:string):Promise<void>{if(!id||this.imageCache.has(id))return;const owner=actorId(),ticket=this.ticket;try{const row=await cloud<Data>('/v1/member/reading/assets/'+id);const uri=stringValue(row,'media_type')==='image/gif'?'data:image/gif;base64,'+row['content']:asDataUri(row);if(owner===actorId()&&ticket===this.ticket){const map=new Map(this.imageCache);map.set(id,uri);this.imageCache=map}}catch(_){}}
  private pageOffset(page:Data):number{return typeof page['next_offset']==='number'?Number(page['next_offset']):-1}
  private async readComments(parent:string,more:boolean=false):Promise<void>{
    const id=this.contentId(),ticket=this.ticket,owner=actorId(),offset=more?(parent?this.replyNext:this.rootNext):0
    if(offset<0)return
    if(parent!==this.thread&&parent){this.replies=[];this.replyNext=-1;this.thread=parent}
    if(!parent)this.commentsError=''
    try{const page=await cloud<Data>('/v1/member/reading/comments?article_id='+encodeURIComponent(id)+(parent?'&parent_id='+encodeURIComponent(parent):'')+'&limit=30&offset='+offset)
      if(ticket!==this.ticket||id!==this.contentId()||parent&&parent!==this.thread)return
      if(owner!==actorId())throw new CloudError(401,'登录状态已改变。')
      const rows=array(page,'items')
      if(parent){this.replies=more?this.replies.concat(rows):rows;this.replyNext=this.pageOffset(page)}
      else{this.comments=more?this.comments.concat(rows):rows;this.rootNext=this.pageOffset(page)}
    }catch(e){
      if(ticket!==this.ticket||id!==this.contentId()||parent&&parent!==this.thread)return
      const err=e as Error
      if(owner!==actorId()||err instanceof CloudError&&[401,403].includes(err.status)){this.denyArticleAccess(err);return}
      if(parent)this.error=noteError(err)
      else this.commentsError=noteError(err)
    }
  }
  private async socialAction(kind:string,flag:string,row?:Data):Promise<void>{
    if(this.busy)return;this.busy=true
    const ticket=this.ticket,owner=actorId(),id=this.contentId()
    try{const account=await cloud<Data>('/v1/member/local-sync/account-state')
      if(ticket!==this.ticket||owner!==actorId())return
      const isComment=kind==='comment';const p:Data={'article_id':id} as Record<string, Object>
      if(isComment){if(!this.comment.trim())return;p['content']=this.comment.trim();if(this.replying&&this.thread)p['parent_id']=this.thread}
      else if(kind==='commentLike'&&row){p['comment_id']=row['id'];p['desired']=!row['liked']}
      else{p['desired']=!this.social[flag];p['kind']=kind}
      const socialKey=kind+'|'+JSON.stringify(p)
      if(this.lastSocialKey!==socialKey){this.op='';this.lastSocialKey=socialKey}
      if(!this.op)this.op=uuid();const packet:Data={'operation_id':this.op,'action':isComment?'reading.comment.add':kind==='commentLike'?'reading.comment.like':'reading.social.set','payload':p,'data_generation':account['data_generation']} as Record<string, Object>
      const result=await cloud<Data>('/v1/member/local-sync/operations','POST',packet)
      if(ticket!==this.ticket||owner!==actorId())return
      if(result['status']!=='applied')throw new Error('阅读状态已变更，请刷新核对。')
      this.op='';if(isComment){this.comment='';this.replying=false}
      if(kind==='commentLike'||isComment){
        const visibleRoot=Math.max(30,this.comments.length),visibleReplies=Math.max(30,this.replies.length)
        await this.readComments('');while(this.rootNext>=0&&this.comments.length<visibleRoot){const before=this.rootNext;await this.readComments('',true);if(before===this.rootNext)break}
        if(this.thread){await this.readComments(this.thread);while(this.replyNext>=0&&this.replies.length<visibleReplies){const before=this.replyNext;await this.readComments(this.thread,true);if(before===this.replyNext)break}}
      }else await this.load()
    }catch(e){
      if(ticket===this.ticket){
        const err=e as Error
        if(owner!==actorId()||err instanceof CloudError&&[401,403].includes(err.status))this.denyArticleAccess(err)
        else this.error=noteError(err)
      }
    }finally{if(ticket===this.ticket)this.busy=false}
  }
  private async shareArticle(copy:boolean):Promise<void>{
    const title=stringValue(this.article,'title').trim()
    if(!title||this.sharing)return
    const id=this.contentId(), owner=actorId(), ticket=this.ticket
    this.sharing=true
    try{
      if(copy){
        const excerpt=blockText(this.article).trim().slice(0,400)
        await pasteboard.getSystemPasteboard().setData(pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN,[title,excerpt].filter((part:string)=>part.length>0).join('\n')))
        if(ticket===this.ticket)this.getUIContext().getPromptAction().showToast({message:'已复制文章内容'})
      }else{
        const source=safeTarget(stringValue(this.article,'source_url'))||stringValue(this.article,'source_title')||'凡思每日一读'
        const selected=await shareText(this.getUIContext().getHostContext() as common.UIAbilityContext,title,title+'\n'+source)
        if(!selected)return
      }
      if(!owner||owner!==actorId())return
      try{
        const account=await cloud<Data>('/v1/member/local-sync/account-state')
        if(owner!==actorId())return
        const result=await cloud<Data>('/v1/member/local-sync/operations','POST',{
          'operation_id':uuid(),'action':'reading.share','payload':{'article_id':id} as Data,
          'data_generation':account['data_generation']
        } as Data)
        if(result['status']!=='applied')throw new Error('未同步')
      }catch(_){
        if(owner===actorId()&&ticket===this.ticket)this.getUIContext().getPromptAction().showToast({message:'本次转发操作的统计暂未同步。'})
      }
    }catch(_){
      if(ticket===this.ticket)this.getUIContext().getPromptAction().showToast({message:copy?'暂时无法复制文章内容。':'暂时无法打开系统分享，可以使用复制。'})
    }finally{this.sharing=false}
  }
  private async saveFood():Promise<void>{if(this.busy)return;this.busy=true
    try{const name=stringValue(this.form,'name').trim();if(!name)throw new Error('请填写食物名称。');const n:Data={};nutrients.forEach((k:string)=>{const s=stringValue(this.form,k);if(s){const v=Number(s);if(!Number.isFinite(v)||v<0)throw new Error('请核对营养数值。');n[k]=v}})
      const p:Data={'name':name,'category':stringValue(this.form,'category','本人填写')||'本人填写','nutrients':n} as Record<string, Object>;const grams=stringValue(this.form,'serving_grams');if(grams)p['serving_grams']=Number(grams);if(!this.op)this.op=uuid()
      const row=await this.expansion('create_food',p,this.op);this.onNavigate('community/knowledge/food/'+encodeURIComponent(String(row['id'])))
    }catch(e){this.error=noteError(e as Error)}finally{this.busy=false}}
  private async favorite():Promise<void>{try{await this.expansion('set_food_favorite',{'food_id':this.contentId(),'favorite':!this.article['favorite']} as Record<string, Object>);await this.load()}catch(e){this.error=noteError(e as Error)}}
  private async analyze():Promise<void>{
    if(this.busy)return;this.busy=true
    try{const keys=['vegetables','water','regularity'];if(!this.agreed||keys.some((k:string)=>!this.form[k]))throw new Error('请完成三个生活习惯问题，并确认饮食教育说明。')
      const payload:Data={};keys.concat(['notes']).forEach((k:string)=>{payload[k]=stringValue(this.form,k)})
      if(!this.op)this.op=uuid()
      const a=await this.expansion('create_analysis',{'kind':'nutrition_check','source_text':JSON.stringify(payload),'scope':{} as Data} as Record<string, Object>,this.op)
      if(a['status']==='ready'){this.review=a}else{
        const ctx=await this.expansion('get_analysis_context',{'analysis_id':a['id'],'consent':false,'include_memory':false} as Record<string, Object>)
        const msg:Data[]=[{'role':'system','content':'你是凡思资料整理助手，只根据用户填写的生活习惯给出教育说明。先说资料覆盖与缺失，再说可观察事实，最后至多三条一般生活建议。不诊断、不作处方。'} as Record<string, Object>,{'role':'user','content':JSON.stringify(payload)} as Record<string, Object>]
        const ai=await cloud<Data>('/v1/ai/chat','POST',{'model':'deepseek-v4-flash','messages':msg} as Record<string, Object>)
        const result:Data={'answer':ai['content'],'source_refs':ctx['source_refs']||[],'missing_data':['未记录的餐次、份量或营养数值不能按零计算。']} as Record<string, Object>
        this.review=await this.expansion('finish_analysis',{'analysis_id':a['id'],'base_version':ctx['base_version']||a['version'],'result':result,'model':ai['model']} as Record<string, Object>)
      }
      this.onNavigate('community/knowledge/nutrition/result')
    }catch(e){this.error=noteError(e as Error)}finally{this.busy=false}
  }
  private async confirm():Promise<void>{try{const packet=await this.expansion('confirm_analysis',{'analysis_id':this.review['id'],'base_version':this.review['version']} as Record<string, Object>);this.review=child(packet,'analysis')['id']?child(packet,'analysis'):packet}catch(e){this.error=noteError(e as Error)}}
  @Builder Btn(label:string,click:()=>void,active:boolean=false){
    Button(label,{type:ButtonType.Normal}).fontSize(15).fontColor(active?'#FFFFFF':ink).backgroundColor(active?teal:'#FFFDF9').border({width:1,color:line}).borderRadius(13).constraintSize({minHeight:42}).onClick(click)
  }
  @Builder Catalog(){
    TextInput({text:this.q,placeholder:'搜索生活资料'}).onChange((s:string)=>{this.q=s;clearTimeout(this.timer);this.timer=setTimeout(()=>this.load(),280)})
    Scroll(){Row({space:6}){ForEach(['全部','饮食','食物资料','已收藏'],(v:string)=>{this.Btn(v,()=>{this.theme=v;this.load()},this.theme===v)});this.Btn('我的社区',()=>this.onNavigate('community/mine'))}}.width('100%').scrollable(ScrollDirection.Horizontal).scrollBar(BarState.Off)
    if(this.theme==='食物资料'){Row({space:6}){this.Btn(this.favorites?'已收藏':'只看收藏',()=>{this.favorites=!this.favorites;this.load()});this.Btn('本人填写食物',()=>this.onNavigate('community/knowledge/food/new'));this.Btn('饮食习惯整理',()=>this.onNavigate('community/knowledge/nutrition'))}}
    ForEach(this.entries,(r:Data)=>{Row({space:10}){if(this.imageCache.get(stringValue(r,'cover_asset'))){Image(this.imageCache.get(stringValue(r,'cover_asset'))).width(74).height(66).borderRadius(10).objectFit(ImageFit.Cover)}Column({space:5}){Text(stringValue(r,'name',stringValue(r,'title'))).fontSize(19).fontColor(ink);Text(stringValue(r,'summary',stringValue(r,'category',stringValue(r,'source_title')))).maxLines(2).fontColor(muted).fontSize(14)}.alignItems(HorizontalAlign.Start).layoutWeight(1)}.width('100%').padding(10).backgroundColor('#FFFDF9').borderRadius(14).onClick(()=>this.onNavigate('community/knowledge/'+(this.theme==='食物资料'?'food/':'')+encodeURIComponent(stringValue(r,'id',stringValue(r,'article_id')))))},(r:Data)=>stringValue(r,'id',stringValue(r,'article_id')))
    if(!this.entries.length&&!this.busy){Text('没有找到相关资料').fontColor(muted)}if(this.entries.length<this.total){this.Btn('加载更多',()=>this.load(true))}
  }
  @Builder FoodForm(){
    ForEach(['name','category','serving_grams'].concat(nutrients),(key:string,i:number)=>{Text(['食物名称 *','食物分类','每份重量（克）'].concat(nutritionNames)[i]).fontColor(ink).fontSize(16);TextInput({text:stringValue(this.form,key)}).type(i<2?InputType.Normal:InputType.NUMBER_DECIMAL).onChange((s:string)=>this.field(key,s))});this.Btn('保存食物',()=>this.saveFood(),true)
  }
  @Builder Education(){
    if(this.route.endsWith('/result')){
      Text(stringValue(child(this.review,'result'),'answer','暂未生成整理，请返回填写。')).fontSize(18).fontColor(ink)
      if(this.review['status']==='ready'){this.Btn('确认保存本次整理',()=>this.confirm(),true)}else if(this.review['status']==='confirmed'){Text('已保存').fontColor(teal)}
      this.Btn('调整本次填写',()=>this.onNavigate('community/knowledge/nutrition'))
    }else{ForEach(questions,(q:string,i:number)=>{Text(q).fontSize(19).fontWeight(FontWeight.Bold).fontColor(ink);ForEach(options[i],(v:string)=>{this.Btn(v,()=>this.field(['vegetables','water','regularity'][i],v),this.form[['vegetables','water','regularity'][i]]===v)})})
      TextArea({text:stringValue(this.form,'notes'),placeholder:'想补充的饮食习惯（可选）'}).onChange((s:string)=>this.field('notes',s))
      Row(){Checkbox().select(this.agreed).onChange((v:boolean)=>this.agreed=v);Text('我了解本页是生活教育整理，不用于判断营养缺乏或改变治疗。').fontSize(15).layoutWeight(1)}
      this.Btn('查看本次整理',()=>this.analyze(),true)}
  }
  @Builder Comment(r:Data,root:boolean){
    Column({space:4}){
      Row({space:6}){if(stringValue(r,'avatar_data').startsWith('data:image/')){Image(stringValue(r,'avatar_data')).width(26).height(26).borderRadius(13)}Text(stringValue(r,'author','社区成员')).fontSize(14).fontWeight(FontWeight.Bold).fontColor(ink);Blank();Text((r['liked']?'♥ ':'♡ ')+String(r['likes']||0)).fontColor(teal).padding(6).onClick(()=>this.socialAction('commentLike','',r))}
      Text(stringValue(r,'content')).fontSize(17).fontColor(ink).copyOption(CopyOptions.LocalDevice)
      if(root){Row({space:8}){this.Btn('回复',()=>{this.readComments(stringValue(r,'id'));this.replying=true;this.comment='';this.op='' });if(Number(r['replies'])>0){this.Btn(this.thread===r['id']?'收起回复':'查看 '+r['replies']+' 条回复',()=>{if(this.thread===r['id']){this.thread='';this.replying=false}else this.readComments(stringValue(r,'id'))})}}}
    }.width('100%').alignItems(HorizontalAlign.Start).padding(8).backgroundColor(root?'#F7F8F4':'#F2F5EE').borderRadius(10)
  }
  @Builder Article(){
    if(this.imageCache.get(stringValue(this.article,'cover_asset'))){Image(this.imageCache.get(stringValue(this.article,'cover_asset'))).width('100%').height(180).objectFit(ImageFit.Cover).borderRadius(16)}
    Text(stringValue(this.article,'title')).fontSize(25).fontWeight(FontWeight.Bold).fontColor(ink)
    Text(stringValue(this.article,'source_title')).fontSize(15).fontColor(muted)
    ForEach(array(this.article,'blocks'),(b:Data)=>{if(b['asset_id']){if(this.imageCache.get(stringValue(b,'asset_id'))){Image(this.imageCache.get(stringValue(b,'asset_id'))).width('100%').objectFit(ImageFit.Contain)}else{this.Btn('查看原文配图',()=>this.asset(stringValue(b,'asset_id')))}}else{Text(blockText(b)).fontSize(/^h\d$/.test(stringValue(b,'tag'))?22:18).lineHeight(30).fontColor(ink)}})
    if(this.socialReady){Row({space:5}){this.Btn(this.social['liked']?'已点赞':'点赞',()=>this.socialAction('like','liked'));this.Btn(this.social['saved']?'已收藏':'收藏',()=>this.socialAction('bookmark','saved'));this.Btn(this.social['read']?'已读':'标为已读',()=>this.socialAction('read','read'));this.Btn(this.social['following']?'已关注':'关注',()=>this.socialAction('follow','following'))}}
    else if(this.socialError){Text('互动状态暂时无法加载。'+this.socialError).fontColor(muted);this.Btn('重试互动状态',()=>this.loadArticlePart(true,this.ticket))}
    if(stringValue(this.article,'title')){Row({space:6}){this.Btn('分享',()=>this.shareArticle(false));this.Btn('复制',()=>this.shareArticle(true))}}
    ForEach(this.comments,(r:Data)=>{this.Comment(r,true);if(this.thread===r['id']){Column({space:6}){ForEach(this.replies,(reply:Data)=>{this.Comment(reply,false)},(v:Data)=>String(v['id'])+v['liked']);if(this.replyNext>=0){this.Btn('更多回复',()=>this.readComments(this.thread,true))}}.width('94%').alignSelf(ItemAlign.End)}},(r:Data)=>String(r['id'])+r['liked']+r['replies'])
    if(this.commentsError){Text('评论暂时无法加载。'+this.commentsError).fontColor(muted);this.Btn('重试评论',()=>this.readComments('',this.rootNext>=0))}
    else if(this.rootNext>=0){this.Btn('更多评论',()=>this.readComments('',true))}
    if(this.replying){this.Btn('取消回复',()=>{this.replying=false;this.comment='';this.op=''})}
    TextInput({text:this.comment,placeholder:this.replying?'回复这条评论':'写下你的评论'}).maxLength(4000).onChange((s:string)=>{this.comment=s;this.op=''});this.Btn(this.replying?'发布回复':'发布评论',()=>this.socialAction('comment',''),true)
  }
  build(){Column(){
    Row({space:8}){Text('‹').fontSize(34).fontColor(ink).width(38).onClick(()=>this.onNavigate(this.catalogue()?'community':'community/knowledge'));Text(this.food()?'食物资料':this.education()?'饮食习惯整理':this.catalogue()?'生活知识':'原文阅读').fontSize(20).fontWeight(FontWeight.Bold).fontColor(ink).layoutWeight(1);Image($r('app.media.records_logo')).width(36).height(36)}.height(60).width('100%').padding({left:16,right:16})
    Scroll(){Column({space:12}){if(this.error){Text(this.error).fontColor('#984B3F');this.Btn('重新读取',()=>this.load())}
      if(this.catalogue()){this.Catalog()}else if(this.education()){this.Education()}else if(this.route.endsWith('/food/new')){this.FoodForm()}else if(this.food()){Text(stringValue(this.article,'name')).fontSize(25).fontColor(ink);Text('每 100 克营养信息').fontWeight(FontWeight.Bold);ForEach(nutrients,(key:string,i:number)=>{Text(nutritionNames[i]+'　'+stringValue(child(this.article,'nutrients'),key,'未知')).fontSize(16)});Text(stringValue(child(this.article,'source'),'name')).fontColor(muted);this.Btn(this.article['favorite']?'已收藏':'收藏食物',()=>this.favorite())}
      else if(stringValue(this.article,'title')){this.Article()}
      else if(this.bodyError){Text(this.bodyError).fontColor(muted);this.Btn('重试文章正文',()=>this.loadArticlePart(false,this.ticket))}
    }.width('100%').alignItems(HorizontalAlign.Start).padding(16)}.width('100%').layoutWeight(1)
  }.width('100%').height('100%').backgroundColor('#FDFBF7')}
}
