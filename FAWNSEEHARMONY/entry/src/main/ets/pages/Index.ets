import { RECORD_ICONS } from './RecordIcons'
import { RecordsScreen } from './Records'
import { TodayPage } from './Today'
import { MinePage } from './Mine'
import { AssistantPage } from './Assistant'
import { CommunityPage } from './Community'
import { AuthPage } from './Auth'
import { MeetPage } from './Meet'
import { actorId,restoreSession } from '../services/Cloud'
import { cancelVoice } from '../services/Voice'
import { common } from '@kit.AbilityKit'
import { clearDeviceReminders, syncDeviceReminders } from '../services/DeviceReminders'

class TabEntry {
  id:string; label:string
  constructor(id:string,label:string){this.id=id;this.label=label}
}
const tabs:TabEntry[]=[new TabEntry('today','今日'),new TabEntry('records','记录'),new TabEntry('ai','助手'),new TabEntry('community','社区'),new TabEntry('mine','我的')]
const tabTints:Map<string,string>=new Map([['today','#FFF0CA'],['records','#D7EFEA'],['ai','#EDDEF6'],['community','#DCECF8'],['mine','#F8E1D8']])
const tabEdges:Map<string,string>=new Map([['today','#E5D7A8'],['records','#B9DCD0'],['ai','#D8C4E6'],['community','#C2D9EC'],['mine','#E8CBBA']])
function tabFor(route:string):string {
  const section=route.split('/')[0]
  return ['form','edit','detail','voice','trends'].includes(section)?'records':section
}
@Entry
@Component
struct Index {
  @State route:string='boot'
  @State startupError:string=''
  @StorageLink('sessionExpired') @Watch('expired') expiredFlag:boolean=false
  history:string[]=[]
  aboutToAppear(){this.boot()}
  aboutToDisappear(){cancelVoice().catch(()=>{})}
  onPageShow(){
    if(actorId())syncDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext,false).catch(()=>{})
  }
  async boot(){
    this.startupError=''
    try {
      const member=await restoreSession()
      this.route=member?'meet':'ai'
      if(member)syncDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext,false).catch(()=>{})
      else clearDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext).catch(()=>{})
    }catch(e){this.startupError=(e as Error).message}
  }
  expired(){if(this.expiredFlag){clearDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext).catch(()=>{});this.history=[];this.route='auth/phone';this.expiredFlag=false}}
  navigate(raw:string):void{
    let next=raw.replace(/^#/,'')
    if(next==='signed-in'){this.history=[];this.route='meet';syncDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext,false).catch(()=>{});return}
    if(next==='guest'){this.history=[];this.route='ai';return}
    if(next==='login'||next==='auth'){if(!actorId())clearDeviceReminders(this.getUIContext().getHostContext() as common.UIAbilityContext).catch(()=>{});this.history=[];this.route='auth/phone';return}
    if(!actorId()&&!next.startsWith('auth/')&&!next.startsWith('ai')){
      this.history.push(this.route);this.route='auth/phone';return
    }
    if(next.startsWith('records/new/'))next='form/'+next.split('/')[2]
    if(next===this.route)return
    if(tabs.some((t:TabEntry)=>t.id===next)){this.history=[]}
    else if(this.history[this.history.length-1]===next){this.history.pop()}
    else {this.history.push(this.route)}
    this.route=next
  }
  onBackPress():boolean{
    if(this.history.length){this.route=this.history.pop()||'ai';return true}
    if(this.route==='meet')return true
    if(this.route!=='ai'&&this.route!=='boot'){this.route='ai';return true}
    return false
  }
  @Builder Mark(id:string,active:boolean){
    Row(){
      Shape(){Path({commands:RECORD_ICONS.get(id)||''}).fill(Color.Transparent).stroke(active?'#607B67':'#768878').strokeWidth('1.8px').strokeLineCap(LineCapStyle.Round).strokeLineJoin(LineJoinStyle.Round)}
        .width(23).height(23).viewPort({x:0,y:0,width:'24px',height:'24px'})
    }.height(27).width(46).justifyContent(FlexAlign.Center).borderRadius(23)
    .linearGradient({angle:140,colors:active?[['#FFFFFF',0],[tabTints.get(id)||'#D7EFEA',1]]:[['#00FFFFFF',0],['#00FFFFFF',1]]})
    .shadow({radius:active?1:0,offsetY:active?3:0,color:active?(tabEdges.get(id)||'#B9DCD0'):'#00000000'})
  }
  @Builder Bottom(){
    Row(){
      ForEach(tabs,(t:TabEntry)=>{
        Column({space:3}){
          this.Mark(t.id,tabFor(this.route)===t.id)
          Text(t.label).fontSize(16).lineHeight(21.6).fontColor(tabFor(this.route)===t.id?'#436653':'#768878').fontWeight(tabFor(this.route)===t.id?FontWeight.Bold:FontWeight.Regular)
        }.id('tab-'+t.id).height(60).layoutWeight(1).justifyContent(FlexAlign.Center).onClick(()=>this.navigate(t.id))
      },(t:TabEntry)=>t.id)
    }.height(79).width('100%').padding({top:8,bottom:10,left:7,right:7}).backgroundColor('#FDFBF7').border({width:{top:1},color:'#F1EFE8'})
  }
  build(){
    Stack(){
      Column(){
        Stack(){
          if(this.route==='boot'){
            Column({space:16}){Image($r('app.media.records_logo')).height(78).width(78);if(this.startupError){Text(this.startupError);Button('重试').onClick(()=>this.boot())}else{LoadingProgress().width(28)}}
          }else if(this.route==='meet'){MeetPage({onDone:():void=>{this.history=[];this.route='ai'},onOffline:():void=>{this.history=[];this.route='records'}})}
          else if(this.route.startsWith('auth/')){AuthPage({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
          else if(tabFor(this.route)==='records'){RecordsScreen({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
          else if(tabFor(this.route)==='today'){TodayPage({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
          else if(tabFor(this.route)==='community'){CommunityPage({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
          else if(tabFor(this.route)==='mine'){MinePage({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
          else if(tabFor(this.route)==='ai'){AssistantPage({route:this.route,onNavigate:(v:string):void=>this.navigate(v)})}
        }.width('100%').layoutWeight(1)
        if(tabs.some((t:TabEntry)=>t.id===this.route)||this.route.startsWith('records/day/')){this.Bottom()}
      }.width('100%').constraintSize({maxWidth:430}).height('100%')
    }.width('100%').height('100%').backgroundColor('#FDFBF7')
  }
}
