import { pasteboard } from '@kit.BasicServicesKit'
import { actorId, cloud, rpc, pickPhotos } from '../services/Cloud'
import { Data, mergeData, array, asDataUri, noteError, stringValue, uploadPhoto, uuid } from './AssistantService'
import { RECORD_ICONS } from './RecordIcons'
import { CommunityKnowledge } from './CommunityKnowledge'

const paper='#FDFBF7', ink='#24473B', muted='#79867C', line='#DBE5D9', teal='#337E72'
function titleOf(post:Data):string{return stringValue(post,'content').split('\n')[0]||stringValue(post,'title','生活分享')}
function bodyOf(post:Data):string{const text=stringValue(post,'content');return text.includes('\n')?text.slice(text.indexOf('\n')+1):text}
function fileId(row:Data):number{return Number(row['file_id']||row['id'])}
function imageKey(post:Data):string{const first=array(post,'attachments')[0];return first?String(fileId(first)):''}
function nextOffset(packet:Data):number{return packet['next_offset']===null||packet['next_offset']===undefined?-1:Number(packet['next_offset'])}
@Component
export struct CommunityPage {
  @Prop @Watch('changeRoute') route:string='community'
  onNavigate:(path:string)=>void=()=>{}
  @State forum:boolean=false
  @State filter:string=''
  @State topic:string=''
  @State section:string=''
  @State query:string=''
  @State hot:boolean=false
  @State topics:string[]=[]
  @State posts:Data[]=[]
  @State comments:Data[]=[]
  @State detail:Data={}
  @State profile:Data={}
  @State images:Map<string,string>=new Map()
  @State next:number=-1
  @State commentNext:number=-1
  @State loading:boolean=false
  @State busy:boolean=false
  @State error:string=''
  @State postTitle:string=''
  @State body:string=''
  @State pictures:string[]=[]
  @State composeComment:boolean=false
  private uploadIds:Map<string,number>=new Map()
  private uploadOps:Map<string,string>=new Map()
  private writeKey:string=''
  private revision:number=0
  private owner:number=0
  private searchTimer:number=0
  aboutToAppear(){this.owner=actorId();this.changeRoute()}
  aboutToDisappear(){this.revision++;clearTimeout(this.searchTimer)}
  private contentId():number{return Number(this.route.split('/')[2])}
  private open(path:string):void{this.onNavigate(path)}
  private changeRoute(){
    this.revision++;this.error='';this.composeComment=false;this.detail={};this.profile={};this.comments=[];this.commentNext=-1;this.loading=false
    if(this.route.startsWith('community/knowledge'))return
    this.body='';this.postTitle='';this.pictures=[];this.writeKey='';this.uploadIds.clear();this.uploadOps.clear()
    this.reload()
  }
  private async reload():Promise<void>{
    if(this.route.startsWith('community/posts/')||this.route.startsWith('community/image/')){await this.loadDetail();return}
    if(this.route.startsWith('community/member/')){const ticket=++this.revision;try{const data=await cloud<Data>('/v1/member/forum/profile/'+this.contentId());if(ticket===this.revision){this.profile=data;this.posts=array(data,'posts');this.loadImages(this.posts)}}catch(e){if(ticket===this.revision)this.error=noteError(e as Error)}return}
    if(this.route==='community/post/new'){await this.getTopics();return}
    if(this.forum||this.route==='community/search')await this.getTopics()
    await this.feed()
  }
  private async getTopics():Promise<void>{
    try{const raw=await rpc<Data[]>('forum','list_topics',[this.owner]);this.topics=raw.map((r:Data)=>stringValue(r,'name')).filter((v:string)=>v.length>0&&!/情绪|睡眠/.test(v));if(!this.topic||!this.topics.includes(this.topic))this.topic=this.topics[0]||''}
    catch(e){this.error=noteError(e as Error)}
  }
  private async feed(more:boolean=false):Promise<void>{
    const ticket=++this.revision, offset=more?this.next:0
    if(more&&offset<0)return
    this.loading=true;this.error=''
    if(!more){this.posts=[];this.next=-1}
    try{
      const path=this.route==='community/mine'?'/v1/member/forum/discovery?limit=24&offset=':'/v1/member/forum/discovery?limit=24&query='+encodeURIComponent(this.query.trim())+'&topic='+encodeURIComponent(this.forum||this.route==='community/search'?this.filter:'')+'&section='+encodeURIComponent(this.forum?'':this.section)+'&offset='
      let packet=await cloud<Data>(path+offset), result=array(packet,'items'),next=nextOffset(packet)
      // Member and popular views need the entire matching feed before filtering or sorting.
      if(this.route==='community/mine'||this.hot){while(next>=0&&ticket===this.revision){packet=await cloud<Data>(path+next);result=result.concat(array(packet,'items'));next=nextOffset(packet)}}
      if(ticket!==this.revision)return
      if(this.route==='community/mine')result=result.filter((r:Data)=>Number(r['author_user_id'])===this.owner)
      if(this.hot)result.sort((a:Data,b:Data)=>Number(b['likes'])-Number(a['likes']))
      this.posts=more?this.posts.concat(result):result;this.next=next;this.loadImages(result)
    }catch(e){if(ticket===this.revision)this.error=noteError(e as Error)}
    finally{if(ticket===this.revision)this.loading=false}
  }
  private loadImages(rows:Data[]){rows.forEach((r:Data)=>array(r,'attachments').forEach((im:Data)=>this.loadImage(fileId(im))))}
  private async loadImage(id:number):Promise<void>{if(this.images.has(String(id)))return;try{const src=asDataUri(await cloud<Data>('/v1/member/forum/images/'+id));if(this.owner!==actorId())return;const cache=new Map(this.images);cache.set(String(id),src);this.images=cache}catch(_){}}
  private async loadDetail():Promise<void>{const id=this.contentId(),t=++this.revision;this.loading=true;this.error=''
    try{const result=await Promise.all([cloud<Data>('/v1/member/forum/detail/'+id),cloud<Data>('/v1/member/forum/detail/'+id+'/comments?limit=30&offset=0')]);if(t!==this.revision)return;this.detail=result[0];this.comments=array(result[1],'items');this.commentNext=nextOffset(result[1]);this.loadImages([this.detail].concat(this.comments))}
    catch(e){if(t===this.revision)this.error=noteError(e as Error)}finally{if(t===this.revision)this.loading=false}}
  private async moreComments():Promise<void>{if(this.commentNext<0||this.busy)return;this.busy=true;try{const id=this.contentId();const p=await cloud<Data>('/v1/member/forum/detail/'+id+'/comments?limit=30&offset='+this.commentNext);if(id===this.contentId()){const more=array(p,'items');this.comments=this.comments.concat(more);this.commentNext=nextOffset(p);this.loadImages(more)}}catch(e){this.error=noteError(e as Error)}finally{this.busy=false}}
  private async engage(p:Data,kind:string):Promise<void>{
    if(this.busy)return;this.busy=true
    const field=kind==='follow'?'following':kind==='bookmark'?'saved':'liked',id=Number(p['id']),body:Data={}
    body[field]=!p[field]
    try{const v=await cloud<Data>('/v1/member/forum/detail/'+id+'/'+kind,'PUT',body);if(v[field]!==body[field])throw new Error('操作结果尚未确认。')
      if(Number(this.detail['id'])===id)this.detail=mergeData(this.detail,v)
      this.posts=this.posts.map((r:Data)=>Number(r['id'])===id?mergeData(r,v):r)}
    catch(e){this.error=noteError(e as Error)}finally{this.busy=false}
  }
  private newWrite(){this.writeKey=''}
  private async selectImages():Promise<void>{if(this.busy||this.pictures.length>=4)return;try{const pics=await pickPhotos(4-this.pictures.length);this.pictures=this.pictures.concat(pics).slice(0,4);this.newWrite()}catch(e){this.error=noteError(e as Error)}}
  private async publish(comment:boolean):Promise<void>{
    if(this.busy)return
    const title=this.postTitle.trim(),body=this.body.trim(),id=this.contentId()
    if(comment?!body&&!this.pictures.length:!title||!body||!this.topic){this.error=comment?'请填写评论或选择照片。':'请填写标题、经验内容和话题。';return}
    this.error='';this.busy=true
    if(!this.writeKey)this.writeKey=uuid()
    try{const ids:number[]=[]
      for(const uri of this.pictures){
        let fid=this.uploadIds.get(uri)
        if(!fid){const op=this.uploadOps.get(uri)||uuid();this.uploadOps.set(uri,op);fid=await uploadPhoto(uri,op);this.uploadIds.set(uri,fid)}
        ids.push(fid)
      }
      if(actorId()!==this.owner)throw new Error('登录状态已变更。')
      if(comment){
        const row=await cloud<Data>('/v1/member/forum/detail/'+id+'/comments','POST',{'content':body,'attachment_ids':ids,'request_id':this.writeKey} as Record<string, Object>);if(Number(row['post_id'])!==id)throw new Error('评论结果尚未确认。')
        this.body='';this.pictures=[];this.writeKey='';this.composeComment=false;await this.loadDetail()
      }else{
        const row=await rpc<Data>('forum','create_post',[this.owner,this.topic,title+'\n'+body,ids],undefined,this.writeKey)
        if(!row['id'])throw new Error('发布结果尚未确认。')
        this.open('community/posts/'+row['id'])
      }
    }catch(e){this.error=noteError(e as Error)}finally{this.busy=false}
  }
  private setQuery(v:string){this.query=v;clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>this.feed(),250)}
  private tab(f:boolean){if(this.forum===f)return;this.forum=f;this.query='';this.filter='';this.hot=false;this.reload()}
  private copy():void{pasteboard.getSystemPasteboard().setData(pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN,stringValue(this.detail,'content'))).then(()=>this.getUIContext().getPromptAction().showToast({message:'已复制帖子内容'})).catch((e:Error)=>{this.error=noteError(e)})}
  @Builder Icon(key:string,size:number,color:string=ink){
    Shape(){Path({commands:RECORD_ICONS.get(key)||''}).fill(Color.Transparent).stroke(color).strokeWidth('1.8px').strokeLineCap(LineCapStyle.Round).strokeLineJoin(LineJoinStyle.Round)}.width(size).height(size).viewPort({x:0,y:0,width:'24px',height:'24px'})
  }
  @Builder Btn(label:string,click:()=>void,selected:boolean=false){
    Button(label,{type:ButtonType.Normal}).fontColor(selected?'#FFFFFF':ink).fontSize(16).fontWeight(FontWeight.Medium).backgroundColor(selected?teal:'#FFFDF9').borderRadius(13).constraintSize({minHeight:44}).border({width:1,color:line}).onClick(click)
  }
  @Builder Header(){
    if(this.route==='community'){
      Row(){Image($r('app.media.records_logo')).width(36).height(36);Blank()
        Column({space:7}){Text('发现').fontSize(19).fontWeight(FontWeight.Bold).fontColor(this.forum?muted:ink);Divider().width(22).strokeWidth(4).color(this.forum?Color.Transparent:teal)}.width(65).onClick(()=>this.tab(false))
        Column({space:7}){Text('论坛').fontSize(19).fontWeight(FontWeight.Bold).fontColor(this.forum?ink:muted);Divider().width(22).strokeWidth(4).color(this.forum?teal:Color.Transparent)}.width(65).onClick(()=>this.tab(true))
        Blank();Row(){this.Icon('mine',22)}.width(44).height(44).justifyContent(FlexAlign.Center).borderRadius(16).backgroundColor('#FFFDF9').border({color:line,width:1}).accessibilityText('我的社区').onClick(()=>this.open('community/mine'))
      }.padding({left:18,right:18,top:6}).height(69).width('100%').backgroundColor(paper)
    }else{Row({space:12}){Row(){this.Icon('back',22)}.width(40).height(44).justifyContent(FlexAlign.Center).onClick(()=>this.open(this.route.startsWith('community/image/')?'community/posts/'+this.contentId():'community'));Text(this.route==='community/post/new'?'发布经验':this.route==='community/search'?'查找社区经验':this.route==='community/mine'?'我的社区':this.route.startsWith('community/member/')?'社区成员':this.route.startsWith('community/image/')?'经验配图':'帖子详情').fontSize(20).fontWeight(FontWeight.Bold).fontColor(ink).layoutWeight(1);Image($r('app.media.records_logo')).width(34).height(34)}.width('100%').height(60).padding({left:10,right:16}).backgroundColor(paper)}
  }
  private cover(p:Data):ResourceStr{
    const source=this.images.get(imageKey(p));if(source)return source
    const s=String(p['id']).split('').reduce((n:number,c:string)=>n+c.charCodeAt(0),0)%3
    return s===0?$r('app.media.community_food'):s===1?$r('app.media.community_reading'):$r('app.media.community_goods')
  }
  @Builder DiscoveryCard(p:Data,i:number){
    Column({space:7}){Image(this.cover(p)).width('100%').height(i%3===0?198:i%3===1?150:175).objectFit(ImageFit.Cover)
      Text(titleOf(p)).fontColor(ink).fontWeight(FontWeight.Bold).fontSize(17).maxLines(3).padding({left:9,right:9})
      Row({space:5}){Text(stringValue(p,'author','友').slice(0,1)).fontSize(12).fontColor(ink).width(20).height(20).textAlign(TextAlign.Center).backgroundColor('#E3EFDF').borderRadius(10);Text(stringValue(p,'author','社区成员')).fontSize(13).fontColor(muted).maxLines(1).layoutWeight(1);Text((p['liked']?'♥ ':'♡ ')+stringValue(p,'likes','0')).fontSize(15).fontColor(p['liked']?'#BA6857':muted).padding(5).onClick(()=>this.engage(p,'like'))}.width('100%').padding({left:7,right:7,bottom:6})
    }.width('100%').alignItems(HorizontalAlign.Start).backgroundColor('#FFFDF9').borderRadius(17).clip(true).border({color:line,width:1}).onClick(()=>this.open('community/posts/'+p['id']))
  }
  @Builder PostList(p:Data){
    Column({space:7}){Text(stringValue(p,'author','社区成员')+'　·　'+stringValue(p,'topic')).fontSize(14).fontColor(muted)
      Text(titleOf(p)).fontSize(20).fontWeight(FontWeight.Bold).fontColor(ink);Text(bodyOf(p)).maxLines(3).fontSize(16).fontColor(ink)
      if(imageKey(p)){Image(this.cover(p)).width('100%').height(135).objectFit(ImageFit.Cover).borderRadius(10)}
      Text('♡ '+stringValue(p,'likes','0')+'　 评论 '+stringValue(p,'comments','0')).fontSize(14).fontColor(muted)
    }.alignItems(HorizontalAlign.Start).width('100%').padding(14).backgroundColor('#FFFDF9').border({color:line,width:1}).borderRadius(18).onClick(()=>this.open('community/posts/'+p['id']))
  }
  @Builder Feed(){
    if(this.route==='community'&&!this.forum){
      TextInput({text:this.query,placeholder:'⌕  搜索帖子、生活灵感或作者'}).height(52).fontSize(18).backgroundColor('#F1F3EA').border({width:1,color:line}).onChange((v:string)=>this.setQuery(v)).onSubmit(()=>this.feed())
      Row({space:24}){Text('推荐').fontSize(19).fontWeight(FontWeight.Bold).fontColor(this.section===''?ink:muted).onClick(()=>{this.section='';this.feed()});Text('慢糖专区').fontSize(19).fontWeight(FontWeight.Bold).fontColor(this.section?ink:muted).onClick(()=>{this.section='slow-sugar';this.feed()})}.height(42).width('100%')
      Row().width('100%').height(104).border({width:1,color:'#CCDBCA',style:BorderStyle.Dashed}).borderRadius(22).linearGradient({angle:110,colors:[['#EEF2E3',0],['#F6EFE7',0.58],['#F2ECF2',1]]}).accessibilityText('广告展示区域')
      if(this.posts.length){Row({space:10}){ForEach([0,1],(column:number)=>{Column({space:10}){ForEach(this.posts.filter((p:Data,i:number)=>i%2===column),(p:Data,i:number)=>{this.DiscoveryCard(p,i*2+column)},(p:Data)=>String(p['id'])+p['liked']+p['likes'])}.layoutWeight(1).alignItems(HorizontalAlign.Start)})}.width('100%').alignItems(VerticalAlign.Top)}
    }else{
      if(this.route==='community/mine'||this.route.startsWith('community/member/')){
        Text(this.route==='community/mine'?'我的公开发布':stringValue(this.profile,'display_name',stringValue(this.profile,'username','公开发布'))).fontColor(ink).fontSize(22).fontWeight(FontWeight.Bold)
      }else{
        Row({space:8}){this.Btn('＋ 发布经验',()=>this.open('community/post/new'),true);Blank();if(this.route==='community/search'){this.Btn('生活知识',()=>this.open('community/knowledge'))}}
        if(this.route==='community/search'){TextInput({text:this.query,placeholder:'关键词、作者或日常体会'}).onChange((v:string)=>this.setQuery(v))}
        Scroll(){Row({space:8}){ForEach(['全部动态'].concat(this.topics),(topic:string)=>{this.Btn(topic,()=>{this.filter=topic==='全部动态'?'':topic;this.feed()},(this.filter||'全部动态')===topic)})}}.width('100%').scrollable(ScrollDirection.Horizontal).scrollBar(BarState.Off)
        Row(){Text('邻里共振 · 真实生活流').fontSize(21).fontWeight(FontWeight.Bold).fontColor(ink).layoutWeight(1);this.Btn(this.hot?'热门讨论':'最新发表',()=>{this.hot=!this.hot;this.feed()})}
      }
      ForEach(this.posts,(p:Data)=>{this.PostList(p)},(p:Data)=>String(p['id'])+p['likes'])
    }
    if(this.loading){Text('正在寻找生活中的新发现…').fontSize(15).fontColor(muted)}
    else if(!this.posts.length&&!this.error){Column({space:10}){if(!this.forum&&this.route==='community'){Image($r('app.media.community_reading')).width(132).height(85).objectFit(ImageFit.Cover).borderRadius(14)}
      Text(this.query?'没有找到相关分享':this.forum?'这个话题还没有分享':this.section?'一起聊聊慢糖生活':'等你分享生活中的小美好').textAlign(TextAlign.Center).fontSize(21).fontColor(ink)
      if(!this.forum){Text(this.query?'换个关键词，试试标题、内容或作者。':'用一张照片，记录一次发现。').fontSize(16).fontColor(muted);this.Btn(this.query?'查看全部分享':'发布第一篇分享',()=>{if(this.query){this.query='';this.feed()}else this.open('community/post/new')},true)}
    }.width('100%').padding(18).backgroundColor('#FFFDF9').borderRadius(20)}
    if(this.next>=0&&!this.loading){this.Btn('再看看更多分享',()=>this.feed(true))}
    if(this.forum){Row({space:8}){this.Btn('查找社区经验',()=>this.open('community/search'));this.Btn('生活知识',()=>this.open('community/knowledge'))}}
  }
  @Builder ImageControls(){
    Row({space:7}){this.Btn('添加照片（可选）',()=>this.selectImages());Text(this.pictures.length+'/4 张 · 每张不超过 2 MB').fontColor(muted).fontSize(13)}
    Row({space:5}){ForEach(this.pictures,(uri:string,i:number)=>{Column(){Image(uri).width(65).height(65).objectFit(ImageFit.Cover).borderRadius(9);Text('移除').fontSize(13).fontColor(muted).onClick(()=>{this.pictures=this.pictures.filter((s:string,j:number)=>i!==j);this.newWrite()})}},(uri:string,i:number)=>String(i)+uri.slice(-24))}
  }
  @Builder Form(){
    Text('选择话题').fontSize(16).fontColor(ink);Select(this.topics.map((t:string)=>({value:t} as SelectOption))).value(this.topic||'请选择话题').onSelect((i:number)=>{this.topic=this.topics[i];this.newWrite()})
    Text('经验标题 *').fontSize(16).fontColor(ink);TextInput({text:this.postTitle,placeholder:'用一句话写下这次经验'}).maxLength(120).onChange((s:string)=>{this.postTitle=s;this.newWrite()})
    Text('经验内容 *').fontSize(16).fontColor(ink);TextArea({text:this.body,placeholder:'分享您的日常做法或亲身体会'}).height(145).maxLength(4000).onChange((s:string)=>{this.body=s;this.newWrite()})
    this.ImageControls();this.Btn(this.busy?'正在发布…':'发布到社区',()=>this.publish(false),true)
  }
  @Builder Detail(){
    if(this.detail['id']){
      Row({space:10}){Text(stringValue(this.detail,'author','友').slice(0,1)).width(36).height(36).textAlign(TextAlign.Center).backgroundColor('#E5EFDF').borderRadius(18);Text(stringValue(this.detail,'author')).fontColor(ink).fontWeight(FontWeight.Bold).layoutWeight(1).onClick(()=>this.open('community/member/'+this.detail['id']));if(!this.detail['is_self']){this.Btn(this.detail['following']?'已关注':'关注',()=>this.engage(this.detail,'follow'))}}
      ForEach(array(this.detail,'attachments'),(ref:Data,i:number)=>{Image(this.images.get(String(fileId(ref)))).width('100%').height(220).objectFit(ImageFit.Contain).onClick(()=>this.open('community/image/'+this.contentId()+'/'+i))},(ref:Data)=>String(fileId(ref)))
      Text(titleOf(this.detail)).fontSize(24).fontWeight(FontWeight.Bold).fontColor(ink);Text(bodyOf(this.detail)).fontSize(19).lineHeight(30).fontColor(ink);Text(stringValue(this.detail,'created_at').slice(0,16).replace('T',' ')).fontSize(14).fontColor(muted)
      Row({space:6}){this.Btn((this.detail['liked']?'已赞 ':'赞 ')+stringValue(this.detail,'likes','0'),()=>this.engage(this.detail,'like'));this.Btn(this.detail['saved']?'已收藏':'收藏',()=>this.engage(this.detail,'bookmark'));this.Btn('复制分享',()=>this.copy())}
      Text('评论 '+stringValue(this.detail,'comments','0')).fontSize(20).fontColor(ink).fontWeight(FontWeight.Bold)
      ForEach(this.comments,(c:Data)=>{Column({space:7}){Text(stringValue(c,'author')).fontSize(15).fontWeight(FontWeight.Bold);Text(stringValue(c,'content')).fontSize(17);ForEach(array(c,'attachments'),(ref:Data)=>{Image(this.images.get(String(fileId(ref)))).width(88).height(88).objectFit(ImageFit.Cover)});Divider().color(line)}.width('100%').alignItems(HorizontalAlign.Start)},(c:Data)=>String(c['id']))
      if(this.commentNext>=0){this.Btn('加载更多评论',()=>this.moreComments())}
      if(this.composeComment){TextArea({text:this.body,placeholder:'分享您的看法…'}).maxLength(4000).onChange((s:string)=>{this.body=s;this.newWrite()});this.ImageControls();this.Btn('发布评论',()=>this.publish(true),true)}
      else{this.Btn('写评论',()=>{this.composeComment=true})}
    }else if(this.loading){Text('正在读取这份生活分享…').fontColor(muted)}
  }
  build(){Column(){
    if(this.route.startsWith('community/knowledge')){CommunityKnowledge({route:this.route,onNavigate:this.onNavigate})}
    else{this.Header();Stack({alignContent:Alignment.Top}){Image($r('app.media.community_ambient')).width('100%').height('100%').objectFit(ImageFit.Cover).opacity(.14)
      Scroll(){Column({space:12}){
        if(this.error){Column({space:8}){Text('暂时没有连接上').fontColor(ink).fontSize(20);Text(this.error).fontColor(muted);this.Btn('重新加载',()=>this.reload(),true)}.width('100%').padding(16).backgroundColor('#FFFDF9').borderRadius(16)}
        if(this.route==='community/post/new'){this.Form()}
        else if(this.route.startsWith('community/posts/')){this.Detail()}
        else if(this.route.startsWith('community/image/')){if(array(this.detail,'attachments').length){Image(this.images.get(String(fileId(array(this.detail,'attachments')[Number(this.route.split('/')[3])]||{})))).width('100%').height(410).objectFit(ImageFit.Contain)}}
        else{this.Feed()}
      }.width('100%').padding(16).alignItems(HorizontalAlign.Start)}.width('100%').height('100%').scrollBar(BarState.Off)
    }.layoutWeight(1).width('100%')}
  }.width('100%').height('100%').backgroundColor(paper)}
}
