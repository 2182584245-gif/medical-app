import { actorId,canOpenOfflineRecords,cloud,CloudNetworkError,rememberOnboarding } from '../services/Cloud'
interface MeetState {stage:string;profile:Record<string,Object>;candidates:string[]}
const rootPath='/v1/member/onboarding'
const steps=['按住这里说话','整理这一天','分析这一周','记录一顿饭','看一下作息']
const hints=['按住紫色按钮说出想聊的内容，松开后会转成文字。','帮您回顾这一天的生活记录，看看有哪些值得留意的变化。','把最近一周的生活放在一起看，了解自己的变化和节奏。','拍下一餐或说说吃了什么，让我帮您分析并整理饮食记录。','一起看看最近的睡眠与作息，找到更舒服的生活节奏。']
@Component
export struct MeetPage {
  onDone:()=>void=()=>{}
  onOffline:()=>void=()=>{}
  @State stage:string='loading'
  @State name:string=''
  @State age:number=30
  @State gender:string=''
  @State choices:string[]=[]
  @State chosen:string=''
  @State error:string=''
  @State busy:boolean=false
  @State guide:number=0
  ages:string[]=Array.from({length:120},(_:Object,i:number)=>String(i+1))
  active:boolean=true
  aboutToAppear(){this.active=true;this.next()}
  aboutToDisappear(){this.active=false}
  async next(){
    if(this.busy)return
    this.busy=true;this.error=''
    const owner=actorId()
    try {
      let state:MeetState
      if(this.stage==='loading'){state=await cloud<MeetState>(rootPath)}
      else if(this.stage==='name'){
        if(!this.name.trim())throw new Error('请先填写姓名。')
        state=await cloud<MeetState>(rootPath,'PATCH',{'stage':'name','name':this.name.trim()} as Record<string,Object>)
      }else if(this.stage==='demographics'){
        if(!this.gender)throw new Error('请选择性别。')
        state=await cloud<MeetState>(rootPath,'PATCH',{'stage':'demographics','age':this.age,'gender':this.gender} as Record<string,Object>)
      }else if(this.stage==='address' && !this.choices.length){state=await cloud<MeetState>(rootPath+'/suggest','POST',{} as Record<string,Object>)}
      else if(this.stage==='address'){
        if(!this.chosen)throw new Error('请选择本次生成的称呼。')
        state=await cloud<MeetState>(rootPath+'/complete','POST',{'nickname':this.chosen} as Record<string,string>)
      }else{
        if(this.guide<4){this.guide++;return}
        state=await cloud<MeetState>(rootPath+'/tutorial-complete','POST',{} as Record<string,Object>)
      }
      if(!this.active)return
      await rememberOnboarding(owner,state.stage==='done')
      if(!this.active||owner!==actorId())return
      this.stage=state.stage;this.choices=state.candidates||[]
      if(this.stage==='done'){this.onDone();return}
      if(this.stage==='address'&&!this.choices.length){
        const options=await cloud<MeetState>(rootPath+'/suggest','POST',{} as Record<string,Object>)
        if(this.active)this.choices=options.candidates||[]
      }
    }catch(e){
      if(this.active&&this.stage==='loading'&&e instanceof CloudNetworkError&&canOpenOfflineRecords())this.onOffline()
      else if(this.active)this.error=(e as Error).message
    }finally{this.busy=false}
  }
  @Builder Option(label:string,active:boolean,choose:()=>void){
    Button(label).backgroundColor(active?'#317C75':'#F1F0EB').fontColor(active?'#FFFFFF':'#345748').onClick(choose)
  }
  build(){
    Column({space:20}){
      Blank()
      Image($r('app.media.meet_guide')).width(84).height(84).borderRadius(44).objectFit(ImageFit.Cover)
      if(this.stage==='loading'){if(!this.error){LoadingProgress().width(38).height(38)}}
      else if(this.stage==='tutorial'){
        Text((this.guide+1)+' / 5').fontColor('#708278')
        Text(steps[this.guide]).fontSize(24).fontWeight(700)
        Text(hints[this.guide]).fontSize(18).lineHeight(28).textAlign(TextAlign.Center)
      }else{
        Text(this.stage==='name'?'您好，初次见面，您的姓名是？':this.stage==='demographics'?'您的年龄和性别是？':'您更喜欢哪个称呼呢？').fontSize(25).fontWeight(600).textAlign(TextAlign.Center)
        if(this.stage==='name'){TextInput({placeholder:'请输入您的姓名',text:this.name}).id('meet-name').maxLength(16).height(54).onChange((v:string)=>{this.name=v})}
        else if(this.stage==='demographics'){
          Row(){TextPicker({range:this.ages,selected:this.age-1}).height(124).width(115).onChange((_v:string|string[],n:number|number[])=>{this.age=(n as number)+1});Text('岁')}
          Row({space:20}){this.Option('男',this.gender==='male',()=>{this.gender='male'});this.Option('女',this.gender==='female',()=>{this.gender='female'})}
        }else{ForEach(this.choices,(s:string)=>{this.Option(s,s===this.chosen,()=>{this.chosen=s})})}
      }
      if(this.error){Text(this.error).fontSize(15).fontColor('#954B43')}
      if(this.stage!=='loading'||this.error){
        Button(this.busy?'请稍候…':this.error?'重试':this.stage==='tutorial'&&this.guide===4?'开始使用':'继续').id('meet-next').height(48).width('72%').backgroundColor('#317C75').enabled(!this.busy).onClick(()=>this.next())
      }
      Blank()
    }.width('100%').height('100%').padding(28).backgroundColor('#FDFBF7')
  }
}
