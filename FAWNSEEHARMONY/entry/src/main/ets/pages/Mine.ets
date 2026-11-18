import {removeMemberDrafts} from '../services/PrivateData';
import {common} from '@kit.AbilityKit';
import {notificationManager} from '@kit.NotificationKit';
import {pasteboard} from '@kit.BasicServicesKit';
import {cloud,rpc,currentMember,actorId,signOut,clearSession,completeCredentials} from '../services/Cloud';
import {MineArchive} from './MineArchive';
import {installedVersion} from '../services/AppUpdate';
import {MineProfile,MemberProfile,AccountPrefs} from './MineProfile';
import {PAPER,INK,TEAL,MUTED,WHITE,GREEN,EDGE,PageResult,Choice,expansion,operationId,TMLabel,TMNote,TMButton,TMHeader,TMBanner,TMField} from './TodayShared';

interface Membership{status:string;plan_code:string;ends_at:string;benefits?:string[];}
interface MemberSummary{membership?:Membership;}
interface LevelInfo{name:string;tier:string;remaining:number;next_threshold:number|null;}
interface Growth {balance:number;earned:number;level:number;level_info:LevelInfo;streak_days:number;}
interface Point {id:number;day?:string;created_at:string;reason:string;delta:number;balance_after:number;}
interface Invite {code:string|null;accepted_count:number;qualified_count:number;}
interface Status {credentials_set:boolean;}
interface Erased {erased:boolean;local_purge_required:boolean;}
interface Reset {reset:boolean;request_id:string;}
interface PublicMember {post_count:number;is_self:boolean;}
interface Post {id:number;author_id?:number;user_id?:number;author_user_id?:number;is_self?:boolean;}
const menuImages:Resource[]=[$r('app.media.tm_icon_membership'),$r('app.media.tm_icon_growth'),$r('app.media.tm_icon_privacy'),$r('app.media.tm_icon_display'),$r('app.media.tm_icon_help')];
const menu:Choice[]=[new Choice('membership','会员与权益'),new Choice('growth','成长记录'),new Choice('privacy','隐私与安全中心'),new Choice('display','应用与显示偏好'),new Choice('help','帮助与使用手册')];
const helpTopics:Choice[]=[
  new Choice("生活记录","七类是饮食、饮水、运动、睡眠、居住环境、医疗检查、身体指标。每一类均可在同一天连续新增多条；保存并继续会清空本次字段。趋势分析从生活记录进入。"),
  new Choice("智能助手","可选择模型复杂度、语气，并分别管理本地资料卡与生活资料参考。照片识别结果只加入对话，语音转写只填入聊天草稿，均需本人确认发送。"),
  new Choice("语音智能记录","生活记录中的语音智能记录会按本人原话整理为七类记录，与 AI 聊天的语音输入是不同流程。估算热量会单独标注。"),
  new Choice("社区交流","支持纯文字或图文发布、评论、点赞。收藏暂未开放。已发布的经验对其他会员可见。每次最多选择四张 JPG、PNG 或 WEBP 图片，每张不超过 2 MB。"),
  new Choice("健康好物与服务","好物提供购物车和 SIM 虚拟订单，不真实付款。服务预约只提交类型、日期、时间、地址和备注；仅本人待确认预约可编辑。"),
  new Choice("备份与数据","个人云备份与恢复暂未接入。")
];
@Component
export struct MinePage{
  @Prop @Watch('routeChange') route:string='mine';
  onNavigate:(r:string)=>void=()=>{};
  @State nameValue:string='';@State avatar:string='';@State complete:boolean=false;
  @State membership:Membership|null=null;
  @State growth:Growth|null=null;@State ledger:Point[]=[];@State page:number=0;@State total:number=0;
  @State records:number=-1;@State posts:number=-1;@State profileDays:number=-1;
  @State invite:Invite|null=null;@State inviteCode:string='';
  @State password:string='';@State newPass:string='';@State again:string='';@State username:string='';
  @State consent:boolean=false;@State credentials:number=-1;
  @State error:string='';@State busy:boolean=false;@State success:string='';
  @State query:string='';
  private serial:number=0;
  private opKey:string='';
  private op:string='';
  private mineSection():string { return this.route==='mine'?'home':this.route.slice(5).split('/')[0]; }
  aboutToAppear(){this.routeChange();}
  aboutToDisappear(){this.serial++;}
  routeChange():void{this.error='';this.success='';this.password='';this.newPass='';this.again='';this.consent=false;this.username='';this.page=0;this.opKey='';this.busy=false;this.load(++this.serial);}
  private async load(id:number):Promise<void>{
    this.error='';
    try{
      if(this.mineSection()==='home'){
        const group=await Promise.all([rpc<MemberProfile>('health','get_profile',[actorId()]),rpc<AccountPrefs>('preferences','get_preferences',[actorId()]),rpc<MemberSummary>('health','get_service_summary',[actorId()])]);const p=group[0];const pref=group[1];const summary=group[2];
        if(id!==this.serial)return;
        this.nameValue=p.display_name||currentMember()?.display_name||currentMember()?.username||'';
        this.avatar=pref.avatar_data||'';this.membership=summary.membership||null;
        this.complete=!!(p.display_name&&p.ai_preferred_name&&p.birth_date&&p.phone&&p.height_cm&&p.weight_kg&&p.personal_introduction&&p.emergency_contact_name&&p.emergency_contact_phone&&p.gender&&p.gender!=='unspecified');
        const totals=await Promise.all([expansion<PageResult<Object>>('list_records',{'limit':1} as Record<string, Object|null>),expansion<PageResult<Object>>('list_observations',{'limit':1} as Record<string, Object|null>),expansion<Growth>('get_growth',{} as Record<string, Object|null>)]);const r=totals[0];const o=totals[1];const g=totals[2];if(id!==this.serial)return;
        this.records=r.total+o.total;this.profileDays=g.streak_days;
        this.postCount(id);
      }else if(this.mineSection()==='privacy'||this.mineSection()==='credentials'){
        const state=await cloud<Status>('/v1/auth/credentials');if(id===this.serial)this.credentials=state.credentials_set?1:0;
      }else if(this.mineSection()==='membership'){const s=await rpc<MemberSummary>('health','get_service_summary',[actorId()]);if(id===this.serial)this.membership=s.membership||null;}
      else if(this.mineSection()==='growth'){
        const group=await Promise.all([expansion<Growth>('get_growth',{} as Record<string, Object|null>),expansion<PageResult<Point>>('list_points',{'limit':8,'offset':this.page*8} as Record<string, Object|null>)]);const g=group[0];const ledger=group[1];if(id===this.serial){this.growth=g;this.ledger=ledger.items;this.total=ledger.total;}
      }else if(this.mineSection()==='invites'){const i=await expansion<Invite>('get_invite_code',{} as Record<string, Object|null>);if(id===this.serial)this.invite=i;}
    }catch(e){if(id===this.serial)this.error=(e as Error).message;}
  }
  private async postCount(t:number):Promise<void>{
    try{
      let offset=0;let count=0;
      while(true){const page=await cloud<PageResult<Post>>('/v1/member/forum/discovery?limit=100&offset='+offset);if(t!==this.serial)return;
        const owned=page.items.find((p:Post)=>p.is_self===true||p.user_id===actorId()||p.author_id===actorId()||p.author_user_id===actorId());
        if(owned){const profile=await cloud<PublicMember>('/v1/member/forum/profile/'+owned.id);if(t!==this.serial)return;if(!profile.is_self)throw Error('账号信息已变化。');count=profile.post_count;break;}
        offset+=page.items.length;if(!page.items.length||offset>=page.total)break;}
      if(t===this.serial)this.posts=count;
    }catch{if(t===this.serial)this.posts=-1;}
  }
  private async mutate(key:string,work:(op:string)=>Promise<void>):Promise<void>{
    if(this.busy)return;this.busy=true;this.error='';this.success='';const t=this.serial;
    if(this.opKey!==key){this.opKey=key;this.op=operationId();}
    try{await work(this.op);if(t===this.serial)this.opKey='';}
    catch(e){if(t===this.serial)this.error=(e as Error).message;}
    finally{if(t===this.serial)this.busy=false;}
  }
  private confirm(title:string,body:string,label:string,act:()=>void):void{this.getUIContext().showAlertDialog({title:title,message:body,primaryButton:{value:'取消',action:()=>{}},secondaryButton:{value:label,action:act}});}
  private async logout():Promise<void>{try{await signOut();this.onNavigate('login');}catch(e){this.error=(e as Error).message;}}
  private reset():void{this.confirm('清除账号信息？','将清空个人资料、生活记录、AI 对话、偏好及其他个人数据。账号、密码和手机号绑定会保留，清空后无法恢复。','确认清空',()=>this.mutate('reset',async(op:string)=>{
    const v=await cloud<Reset>('/v1/auth/account/reset','POST',{'confirmation':'清空账号内全部数据','request_id':op} as Record<string, Object|null>,op);
    if(!v.reset)throw Error('云端清空结果尚未确认。');await removeMemberDrafts(actorId());await clearSession();this.onNavigate('login');
  }));}
  private erase():void{
    if(!this.consent||!this.password){this.error='请填写当前密码，并确认注销账号及删除全部数据。';return;}
    this.confirm('最后确认注销账号','确认删除账号、手机绑定及全部个人数据？此操作完成后无法恢复。','确认注销并删除全部数据',()=>this.mutate('erase',async(op:string)=>{
      const v=await cloud<Erased>('/v1/auth/account/erase','POST',{'confirmation':'删除账号及全部数据','current_password':this.password,'request_id':op} as Record<string, Object|null>,op);
      if(v.erased!==true||!v.local_purge_required)throw Error('账号注销结果尚未确认。');await removeMemberDrafts(actorId());await clearSession();this.onNavigate('login');
    }));
  }
  private savePassword():void{
    if(this.newPass.length<8||this.newPass.length>256||this.newPass!==this.again){this.error='请设置8至256位密码，两次输入应相同。';return;}
    if(this.mineSection()==='credentials'){
      if(!this.username.trim()){this.error='请填写用户名。';return;}
      this.mutate('setup'+this.username+this.newPass,async(op:string)=>{await completeCredentials({'username':this.username.trim(),'password':this.newPass,'request_id':op} as Record<string, Object|null>);this.onNavigate('mine/privacy');});
    }else{this.mutate('password',async(op:string)=>{await cloud<Object>('/v1/auth/change-password','POST',{'current_password':this.password,'new_password':this.newPass} as Record<string, Object|null>,op);this.password='';this.newPass='';this.again='';this.success='密码已更新';});}
  }
  private title():string {return menu.find((c:Choice)=>c.key===this.mineSection())?.label||new Map<string,string>([['profile','个人资料'],['manage','功能设置与管理'],['password','修改密码'],['credentials','设置用户名/密码'],['archive','我的AI生活档案'],['invites','邀请朋友'],['erase','注销账号']]).get(this.mineSection())||'我的';}
  @Builder toastText(){if(this.error){TMNote({label:this.error});TMButton({label:'重新读取',action:()=>this.load(this.serial)})} if(this.success){TMNote({label:this.success})}}
  @Builder header(){
    Row({space:8}){Image($r('app.media.records_logo')).width(40).height(40);Text('我的  ·  凡思账户').fontSize(18).fontColor(INK).fontWeight(FontWeight.Bold).layoutWeight(1);Text('邀请').id('mine-open-invites').fontSize(17).fontWeight(FontWeight.Bold).fontColor(TEAL).onClick(()=>this.onNavigate('mine/invites'))}.height(68).width('100%').padding({left:16,right:20}).backgroundColor(PAPER)
  }
  private statValue(target:string):string {
    const count=target==='records'?this.records:target==='community/mine'?this.posts:this.profileDays;
    return count<0?'—':String(count);
  }
  @Builder stat(label:string,target:string,color:string,icon:Resource){
    Column({space:6}){Row({space:4}){Image(icon).height(15).width(15);Text(label).fontSize(15).fontColor(color)};Text(this.statValue(target)).fontSize(26).fontWeight(FontWeight.Bold).fontColor(INK)}.layoutWeight(1).height(94).justifyContent(FlexAlign.Center).backgroundColor(WHITE).borderRadius(24).shadow({radius:2,color:'#BBD4C9',offsetY:2}).onClick(()=>this.onNavigate(target))
  }
  @Builder home(){
    Column({space:16}){
      Column({space:13}){
        Row({space:8}){
          Image(this.avatar||$r('app.media.records_logo')).width(56).height(56).padding(this.avatar?0:6).objectFit(ImageFit.Cover).backgroundColor(GREEN).borderRadius(16).onClick(()=>this.onNavigate('mine/profile'))
          Column({space:6}){
            Row({space:4}){Text(this.nameValue||currentMember()?.username||'').fontColor(INK).fontWeight(FontWeight.Bold).fontSize(22).maxLines(1).textOverflow({overflow:TextOverflow.Ellipsis}).layoutWeight(1);Text(this.complete?'已完善':'完善个人信息').fontSize(17).fontColor(TEAL).decoration({type:TextDecorationType.Underline}).onClick(()=>this.onNavigate('mine/profile'))}.width('100%')
            Text('凡思账户  ·  '+(this.membership?.status==='active'?'已开通会员':'未开通会员')).fontSize(16).fontColor(INK).backgroundColor('#B9E8DA').borderRadius(9).padding({left:5,right:5,top:2,bottom:2})
          }.layoutWeight(1).alignItems(HorizontalAlign.Start)
        }.width('100%').height(76)
        Row({space:9}){Image($r('app.media.tm_icon_book')).width(20).height(20);Text('我的AI生活档案').fontColor(INK).fontWeight(FontWeight.Bold).fontSize(18).layoutWeight(1);Text('›').fontSize(24).fontColor(TEAL)}.width('100%').height(52).padding({left:16,right:16}).border({width:1,color:'#C4DFD2'}).backgroundColor('#E3F1EA').borderRadius(16).onClick(()=>this.onNavigate('mine/archive'))
      }.width('100%').padding(13).backgroundColor(WHITE).borderRadius(24).shadow({radius:2,color:'#C7DFD2',offsetY:2})
      Row({space:6}){
        this.stat('坚持天数','today/goals','#906C25',$r('app.media.tm_icon_sun'))
        this.stat('生活记录','records',TEAL,$r('app.media.tm_icon_records'))
        this.stat('社区发表','community/mine','#486B60',$r('app.media.tm_icon_posts'))
      }
      Column(){
        ForEach(menu,(c:Choice,index:number)=>{Row({space:12}){Image(menuImages[index]).width(30).height(30).padding(6).backgroundColor('#EEF4E7').borderRadius(10);Text(c.label).fontSize(18).fontWeight(FontWeight.Bold).fontColor('#29362D').layoutWeight(1);Text('›').fontColor(MUTED)}.width('100%').height(52).padding({left:12,right:12}).onClick(()=>this.onNavigate('mine/'+c.key))},(c:Choice)=>c.key)
      }.width('100%').padding(8).backgroundColor(WHITE).borderRadius(24)
      Text('凡思 · 自然节律　身心舒和').fontSize(15).fontColor(MUTED).margin({top:20,bottom:12})
      TMButton({label:'退出账号',danger:true,action:()=>this.confirm('退出账号？','下次使用会员功能时需要重新登录。','退出账号',()=>this.logout())})
      TMButton({label:'清除账号信息',danger:true,action:()=>this.reset(),blocked:this.busy})
      this.toastText()
    }.width('100%').padding({left:16,right:16,top:8,bottom:18})
  }
  @Builder growthView(){
    TMBanner({title:'成长记录',subtitle:'凡思账户',art:$r('app.media.tm_member_account')})
    if(this.growth){TMLabel({label:this.growth.level_info.name+' '+this.growth.level_info.tier});Text(this.growth.balance+' 当前积分').fontSize(26).fontWeight(FontWeight.Bold).fontColor(INK);TMNote({label:'等级 '+this.growth.level+' · 距下一级 '+this.growth.level_info.remaining+' 积分'});TMNote({label:'累计获得 '+this.growth.earned+'　连续目标打卡 '+this.growth.streak_days+' 天'})}
    TMLabel({label:'积分明细'})
    ForEach(this.ledger,(p:Point)=>{TMNote({label:(p.reason||'积分变更')+'　'+p.delta+'\n'+p.created_at.slice(0,10)+' · 变更后 '+p.balance_after})},(p:Point)=>String(p.id))
    Row({space:16}){Button('上一页').enabled(this.page>0).onClick(()=>{this.page--;this.load(++this.serial);});Text((this.page+1)+' / '+Math.max(1,Math.ceil(this.total/8)));Button('下一页').enabled((this.page+1)*8<this.total).onClick(()=>{this.page++;this.load(++this.serial);})}
  }
  @Builder invitesView(){
    TMBanner({title:'邀请朋友',subtitle:'凡思账户',art:$r('app.media.tm_member_account')})
    TMNote({label:'把生活分享给朋友'});TMLabel({label:this.invite?.code||'我的邀请码'})
    if(this.invite?.code){TMButton({label:'复制邀请码',primary:true,action:()=>{pasteboard.getSystemPasteboard().setData(pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN,this.invite?.code||'')).then(()=>this.success='邀请码已复制').catch((e:Error)=>this.error=e.message);}})}
    else{TMButton({label:'生成邀请码',primary:true,blocked:this.busy,action:()=>this.mutate('invite',async(op:string)=>{this.invite=await expansion<Invite>('create_invite_code',{} as Record<string, Object|null>,op);})})}
    TMNote({label:'已接受 '+(this.invite?.accepted_count||0)+'　有效邀请 '+(this.invite?.qualified_count||0)})
    TMField({label:'绑定朋友的邀请码',value:this.inviteCode,change:(v:string)=>this.inviteCode=v})
    TMButton({label:'绑定邀请码',blocked:this.busy||!this.inviteCode.trim(),action:()=>this.mutate('accept'+this.inviteCode,async(op:string)=>{await expansion<Object>('accept_invite',{'code':this.inviteCode.trim()} as Record<string, Object|null>,op);this.success='邀请码已绑定';await this.load(this.serial);})})
  }
  @Builder security(){
    if(this.mineSection()==='privacy'){
      if(this.credentials===0){TMButton({label:'设置用户名/密码',primary:true,action:()=>this.onNavigate('mine/credentials')})}
      TMButton({label:'通知横幅与锁屏设置',primary:true,action:()=>{const ctx=this.getUIContext().getHostContext() as common.UIAbilityContext; notificationManager.openNotificationSettings(ctx).catch((e:Error)=>this.error=e.message);}})
      TMNote({label:'在系统设置中开启通知，并按需要允许横幅和锁屏显示。'})
      if(this.credentials===1){TMButton({label:'修改密码',action:()=>this.onNavigate('mine/password')})}
      TMButton({label:'注销账号',danger:true,action:()=>this.onNavigate('mine/erase')})
    }else if(this.mineSection()==='erase'){
      TMNote({label:'个人资料、生活记录、对话、偏好、提醒、阅读及其他账号数据都将删除。完成后无法恢复，请确认已自行保留需要的内容。'})
      TMField({label:'当前账号密码',password:true,value:this.password,change:(v:string)=>this.password=v})
      Row({space:8}){Checkbox().select(this.consent).onChange((v:boolean)=>this.consent=v);Text('我确认注销账号、解除手机绑定并删除全部数据').layoutWeight(1).fontSize(17).fontColor(INK)}
      TMButton({label:'注销账号',danger:true,primary:true,blocked:this.busy||!this.consent||!this.password,action:()=>this.erase()})
    }else{
      if(this.mineSection()==='credentials'){TMField({label:'用户名',value:this.username,change:(v:string)=>this.username=v})}
      else{TMField({label:'当前密码 *',value:this.password,password:true,change:(v:string)=>this.password=v})}
      TMField({label:'新密码 *',value:this.newPass,password:true,change:(v:string)=>this.newPass=v})
      TMField({label:'确认新密码 *',value:this.again,password:true,change:(v:string)=>this.again=v})
      TMButton({label:this.mineSection()==='credentials'?'保存用户名/密码':'保存修改',primary:true,blocked:this.busy,action:()=>this.savePassword()})
    }
  }
  @Builder contents(){
    if(this.mineSection()==='profile'){MineProfile({onNavigate:this.onNavigate})}
    else if(this.mineSection()==='archive'){MineArchive()}
    else if(this.mineSection()==='growth'){this.growthView()}
    else if(this.mineSection()==='invites'){this.invitesView()}
    else if(this.mineSection()==='display'){
      TMLabel({label:'字号设置'});TMNote({label:'当前字号'});TMButton({label:'22像素',action:()=>this.getUIContext().getPromptAction().showToast({message:'当前应用只支持22像素的字号，设置字号功能敬请期待'})})
    }else if(this.mineSection()==='membership'){
      TMBanner({title:'会员与权益',subtitle:'凡思账户',art:$r('app.media.tm_member_account')})
      if(this.membership?.status==='active'){TMLabel({label:this.membership?.plan_code==='gold'?'金卡':'银卡'});TMNote({label:'有效期至 '+this.membership?.ends_at?.slice(0,10)})}
      ForEach([new Choice('银卡','180 天'),new Choice('金卡','365 天')],(p:Choice)=>{Column({space:9}){TMLabel({label:p.key});TMNote({label:p.label});TMNote({label:'预约与咨询权益预览'});TMButton({label:'开通'+p.key,action:()=>this.getUIContext().getPromptAction().showToast({message:'云端未开放会员自助开通，请联系服务人员。'})})}.padding(14).backgroundColor(WHITE).borderRadius(18)},(p:Choice)=>p.key)
    }else if(this.mineSection()==='help'){
      TMField({label:'帮助与使用手册',placeholder:'搜索功能与说明',value:this.query,change:(v:string)=>this.query=v})
      ForEach(helpTopics.filter((p:Choice)=>(p.key+p.label).includes(this.query)),(p:Choice)=>{TMLabel({label:p.key});TMNote({label:p.label})},(p:Choice)=>p.key)
      TMNote({label:'凡思 · 鸿蒙版 ' + installedVersion()})
    }else if(this.mineSection()==='manage'){ForEach(['profile','password','privacy','display','help'],(r:string,i:number)=>{TMButton({label:['个人资料','修改密码','隐私与数据','应用与显示','帮助与关于'][i],action:()=>this.onNavigate('mine/'+r)})})}
    else if(['privacy','credentials','password','erase'].includes(this.mineSection())){this.security()} else {TMButton({label:'返回我的',action:()=>this.onNavigate('mine')})}
    this.toastText()
  }
  build(){
    Column(){
      if(this.mineSection()==='home'){this.header()}else{TMHeader({title:this.title(),parentTitle:'我的',back:()=>this.onNavigate(['erase','credentials','password'].includes(this.mineSection())?'mine/privacy':'mine')})}
      Stack({alignContent:Alignment.Top}){
        Image($r('app.media.records_petal')).width('100%').opacity(0.16).objectFit(ImageFit.Contain)
        Scroll(){
          if(this.mineSection()==='home'){this.home()}else{Column({space:14}){this.contents()}.padding(16).width('100%').alignItems(HorizontalAlign.Start)}
        }.width('100%').height('100%').scrollBar(BarState.Off)
      }.layoutWeight(1).width('100%')
    }.height('100%').width('100%').backgroundColor(PAPER)
  }
}
