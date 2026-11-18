import { common } from '@kit.AbilityKit'
import { actorId, CloudNetworkError } from '../services/Cloud'
import { forgetRecordMutation } from '../services/RecordsCacheStore'
import { detailSnapshot, photoSnapshot, recordsSnapshot } from './RecordsCache'
import { capturePhoto } from '../services/Camera'
import { startVoice, finishVoice, cancelVoice } from '../services/Voice'
import { RecordsTrends } from './RecordsTrends'
import { proposeVoiceRows, VoiceRecord, saveVoiceRow } from './RecordsVoice'
import { JournalRecord, Fields, Photo, Revision, FieldSpec, fieldsFor, uuid,
  metricNames, metricUnits, newFields, chinaDate, summary, shortNote, knownPhoto, uploadPhoto,
  storeRecord, validatedFields, removeRecord, readDraft, writeDraft, dropDraft } from './RecordsData'
import { RECORD_ICONS } from './RecordIcons'
import { syncDeviceReminders } from '../services/DeviceReminders'

const PAPER = '#FDFBF7'
const INK = '#2D443A'
const TITLE = '#263C34'
const MUTED = '#6A746A'
const LINE = '#E6E8DF'
const ACTION = '#246F68'

class Category {
  id: string
  label: string
  tint: string
  ink: string
  edge: string

  constructor(id: string, label: string, tint: string, ink: string, edge: string) {
    this.id = id
    this.label = label
    this.tint = tint
    this.ink = ink
    this.edge = edge
  }
}

const CATEGORIES: Category[] = [
  new Category('diet', '饮食', '#F8E1D8', '#AD6B55', '#E8CBBA'),
  new Category('water', '饮水', '#DCECF8', '#397DAD', '#C2D9EC'),
  new Category('activity', '运动', '#FFF0CA', '#9B7830', '#E5D7A8'),
  new Category('sleep', '睡眠', '#EDDEF6', '#8A59A9', '#D8C4E6'),
  new Category('environment', '居住环境', '#E5EEDB', '#6B8B55', '#CFDFBF'),
  new Category('medical', '医疗检查', '#D7EFEA', '#378B81', '#B9DCD0'),
  new Category('metric', '身体指标', '#E6EEDA', '#578266', '#CFDDC7')
]

@Component
export struct RecordsScreen {
  @Prop @Watch('routeChanged') route: string = 'records'
  onNavigate: (route: string) => void = () => {}
  @State section: string = 'home'
  @State category: string = 'diet'
  @State old: JournalRecord | undefined = undefined
  @State data: JournalRecord[] = []
  @State edit: Fields = {}
  @State photos: Photo[] = []
  @State history: Revision[] = []
  @State error: string = ''
  @State notice: string = ''
  @State cacheMessage:string = ''
  @State cachedRecord:boolean = false
  @State historyLoaded:boolean = true
  @State loading: boolean = false
  @State busy: boolean = false
  @State voiceText: string = ''
  @State speaking: boolean = false
  @State voiceRows: VoiceRecord[] = []
  @State savedVoice: number = 0
  @State pendingVoice: boolean = false
  @State preview: string = ''
  private operation: string = uuid()
  private deletion: string = uuid()
  private epoch: number = 0
  private recorder: Promise<void> | undefined = undefined
  private stopped: boolean = false
  private pageScroller: Scroller = new Scroller()
  private alive: boolean = true

  private ctx(): common.UIAbilityContext { return this.getUIContext().getHostContext() as common.UIAbilityContext }
  private local(d: string): Date { return new Date(d+'T12:00:00') }
  private day(): string {
    const d=this.selectedDate
    return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')
  }
  private label(cat: string): string { return CATEGORIES.find((c:Category)=>c.id===cat)?.label ?? '记录' }
  private asset(cat:string):Resource {
    if(cat==='diet'||cat==='water') return $r('app.media.records_meal')
    if(cat==='sleep'||cat==='activity'||cat==='environment') return $r('app.media.records_rest')
    return $r('app.media.records_journal')
  }
  private heroTitle(): string {
    if(this.section==='detail') return this.label(this.category)+'里的\n一个小片段'
    const titles:Fields={ 'diet':'好好吃饭\n也是爱自己','water':'一杯水的\n温柔时刻','activity':'每一步\n都有自己的节奏','sleep':'把休息\n还给自己','environment':'舒适的空间\n从觉察开始','medical':'发生过的事\n清楚地留存','metric':'看见身体的\n细微变化' }
    return titles[this.category]
  }
  private viewRows(): JournalRecord[] {
    return this.data.filter((r:JournalRecord)=>r.fields['date']===this.day()&&(this.selectedCategory==='all'||this.selectedCategory===r.category))
  }
  private go(route:string): void { if(!this.busy) this.onNavigate(route) }
  private valid(ticket:number,actor:number): boolean { return this.alive && ticket===this.epoch && actor===actorId() }
  private err(e: Error):string { return e.message || '暂时无法完成，请再试一次。' }
  private cacheLabel(time:number,subject:string):string {
    return '当前离线，正在查看'+new Date(time).toLocaleString()+'同步到此设备的'+subject+'。'
  }
  aboutToAppear(): void { this.alive=true; this.selectedDate=this.local(chinaDate().slice(0,10)); this.routeChanged() }
  aboutToDisappear(): void { this.alive=false; this.epoch++; this.stopCapture() }
  private stopCapture(): void {
    if(this.recorder) { this.stopped=true; this.recorder=undefined; cancelVoice().catch(()=>{}) }
    this.speaking=false
  }
  private routeChanged(): void {
    this.stopCapture()
    this.voiceText=''; this.voiceRows=[]; this.savedVoice=0; this.pendingVoice=false; this.preview=''
    this.loadRoute()
  }
  private async loadRoute():Promise<void> {
    const ticket=++this.epoch, actor=actorId(), parts=this.route.replace(/^#/,'').split('/')
    this.error=''; this.notice=''; this.cacheMessage=''; this.cachedRecord=false; this.historyLoaded=true
    this.old=undefined; this.history=[]; this.photos=[]; this.edit={}
    this.busy=false; this.loading=true; this.operation=uuid(); this.deletion=uuid()
    let screen=parts[0], param=parts[1]||''
    if(screen==='records') {
      if(parts[1]==='day'&&/^\d{4}-\d{2}-\d{2}$/.test(parts[2]||'')) this.selectedDate=this.local(parts[2])
      if(parts[1]==='new'||parts[1]==='form') { screen='form'; param=parts[2] }
      else if(['detail','edit','trends','voice'].includes(parts[1])) { screen=parts[1]; param=parts[2]||'' }
      else screen='home'
    }
    if(!['home','form','edit','detail','trends','voice'].includes(screen)) screen='home'
    this.section=screen
    try {
      if(screen==='form') {
        if(!CATEGORIES.some((c:Category)=>c.id===param)) throw Error('未找到这种记录类型。')
        this.category=param; this.edit=newFields(param)
        if(actor>0) {
          const draft=await readDraft(this.ctx(),actor,param)
          if(!this.valid(ticket,actor)) return
          if(draft) { this.edit=draft.fields; this.photos=draft.photos||(draft.ids||[]).map(knownPhoto); this.operation=draft.operation; this.notice='已恢复暂存草稿，可继续填写。'; await this.showPhotos(ticket,actor) }
        }
      } else if(screen==='detail'||screen==='edit') {
        const result=await detailSnapshot(this.ctx(),actor,param)
        if(!this.valid(ticket,actor)) return
        const rec=result.value.record
        this.category=rec.category; this.old=rec; this.edit=JSON.parse(JSON.stringify(rec.fields)) as Fields
        this.photos=rec.photos.map(knownPhoto)
        this.history=result.value.history; this.historyLoaded=result.value.historyLoaded
        if(result.cachedAt){
          this.cachedRecord=true;this.section='detail'
          this.cacheMessage=this.cacheLabel(result.cachedAt,'记录')+'联网刷新后可编辑或删除。'
        }else if(screen==='edit'){
          const draft=await readDraft(this.ctx(),actor,'edit:'+rec.key)
          if(!this.valid(ticket,actor))return
          if(draft&&draft.baseVersion===rec.raw.version){
            this.edit=draft.fields;this.photos=draft.photos||this.photos;this.operation=draft.operation
            this.notice='已恢复此记录的修改草稿，请核对后保存。'
          }else if(draft){
            this.notice='云端记录已有新版本。请对照最新内容重新填写修改。'
          }
        }
        await this.showPhotos(ticket,actor)
      } else if(screen==='home'||screen==='trends') {
        this.data=[]
        if(actor>0) {
          const items=await recordsSnapshot(this.ctx(),actor)
          if(!this.valid(ticket,actor)) return
          this.data=items.value
          if(items.cachedAt)this.cacheMessage=this.cacheLabel(items.cachedAt,'记录列表')
        }
      }
    } catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) { this.loading=false; this.pageScroller.scrollTo({xOffset:0,yOffset:0}) } }
  }
  private async showPhotos(ticket:number,actor:number):Promise<void> {
    const photos=this.photos.slice()
    await Promise.all(photos.map(async(p:Photo)=>{
      if(!p.id||p.uri) return
      try {
        const result=await photoSnapshot(this.ctx(),actor,p.id)
        if(!this.valid(ticket,actor))return
        p.uri=result.value
        if(result.cachedAt&&!this.cacheMessage)this.cacheMessage=this.cacheLabel(result.cachedAt,'原图')
      } catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    }))
    if(this.valid(ticket,actor)) this.photos=photos
  }
  private setField(k:string,v:string):void {
    const f:Fields=JSON.parse(JSON.stringify(this.edit)) as Fields
    f[k]=v; this.edit=f; this.operation=uuid()
  }
  private setMetric(kind:string):void {
    if(this.old) return
    const f:Fields=JSON.parse(JSON.stringify(this.edit)) as Fields
    f['kind']=kind; f['unit']=metricUnits[kind]; f['context']='未说明'; f['value']=''; f['diastolic']=''; f['metric_name']=''
    this.edit=f; this.operation=uuid()
  }
  private async addPhoto():Promise<void> {
    if(this.busy||this.photos.length>=4) return
    const actor=actorId(),ticket=this.epoch
    this.busy=true; this.error=''
    try {
      const selected=await capturePhoto(this.ctx())
      if(!this.valid(ticket,actor)) return
      const items=selected.slice(0,4-this.photos.length).map((uri:string):Photo=>({id:0,uri:uri,operation:uuid(),local:true}))
      this.photos=this.photos.concat(items); this.operation=uuid()
    } catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) this.busy=false }
  }
  private async save(draft:boolean):Promise<void> {
    if(this.busy||this.loading||this.cachedRecord) return
    const actor=actorId(), ticket=this.epoch, cat=this.category, old=this.old
    if(actor<=0) { this.error='请先登录后保存生活记录。'; return }
    this.busy=true; this.error=''; this.notice=''
    try {
      const f=draft?this.edit:validatedFields(cat,this.edit,this.photos.length,!!old)
      const draftKey=old?'edit:'+old.key:cat
      await writeDraft(this.ctx(),actor,draftKey,f,this.photos,this.operation,old?.raw.version)
      if(!this.valid(ticket,actor)) return
      if(draft) { this.notice='图文草稿已保存在此设备，可稍后继续填写。'; return }
      const ids:number[]=[]
      for(const p of this.photos) {
        if(!this.valid(ticket,actor)) return
        if(!p.id)p.local=true
        p.id=await uploadPhoto(p,actor); ids.push(p.id)
      }
      if(!this.valid(ticket,actor)) return
      await storeRecord(cat,f,ids,old,this.operation)
      await forgetRecordMutation(this.ctx(),actor,old?.key||'').catch(()=>{})
      if(!this.valid(ticket,actor)) return
      if(cat==='diet')syncDeviceReminders(this.ctx(),false).catch(()=>{})
      try { await dropDraft(this.ctx(),actor,draftKey) } catch { this.getUIContext().getPromptAction().showToast({message:'记录已保存，暂存草稿清理失败。'}) }
      if(this.valid(ticket,actor)) this.onNavigate('records/day/'+f['date'])
    } catch(e) { if(this.valid(ticket,actor)) this.error=e instanceof CloudNetworkError?'暂时无法确认云端保存结果。图文草稿已保留，请联网后再次保存。':this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) this.busy=false }
  }
  private async destroyRecord():Promise<void> {
    const r=this.old, ticket=this.epoch, actor=actorId()
    if(!r||this.busy||this.cachedRecord) return
    this.busy=true; this.error=''
    try {
      await removeRecord(r,this.deletion)
      await forgetRecordMutation(this.ctx(),actor,r.key).catch(()=>{})
      await dropDraft(this.ctx(),actor,'edit:'+r.key).catch(()=>{})
      if(!this.valid(ticket,actor))return
      if(r.category==='diet')syncDeviceReminders(this.ctx(),false).catch(()=>{})
      this.onNavigate('records/day/'+r.fields['date'])
    }
    catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) this.busy=false }
  }
  private confirmDelete():void {
    this.getUIContext().showAlertDialog({title:'删除这条记录？',message:'删除后将同步更新云端记录与趋势。',primaryButton:{value:'保留记录',action:()=>{}},secondaryButton:{value:'确认删除',fontColor:'#9C443B',action:()=>{this.destroyRecord()}}})
  }
  private beginCapture():void {
    if(this.busy||this.recorder||this.voiceRows.length) return
    const ticket=this.epoch
    this.stopped=false; this.error=''; this.speaking=true
    this.recorder=startVoice(this.ctx()).catch((e:Error)=>{
      if(this.epoch===ticket&&!this.stopped) { this.speaking=false; this.error=this.err(e); this.recorder=undefined }
      throw e
    })
    this.recorder.catch(()=>{})
  }
  private async endCapture(field:string=''):Promise<void> {
    const active=this.recorder, ticket=this.epoch, actor=actorId()
    if(!active||this.busy) return
    this.busy=true
    try {
      await active
      if(!this.valid(ticket,actor)||this.stopped) return
      const text=await finishVoice()
      if(!this.valid(ticket,actor)) return
      if(field) this.setField(field,[this.edit[field]||'',text].filter((s:string)=>!!s).join(' '))
      else this.voiceText=[this.voiceText,text].filter((s:string)=>!!s).join(' ')
    } catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) { this.busy=false; this.speaking=false; this.recorder=undefined } }
  }
  private async voiceAction():Promise<void> {
    if(this.busy) return
    const actor=actorId(), ticket=this.epoch
    this.busy=true; this.error=''
    try {
      if(!this.voiceRows.length) {
        const rows=await proposeVoiceRows(this.voiceText)
        if(this.valid(ticket,actor)) {this.voiceRows=rows; this.savedVoice=0; this.notice='请逐条核对类别、时间和数值，确认无误后再保存。'}
      } else {
        this.pendingVoice=true
        for(let i=this.savedVoice;i<this.voiceRows.length;i++) {
          if(!this.valid(ticket,actor)) return
          await saveVoiceRow(this.voiceRows[i],actor)
          await forgetRecordMutation(this.ctx(),actor,'').catch(()=>{})
          if(!this.valid(ticket,actor)) return
          this.savedVoice=i+1
        }
        this.getUIContext().getPromptAction().showToast({message:'已保存 '+this.savedVoice+' 条生活记录。'})
        syncDeviceReminders(this.ctx(),false).catch(()=>{})
        this.voiceRows=[]; this.pendingVoice=false; this.savedVoice=0; this.voiceText=''
        this.loadRoute()
      }
    } catch(e) { if(this.valid(ticket,actor)) this.error=this.err(e as Error) }
    finally { if(this.valid(ticket,actor)) this.busy=false }
  }
  @State selectedDate: Date = new Date()
  @State selectedCategory: string = 'all'

  private moveDate(days: number): void {
    const date = new Date(this.selectedDate.getTime())
    date.setDate(date.getDate() + days)
    this.selectedDate = date
  }

  private displayDate(): string {
    const date = this.selectedDate
    const month = (date.getMonth() + 1).toString().padStart(2, '0')
    const day = date.getDate().toString().padStart(2, '0')
    return month + '/' + day + '/' + date.getFullYear().toString()
  }

  private selectedLabel(): string {
    return CATEGORIES.find((item: Category) => item.id === this.selectedCategory)?.label ?? '饮水'
  }

  @Builder
  Icon(name: string, color: string, side: number) {
    Shape() {
      Path({ commands: RECORD_ICONS.get(name) ?? '' })
        .fill(Color.Transparent)
        .stroke(color)
        .strokeWidth('1.8px')
        .strokeLineCap(LineCapStyle.Round)
        .strokeLineJoin(LineJoinStyle.Round)
    }
    .width(side)
    .height(side)
    .viewPort({ x: 0, y: 0, width: '24px', height: '24px' })
    .accessibilityLevel('no')
  }

  @Builder
  Header() {
    Row({ space: 8 }) {
      Image($r('app.media.records_logo'))
        .width(40)
        .height(40)
        .objectFit(ImageFit.Contain)
        .accessibilityLevel('no')
      Text(this.section==='home'?'生活记录 · 凡思日常':'凡思生活')
        .fontSize(18)
        .fontColor(TITLE)
        .fontWeight(FontWeight.Bold)
    }
    .width('100%')
    .height(this.section==='home'?68:63)
    .padding({ left: 16, right: 16 })
    .backgroundColor(PAPER)
    .border({ width: { bottom: 1 }, color: '#F3F1E8' })
  }

  @Builder
  Banner() {
    Stack({ alignContent: Alignment.BottomStart }) {
      Image($r('app.media.records_journal'))
        .width('100%')
        .height('100%')
        .objectFit(ImageFit.Cover)
        .accessibilityLevel('no')
      Column()
        .width('100%')
        .height('100%')
        .linearGradient({ direction: GradientDirection.Bottom,
          colors: [['#00FDFBF7', 0], ['#5CFDFBF7', 0.28], ['#F5FDFBF7', 1]] })
      Text('一日一页\n生活有迹可循')
        .fontSize(26)
        .lineHeight(32.5)
        .fontWeight(FontWeight.Bold)
        .fontColor(TITLE)
        .margin({ left: 17, bottom: 17 })
    }
    .width('100%')
    .height(156)
    .borderRadius(25)
    .border({ width: 1, color: '#FFFFFF' })
    .clip(true)
    .shadow({ radius: 1, offsetY: 3, color: '#DCE9E1' })
  }

  @Builder
  VoiceEntry() {
    Row({ space: 8 }) {
      Row() {
        this.Icon('mic', '#FFFFFF', 24)
      }
      .width(38)
      .height(44)
      .justifyContent(FlexAlign.Center)
      Column({ space: 3 }) {
        Text('长按进行语音智能记录')
          .fontSize(18)
          .lineHeight(27)
          .fontWeight(900)
          .fontColor('#FFFFFF')
        Text('说一段日常，整理成生活记录')
          .fontSize(16)
          .lineHeight(20.8)
          .fontColor('#FFFFFF')
      }
      .alignItems(HorizontalAlign.Start)
      .layoutWeight(1)
    }
    .width('100%')
    .height(76)
    .padding({ left: 13, right: 13 })
    .linearGradient({ angle: 140, colors: [['#A17DBF', 0], ['#8356A7', 1]] })
    .borderRadius(24)
    .border({ width: 1, color: '#BFA6D8' })
    .shadow({ radius: 1, offsetY: 5, color: '#69418D' })
    .gesture(LongPressGesture({duration:300}).onAction(()=>this.beginCapture()).onActionEnd(()=>{this.endCapture()}).onActionCancel(()=>this.stopCapture()))
  }

  @Builder
  CategoryTile(category: Category) {
    Column({ space: 7 }) {
      Row() {
        this.Icon(category.id, category.ink, 23)
      }
      .width(51)
      .height(51)
      .justifyContent(FlexAlign.Center)
      .linearGradient({ angle: 140, colors: [['#FFFFFF', 0], [category.tint, 1]] })
      .border({ width: 1, color: '#FFFFFF' })
      .borderRadius(14)
      .shadow({ radius: 2, offsetY: 2, color: category.edge })
      Text(category.label)
        .fontSize(22)
        .lineHeight(28.6)
        .fontWeight(FontWeight.Regular)
        .fontColor(INK)
        .textAlign(TextAlign.Center)
    }
    .height(98.6)
    .padding({ top: 6, bottom: 6 })
    .layoutWeight(1)
    .id('record-category-'+category.id)
    .onClick(()=>this.go('form/'+category.id))
  }

  @Builder
  CategoryGrid() {
    Column({ space: 14 }) {
      Row({ space: 8 }) {
        this.CategoryTile(CATEGORIES[0])
        this.CategoryTile(CATEGORIES[1])
        this.CategoryTile(CATEGORIES[2])
        this.CategoryTile(CATEGORIES[3])
      }
      .width('100%')
      Row({ space: 8 }) {
        this.CategoryTile(CATEGORIES[4])
        this.CategoryTile(CATEGORIES[5])
        this.CategoryTile(CATEGORIES[6])
        Blank().layoutWeight(1)
      }
      .width('100%')
    }
    .width('100%')
  }

  @Builder
  DateArrow(icon: string, description: string, days: number) {
    Button({ type: ButtonType.Normal }) {
      this.Icon(icon, '#378B81', 22)
    }
    .width(44)
    .height(44)
    .linearGradient({ angle: 140, colors: [['#FFFFFF', 0], ['#D7EFEA', 1]] })
    .border({ width: 1, color: '#FFFFFF' })
    .shadow({ radius: 1, offsetY: 2, color: '#B9DCD0' })
    .borderRadius(15)
    .accessibilityText(description)
    .onClick(() => this.moveDate(days))
  }

  @Builder
  Filter(id: string, label: string, tint: string, ink: string, edge: string) {
    Button({ type: ButtonType.Normal }) {
      Text(label.split('').join('\n'))
        .fontSize(18)
        .lineHeight(27)
        .fontWeight(FontWeight.Bold)
        .fontColor(this.selectedCategory === id ? '#FFFFFF' : ink)
        .textAlign(TextAlign.Center)
    }
    .width(48)
    .height(128)
    .linearGradient({ angle: 140, colors: this.selectedCategory === id ?
      [['#659385', 0], ['#4F796B', 1]] : [['#FFFFFF', 0], [tint, 1]] })
    .border({ width: 1, color: this.selectedCategory === id ? '#ABCCC0' : '#FFFFFF' })
    .shadow({ radius: 1, offsetY: 3, color: this.selectedCategory === id ? '#365E50' : edge })
    .borderRadius(14)
    .accessibilityText(label)
    .onClick(() => { this.selectedCategory = id })
  }

  @Builder
  RecordList() {
    Column() {
      Row() {
        Text('今天的生活片段')
          .fontSize(26)
          .fontWeight(FontWeight.Bold)
          .fontColor(TITLE)
        Row({ space: 5 }) {
          this.Icon('trend', ACTION, 19)
          Text('趋势分析')
            .fontSize(18)
            .fontWeight(FontWeight.Medium)
            .fontColor(ACTION)
        }.onClick(()=>this.go('trends'))
      }
      .width('100%')
      .height(44)
      .justifyContent(FlexAlign.SpaceBetween)

      Row({ space: 8 }) {
        this.DateArrow('back', '前一天', -1)
        Row() {
          Text(this.displayDate())
            .fontSize(18)
            .fontColor(INK)
          this.Icon('calendar', '#000000', 20)
        }
        .height(53)
        .layoutWeight(1)
        .justifyContent(FlexAlign.SpaceBetween)
        .padding({ left: 14, right: 12 })
        .backgroundColor('#FDFDFA')
        .border({ width: 1, color: LINE })
        .borderRadius(14)
        .accessibilityText('记录日期 ' + this.displayDate())
        .onClick(() => {
          this.getUIContext().showDatePickerDialog({
            selected: this.selectedDate,
            onDateAccept: (value: Date) => { this.selectedDate = value }
          })
        })
        this.DateArrow('chevron', '后一天', 1)
      }
      .width('100%')
      .margin({ top: 12, bottom: 10 })

      Scroll() {
        Row({ space: 8 }) {
          this.Filter('all', '全部', '#E6EEDA', '#578266', '#CFDDC7')
          ForEach(CATEGORIES, (category: Category) => {
            this.Filter(category.id, category.label, category.tint, category.ink, category.edge)
          }, (category: Category) => category.id)
        }
        .padding({ top: 3, bottom: 8 })
      }
      .scrollable(ScrollDirection.Horizontal)
      .scrollBar(BarState.Off)
      .width('100%')

      Text(this.viewRows().length+' 条记录 · 按发生时间排列')
        .fontSize(22)
        .lineHeight(33)
        .fontColor(MUTED)

      if(this.loading) { LoadingProgress().width(32).height(32) } else if(this.viewRows().length) {
        ForEach(this.viewRows(),(r:JournalRecord)=>{this.RecordRow(r)},(r:JournalRecord)=>r.key+r.raw.version)
      } else if(!this.error) {
      Column() {
        Image($r('app.media.records_journal'))
          .width(180)
          .height(120)
          .objectFit(ImageFit.Cover)
          .borderRadius(60)
          .accessibilityLevel('no')
        Text(this.selectedCategory === 'all' ? '这里还没有生活记录' : '这一天还没有' + this.selectedLabel() + '记录')
          .fontSize(26)
          .lineHeight(39)
          .fontWeight(FontWeight.Bold)
          .fontColor(TITLE)
          .margin({ top: 19 })
        Text('从一杯水、一顿饭开始，给今天留下第一笔。')
          .fontSize(22)
          .lineHeight(33)
          .fontColor(MUTED)
          .textAlign(TextAlign.Center)
        Row({ space: 6 }) {
          this.Icon(this.selectedCategory === 'all' ? 'water' : this.selectedCategory, '#FFFFFF', 20)
          Text('记录' + this.selectedLabel())
            .fontSize(18)
            .fontWeight(FontWeight.Medium)
            .fontColor('#FFFFFF')
        }
        .width(136)
        .height(49)
        .justifyContent(FlexAlign.Center)
        .margin({ top: 18 })
        .linearGradient({ angle: 140, colors: [['#4B9690', 0], ['#317C75', 1]] })
        .border({ width: 1, color: '#A9D2C9' })
        .shadow({ radius: 1, offsetY: 4, color: '#245E58' })
        .borderRadius(16)
        .onClick(()=>this.go('form/'+(this.selectedCategory==='all'?'water':this.selectedCategory)))
      }
      .width('100%')
      .padding({ top: 20, bottom: 28, left: 15, right: 15 })
      }
    }
    .alignItems(HorizontalAlign.Start)
    .width('100%')
  }

  @Builder
  Backline(title:string) {
    Row({space:10}) {
      Button({type:ButtonType.Normal}) {this.Icon('back','#378B81',23)}
        .width(44).height(44).borderRadius(15).linearGradient({angle:140,colors:[['#FFFFFF',0],['#D7EFEA',1]]})
        .shadow({radius:1,offsetY:2,color:'#B9DCD0'}).accessibilityText('返回').onClick(()=>this.go(this.section==='edit'&&this.old?'detail/'+this.old.key:'records'))
      Text(title).fontSize(22).fontWeight(FontWeight.Bold).fontColor(TITLE)
    }.width('100%').height(44).margin({bottom:12})
  }
  @Builder
  SmallHero() {
    Stack({alignContent:Alignment.Start}) {
      Image(this.asset(this.category)).width('100%').height('100%').objectFit(ImageFit.Cover)
      Column().width('100%').height('100%').linearGradient({direction:GradientDirection.Right,colors:[['#F7FDFBF7',0],['#DBFDFBF7',0.34],['#00FDFBF7',0.8]]})
      Column({space:6}) {
        Text(this.heroTitle()).fontSize(26).lineHeight(39).fontColor(TITLE).fontWeight(FontWeight.Bold)
        Text(this.section==='detail'?(this.edit['date']+' · '+this.edit['time']):'留一份真实的日常。').fontSize(18).lineHeight(25).fontColor(MUTED)
      }.alignItems(HorizontalAlign.Start).padding({left:21})
    }.width('100%').height(154).borderRadius(24).clip(true).border({width:1,color:'#FFFFFF'}).shadow({radius:1,offsetY:3,color:'#DCE9E1'}).margin({bottom:18})
  }
  @Builder
  RecordRow(r:JournalRecord) {
    Row({space:12}) {
      Row(){this.Icon(r.category,CATEGORIES.find((c:Category)=>c.id===r.category)?.ink||ACTION,23)}
        .width(46).height(46).justifyContent(FlexAlign.Center).borderRadius(14)
        .linearGradient({angle:140,colors:[['#FFFFFF',0],[CATEGORIES.find((c:Category)=>c.id===r.category)?.tint||'#D7EFEA',1]]})
      Column({space:4}) {
        Row() { Text(this.label(r.category)).fontSize(18).fontWeight(FontWeight.Bold).fontColor(ACTION); Blank(); Text(r.fields['time']).fontSize(18).fontColor(MUTED) }.width('100%')
        Text(summary(r.category,r.fields)).fontSize(22).lineHeight(32).fontColor(INK)
        if(shortNote(r)){Text(shortNote(r)).fontSize(18).fontColor(MUTED)}
      }.alignItems(HorizontalAlign.Start).layoutWeight(1)
      this.Icon('chevron',MUTED,19)
    }.padding({left:12,right:12,top:14,bottom:14}).width('100%').borderRadius(24).backgroundColor('#FFFDF9').border({width:1,color:LINE}).margin({top:10})
    .id('record-row-'+r.key)
    .onClick(()=>this.go('detail/'+r.key))
  }
  @Builder
  FieldLabel(title:string,required:boolean=false) {
    Text() { Span(title).fontColor(INK); if(required){Span(' *').fontColor('#A7463D')} }.fontSize(22).lineHeight(33).fontWeight(FontWeight.Medium).width('100%')
  }
  @Builder
  Action(label:string,primary:boolean,act:()=>void) {
    Button(label,{type:ButtonType.Normal}).width('100%').constraintSize({minHeight:49}).fontSize(18).fontWeight(FontWeight.Medium)
      .fontColor(primary?'#FFFFFF':ACTION).borderRadius(16).border({width:1,color:primary?'#A9D2C9':'#D8E3D9'})
      .linearGradient({angle:140,colors:primary?[['#4B9690',0],['#317C75',1]]:[['#FFFFFF',0],['#E5F0E5',1]]})
      .shadow({radius:1,offsetY:3,color:primary?'#245E58':'#C6D7CA'}).enabled(!this.busy&&!this.speaking).onClick(act)
  }
  @Builder
  Chip(text:string,active:boolean,act:()=>void) {
    Button(text,{type:ButtonType.Normal}).fontSize(17).fontColor(active?'#FFFFFF':ACTION).borderRadius(14).constraintSize({minHeight:44})
      .backgroundColor(active?'#568174':'#EDF4EB').onClick(act)
  }
  @Builder
  DatePart(time:boolean) {
    Column({space:7}) {
      this.FieldLabel(time?'发生时间':'记录日期',true)
      Button({type:ButtonType.Normal}) {
        Row() { Text(this.edit[time?'time':'date']||'请选择').fontSize(18).fontColor(INK); Blank(); this.Icon(time?'clock':'calendar',INK,18) }.width('100%')
      }.width('100%').height(53).padding({left:12,right:10}).backgroundColor('#FDFDFA').borderRadius(14).border({width:1,color:LINE})
      .onClick(()=>{
        const d=new Date(this.edit['date']+'T'+this.edit['time']+':00')
        if(time) this.getUIContext().showTimePickerDialog({selected:d,useMilitaryTime:true,onAccept:(v:TimePickerResult)=>{
          this.setField('time',String(v.hour??0).padStart(2,'0')+':'+String(v.minute??0).padStart(2,'0'))
        }})
        else this.getUIContext().showDatePickerDialog({selected:d,onDateAccept:(v:Date)=>{
          this.setField('date',v.getFullYear()+'-'+String(v.getMonth()+1).padStart(2,'0')+'-'+String(v.getDate()).padStart(2,'0'))
        }})
      })
    }.alignItems(HorizontalAlign.Start).layoutWeight(1)
  }
  private options(field:FieldSpec):string[] {
    return field.type==='meal'?(this.edit['meal']==='未说明'?['未说明','早餐','午餐','晚餐','加餐']:['早餐','午餐','晚餐','加餐']):['未说明','空腹','餐前','餐后','随机']
  }
  @Builder
  Gallery(removable:boolean) {
    if(this.photos.length) {
      Flex({wrap:FlexWrap.Wrap}) {
        ForEach(this.photos,(p:Photo,index:number)=>{
          Stack({alignContent:Alignment.TopEnd}) {
            if(p.uri){Image(p.uri).id('record-photo-'+p.id).width(130).height(102).objectFit(ImageFit.Contain).borderRadius(13).onClick(()=>{this.preview=p.uri})}
            else {Button('重读照片').onClick(()=>{this.showPhotos(this.epoch,actorId())})}
            if(removable){Button('×').width(29).height(29).fontSize(18).padding(0).backgroundColor('#98443E').onClick(()=>{
              this.photos=this.photos.filter((_:Photo,i:number)=>i!==index); this.operation=uuid()
            })}
          }.margin(5)
        },(p:Photo)=>p.operation+p.uri.length)
      }.width('100%')
    }
  }
  @Builder
  Field(field:FieldSpec) {
    Column({space:7}) {
      this.FieldLabel(field.label,field.required)
      if(field.type==='meal'||field.type==='context') {
        Select(this.options(field).map((s:string):SelectOption=>({value:s})))
          .value(this.edit[field.key]||this.options(field)[0]).selected(this.options(field).indexOf(this.edit[field.key]))
          .width('100%').height(52).font({size:18}).fontColor(INK).backgroundColor('#FDFDFA')
          .border({width:1,color:LINE}).borderRadius(14).onSelect((_:number,v:string)=>{this.setField(field.key,v)})
      } else if(field.type==='area'||field.type==='photo') {
        TextArea({text:this.edit[field.key]||'',placeholder:field.hint}).id('record-input-'+field.key).width('100%').height(field.type==='area'?103:91)
          .backgroundColor('#FFFDF9').fontSize(18).fontColor(INK).border({width:1,color:LINE}).borderRadius(14)
          .onChange((v:string)=>this.setField(field.key,v))
        if(field.type==='photo') {
          this.Gallery(true)
          this.Action('拍照输入',true,()=>{this.addPhoto()})
          Row({space:8}) { this.Icon('mic','#FFFFFF',22); Text(this.speaking?'录音中…':'语音输入').fontSize(18).fontColor('#FFFFFF'); Blank(); Text('按住 说话').fontSize(16).fontColor('#FFFFFF') }
          .width('100%').height(48).padding(12).borderRadius(16).backgroundColor('#9371AD')
          .gesture(LongPressGesture({duration:300}).onAction(()=>this.beginCapture()).onActionEnd(()=>{this.endCapture(field.key)}).onActionCancel(()=>this.stopCapture()))
        }
      } else {
        TextInput({text:this.edit[field.key]||'',placeholder:field.hint}).id('record-input-'+field.key).width('100%').height(51)
          .type(field.type==='number'&&field.min>=0?(field.integer?InputType.Number:InputType.NUMBER_DECIMAL):InputType.Normal)
          .backgroundColor('#FDFDFA').fontSize(18).fontColor(INK).border({width:1,color:LINE}).borderRadius(14)
          .onChange((v:string)=>this.setField(field.key,v))
      }
      if(field.key==='amount_ml') {
        Row({space:4}) { ForEach([100,200,250,500],(n:number)=>{
          Button(n+' 毫升',{type:ButtonType.Normal}).layoutWeight(1).fontSize(16).padding(3).height(55).fontColor(ACTION).backgroundColor('#EDF4EB').borderRadius(14).onClick(()=>this.setField('amount_ml',String(n)))
        }) }.width('100%').margin({top:8})
      }
    }.width('100%').alignItems(HorizontalAlign.Start)
  }
  private mainFields():FieldSpec[] {
    return fieldsFor(this.category,this.edit['kind']||'weight').filter((f:FieldSpec)=>!['duration_minutes','steps','temperature_c','humidity_percent'].includes(f.key))
  }
  @Builder
  PairedFields() {
    Row({space:12}) {
      ForEach(fieldsFor(this.category,'').filter((f:FieldSpec)=>['duration_minutes','steps','temperature_c','humidity_percent'].includes(f.key)),(f:FieldSpec)=>{
        Column(){this.Field(f)}.layoutWeight(1)
      },(f:FieldSpec)=>f.key)
    }.width('100%')
  }
  @Builder
  FormPage() {
    this.Backline((this.section==='edit'?'编辑':'记录')+this.label(this.category))
    this.SmallHero()
    if(this.loading) {LoadingProgress().width(32).height(32)}
    else if(this.section!=='edit'||this.old) {
      Column({space:17}) {
        Row({space:12}) {this.DatePart(false); this.DatePart(true)}.width('100%')
        if(this.category==='metric') {
          Flex({wrap:FlexWrap.Wrap}) {
            ForEach(Object.keys(metricNames),(kind:string)=>{
              Column(){this.Chip(metricNames[kind],this.edit['kind']===kind,()=>this.setMetric(kind))}.margin(3)
            },(kind:string)=>kind)
          }
        }
        if(this.category==='environment'){this.PairedFields()}
        ForEach(this.mainFields(),(f:FieldSpec)=>{
          this.Field(f)
          if(this.category==='activity'&&f.key==='activity'){this.PairedFields()}
        },(f:FieldSpec)=>this.category+'-'+(this.edit['kind']||'')+'-'+f.key)
        if(this.section==='edit'){this.Field(new FieldSpec('reason','修改原因','area','',true))}
        this.Action(this.section==='edit'?'保存修改':'保存记录',true,()=>{this.save(false)})
        this.Action('暂时保存',false,()=>{this.save(true)})
        this.Action(this.section==='edit'?'取消修改':'取消填写',false,()=>this.go(this.old?'detail/'+this.old.key:'records'))
      }.width('100%').alignItems(HorizontalAlign.Start).enabled(!this.busy)
    }
  }
  @Builder
  Pair(label:string,value:string) {
    Row({space:12}) {
      Text(label).fontSize(18).fontColor(MUTED).layoutWeight(1)
      Text(value||'未填写').fontSize(20).fontColor(INK).textAlign(TextAlign.End).layoutWeight(1.5)
    }.width('100%').padding({top:10,bottom:10}).border({width:{bottom:1},color:LINE})
  }
  private source():string {
    const s=this.old?.raw.source
    return this.category==='metric'?'身体指标记录':s==='manual'?'手动记录':s==='voice'?'语音记录':s==='ai_confirmed'?'AI 智能记录':'生活记录'
  }
  private detailLabel(f:FieldSpec):string {
    const labels:Fields={'notes':'备注','food':'食物','calories_kcal':'热量','amount_ml':'饮水量','duration_minutes':'时长','duration_hours':'睡眠时长','temperature_c':'室温','humidity_percent':'湿度','value':'数值','diastolic':'舒张压'}
    if(f.key==='description'&&this.category==='medical') return '事实说明'
    return labels[f.key]||f.label
  }
  private detailValue(f:FieldSpec):string {
    const value=this.edit[f.key]
    if(!value) return '未填写'
    const units:Fields={'calories_kcal':'千卡','amount_ml':'毫升','duration_minutes':'分钟','duration_hours':'小时','steps':'步','temperature_c':'℃','humidity_percent':'%','value':this.edit['unit']||'','diastolic':this.edit['unit']||''}
    return value+(units[f.key]?' '+units[f.key]:'')+(f.key==='calories_kcal'&&this.old?.raw.details?.['estimated']===true?' · AI估算':'')
  }
  @Builder
  DetailPage() {
    this.Backline('记录详情')
    if(this.loading){LoadingProgress().width(32).height(32)}
    else if(this.old){
      this.SmallHero()
      Column({space:7}) {
        Row() { Text(this.label(this.category)).fontSize(26).fontColor(INK).fontWeight(FontWeight.Bold); Blank(); if(this.historyLoaded){Text('v'+(this.history.length+1)).fontColor(ACTION)} }.width('100%')
        Text(this.source()).fontColor(MUTED).fontSize(18)
        this.Pair('发生时间',this.edit['date']+' '+this.edit['time'])
        if(this.category==='metric'){this.Pair('指标',metricNames[this.edit['kind']]||'指标')}
        ForEach(fieldsFor(this.category,this.edit['kind']).filter((f:FieldSpec)=>f.key!=='notes'),(f:FieldSpec)=>{this.Pair(this.detailLabel(f),this.detailValue(f))},(f:FieldSpec)=>f.key)
        if(this.category==='diet'){this.Pair('含糖量',this.edit['sugar_g']?this.edit['sugar_g']+' 克':'未填写')}
        if(this.category==='environment'){this.Pair('通风',this.edit['ventilation_minutes']?this.edit['ventilation_minutes']+' 分钟':'未填写');this.Pair('居住感受',this.edit['quality_score']?this.edit['quality_score']+' 分':'未填写')}
        if(this.category!=='medical'){this.Pair('备注',this.edit['notes']||'未填写')}
        this.Gallery(false)
      }.width('100%').padding(16).alignItems(HorizontalAlign.Start).backgroundColor('#FFFDF9').border({width:1,color:LINE}).borderRadius(22)
      if(!this.cachedRecord){Row({space:10}) {
        Column(){this.Action('编辑记录',true,()=>this.go('edit/'+this.old!.key))}.layoutWeight(1)
        Column(){this.Action('删除记录',false,()=>this.confirmDelete())}.layoutWeight(1)
      }.width('100%').margin({top:17,bottom:20})}
      Text('修改历史').fontSize(26).fontWeight(FontWeight.Bold).fontColor(TITLE).width('100%').margin({bottom:12})
      Column({space:11}){
        if(!this.historyLoaded){Text('修改历史尚未同步到此设备。').fontColor(MUTED).fontSize(18)}
        else if(!this.history.length){Text('首次记录，尚无修改。').fontColor(MUTED).fontSize(18)}
        ForEach(this.history,(h:Revision,index:number)=>{
          Text('v'+(this.history.length+1-index)+' · '+h.reason).fontSize(20).fontColor(INK).fontWeight(FontWeight.Bold)
          Text(chinaDate(h.created_at).replace('T',' ')).fontSize(16).fontColor(MUTED)
          Text('修改前：'+(h.before?.content||'')).fontColor(MUTED).fontSize(18)
          Text('修改后：'+(h.after?.content||'')).fontColor(INK).fontSize(18)
        },(h:Revision)=>h.created_at)
        if(this.historyLoaded){
          Text('v1 · 创建记录').fontSize(20).fontWeight(FontWeight.Bold).fontColor(INK)
          Text(this.edit['date']+' '+this.edit['time']).fontColor(MUTED)
        }
      }.alignItems(HorizontalAlign.Start).width('100%').padding(16).backgroundColor('#FFFDF9').border({width:1,color:LINE}).borderRadius(22)
    }
  }
  @Builder
  VoicePanel() {
    Column({space:12}) {
      this.FieldLabel('请核对或修改识别文字')
      TextArea({text:this.voiceText,placeholder:'识别结果会显示在这里'}).height(98).fontSize(18).backgroundColor('#FFFDF9')
        .enabled(!this.busy&&!this.speaking&&!this.voiceRows.length).onChange((v:string)=>{this.voiceText=v})
      ForEach(this.voiceRows,(r:VoiceRecord)=>{
        this.Pair(this.label(r.category)+' · '+chinaDate(r.stamp).replace('T',' '),r.content+'\n'+Object.keys(r.details).map((k:string)=>String(r.details[k])).join(' · '))
      },(r:VoiceRecord)=>r.op)
      this.Action(this.voiceRows.length?'确认保存以上记录':'确认文字并整理',true,()=>{this.voiceAction()})
      if(this.voiceRows.length&&!this.pendingVoice) {this.Action('返回修改文字',false,()=>{this.voiceRows=[];this.notice=''})}
      if(this.speaking){Button('取消本次语音').onClick(()=>this.stopCapture())}
    }.alignItems(HorizontalAlign.Start).width('100%').padding(14).backgroundColor('#FFFDF9').borderRadius(20).margin({top:12})
  }
  build() {
    Stack({ alignContent: Alignment.Center }) {
      Column() {
        this.Header()
        Stack({ alignContent: Alignment.Top }) {
          Image($r('app.media.records_petal'))
            .width('100%')
            .aspectRatio(288 / 512)
            .objectFit(ImageFit.Fill)
            .opacity(0.16)
            .accessibilityLevel('no')
          Scroll(this.pageScroller) {
            Column() {
              if(this.cacheMessage){
                Row({space:8}){
                  Text(this.cacheMessage).fontSize(15).fontColor(MUTED).layoutWeight(1)
                  if(this.section!=='form'&&this.section!=='edit'){Button('刷新').onClick(()=>this.loadRoute())}
                }.width('100%').padding(10)
              }
              if(this.section==='home') {
              this.Banner()
              Blank().height(19)
              this.VoiceEntry()
              if(this.speaking||this.voiceText||this.voiceRows.length) {this.VoicePanel()}
              Blank().height(17)
              this.CategoryGrid()
              Blank().height(24)
              this.RecordList()
              } else if(this.section==='form'||this.section==='edit') { this.FormPage() }
              else if(this.section==='detail') { this.DetailPage() }
              else if(this.section==='trends') { this.Backline('趋势分析'); RecordsTrends({data:this.data,onOpen:(key:string)=>this.go('detail/'+key),onBack:()=>this.go('records')}) }
              else if(this.section==='voice') { this.Backline('语音智能记录'); this.VoiceEntry(); this.VoicePanel() }
              if(this.error) { Text(this.error).fontSize(16).fontColor('#9C443B').padding(12); if(!this.busy) {if(this.section==='home'||this.section==='detail'||this.section==='trends') { Button('重新读取').onClick(()=>this.loadRoute()) } } }
              if(this.notice) { Text(this.notice).fontColor(ACTION).padding(12) }
            }
            .width('100%')
            .padding({ left: 20, right: 20, top: this.section==='home'?21:8, bottom: 31 })
          }
          .width('100%')
          .height('100%')
          .scrollBar(BarState.Off)
          .align(Alignment.Top)
        }
        .layoutWeight(1)
        .width('100%')
        .clip(true)
      }
      .width('100%')
      .constraintSize({ maxWidth: 430 })
      .height('100%')
      if(this.preview) {
        Stack({alignContent:Alignment.TopEnd}) {
          Image(this.preview).width('100%').height('100%').objectFit(ImageFit.Contain)
          Button('关闭').margin(16).onClick(()=>{this.preview=''})
        }.width('100%').height('100%').backgroundColor('#EE15201E').onClick(()=>{this.preview=''})
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(PAPER)
  }
}



