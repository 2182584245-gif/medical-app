import { JournalRecord, chinaDate, summary } from './RecordsData'
class Metric {
  label:string; cat:string; key:string; unit:string; aggregation:string; kind:string
  constructor(label:string,cat:string,key:string,unit:string,aggregation:string='sum',kind:string='') {
    this.label=label;this.cat=cat;this.key=key;this.unit=unit;this.aggregation=aggregation;this.kind=kind
  }
}
const METRICS:Metric[]=[
  new Metric('饮水量','water','amount_ml','毫升'),new Metric('饮食热量','diet','calories_kcal','千卡'),
  new Metric('运动时长','activity','duration_minutes','分钟'),new Metric('步数','activity','steps','步'),new Metric('睡眠时长','sleep','duration_hours','小时'),
  new Metric('居住感受','environment','quality_score','分','avg'),new Metric('医疗事项','medical','','次','count'),
  new Metric('体重','metric','value','kg','avg','weight'),new Metric('血糖','metric','value','mmol/L','avg','glucose'),
  new Metric('收缩压','metric','value','mmHg','avg','blood_pressure'),new Metric('舒张压','metric','diastolic','mmHg','avg','blood_pressure'),
  new Metric('血脂','metric','value','mmol/L','avg','lipids'),new Metric('身高','metric','value','cm','avg','height'),new Metric('自定义指标','metric','value','','avg','custom')]
interface Point { day:string; value:number; rows:JournalRecord[] }
function shifted(day:string,delta:number):string { return new Date(Date.parse(day+'T12:00:00Z')+delta*86400000).toISOString().slice(0,10) }
function short(n:number):string { return String(Number(n.toFixed(2))) }
@Component
export struct RecordsTrends {
  @Prop data:JournalRecord[]=[]
  onOpen:(key:string)=>void=()=>{}
  onBack:()=>void=()=>{}
  @State selected:number=0
  @State range:number=0
  @State start:string=shifted(chinaDate().slice(0,10),-6)
  @State end:string=chinaDate().slice(0,10)
  @State unit:string='mmol/L'
  @State context:string='全部'
  @State customName:string=''
  @State focusDay:string=''
  private canvas:CanvasRenderingContext2D=new CanvasRenderingContext2D(new RenderingContextSettings(true))
  private metric():Metric{return METRICS[this.selected]}
  private names():string[] {
    return Array.from(new Set(this.data.filter((r:JournalRecord)=>r.category==='metric'&&r.fields['kind']==='custom'&&!!r.fields['metric_name']).map((r:JournalRecord)=>r.fields['metric_name'])))
  }
  private rows():JournalRecord[] {
    const c=this.metric(),name=this.customName||this.names()[0]||''
    return this.data.filter((r:JournalRecord)=>{
      const f=r.fields
      return r.category===c.cat&&f['date']>=this.start&&f['date']<=this.end&&(!c.kind||c.kind===f['kind'])&&
        (c.kind!=='glucose'||(this.unit===f['unit']&&(this.context==='全部'||f['context']===this.context)))&&
        (c.kind!=='custom'||!name||name===f['metric_name'])&&(c.aggregation==='count'||!!f[c.key]&&Number.isFinite(Number(f[c.key])))
    })
  }
  private measure(rows:JournalRecord[]):number {
    const c=this.metric()
    if(c.aggregation==='count')return rows.length
    const total=rows.reduce((n:number,r:JournalRecord)=>n+Number(r.fields[c.key]),0)
    return c.aggregation==='avg'&&rows.length?total/rows.length:total
  }
  private points():Point[]{
    const groups=new Map<string,JournalRecord[]>()
    this.rows().forEach((r:JournalRecord)=>{const day=r.fields['date'];const items=groups.get(day)||[];items.push(r);groups.set(day,items)})
    return Array.from(groups.keys()).sort().map((day:string):Point=>({day:day,rows:groups.get(day)!,value:this.measure(groups.get(day)!)}))
  }
  private coordinate(day:string,width:number):number {
    const span=Math.max(86400000,Date.parse(this.end)-Date.parse(this.start))
    return 34+(width-51)*(Date.parse(day)-Date.parse(this.start))/span
  }
  private chart():void {
    const ctx=this.canvas,w=ctx.width,h=ctx.height,pts=this.points()
    if(w<=0||h<=0)return
    ctx.clearRect(0,0,w,h)
    if(!pts.length)return
    const values=pts.map((p:Point)=>p.value),hi=Math.max(...values),lo=Math.min(...values),pad=Math.max(.5,hi*.12,hi-lo)*.35
    const min=lo>=0?Math.max(0,lo-pad):lo-pad,max=hi+pad
    const y=(v:number)=>20+(max-v)/(max-min)*(h-52)
    ctx.font='12vp sans-serif';ctx.textAlign='right';ctx.fillStyle='#596D61'
    for(let i=0;i<4;i++){const v=min+(max-min)*i/3;ctx.beginPath();ctx.strokeStyle='#DCE5DE';ctx.moveTo(34,y(v));ctx.lineTo(w-17,y(v));ctx.stroke();ctx.fillText(String(Number(v.toFixed(1))),29,y(v)+3)}
    ctx.beginPath();ctx.strokeStyle='#43847B';ctx.lineWidth=2
    pts.forEach((p:Point,i:number)=>{const x=this.coordinate(p.day,w);if(i>0&&Date.parse(p.day)-Date.parse(pts[i-1].day)===86400000)ctx.lineTo(x,y(p.value));else ctx.moveTo(x,y(p.value))});ctx.stroke()
    ctx.textAlign='center';ctx.fillStyle='#336D65'
    pts.forEach((p:Point)=>{const x=this.coordinate(p.day,w);ctx.beginPath();ctx.arc(x,y(p.value),5,0,Math.PI*2);ctx.fill();ctx.fillText(short(p.value),x,Math.max(12,y(p.value)-9))})
    ctx.textAlign='left';ctx.fillText(this.start.slice(5),34,h-5);ctx.textAlign='right';ctx.fillText(this.end.slice(5),w-17,h-5)
  }
  private chooseRange(i:number):void{
    this.range=i;this.focusDay=''
    if(i<2){this.end=chinaDate().slice(0,10);this.start=shifted(this.end,i===0?-6:-29)}
  }
  private pickDate(start:boolean):void{
    this.getUIContext().showDatePickerDialog({selected:new Date((start?this.start:this.end)+'T12:00:00'),onDateAccept:(d:Date)=>{
      const value=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')
      if(start)this.start=value;else this.end=value;this.focusDay=''
    }})
  }
  private useUnit():string {return this.metric().kind==='glucose'?this.unit:this.metric().unit||this.rows()[0]?.fields['unit']||''}
  private chartKey():string{return [this.selected,this.range,this.start,this.end,this.unit,this.context,this.customName,this.points().map((p:Point)=>p.day+':'+p.value).join(',')].join('|')}
  @Builder
  Choice(label:string,options:string[],value:string,change:(value:string)=>void){
    Column({space:7}){
      Text(label).fontSize(22).lineHeight(33).fontColor('#2D443A').width('100%')
      Select(options.map((s:string):SelectOption=>({value:s}))).width('100%').height(52).selected(options.indexOf(value)).value(value).font({size:18})
        .backgroundColor('#FDFDFA').border({width:1,color:'#E6E8DF'}).borderRadius(14).onSelect((i:number,s:string)=>{change(s);this.focusDay=''})
    }.width('100%')
  }
  build() {
    Column({space:17}) {
      Stack({alignContent:Alignment.Start}) {
        Image($r('app.media.records_journal')).width('100%').height('100%').objectFit(ImageFit.Cover)
        Column().width('100%').height('100%').linearGradient({direction:GradientDirection.Right,colors:[['#F7FDFBF7',0],['#DBFDFBF7',0.34],['#00FDFBF7',0.8]]})
        Text('看见生活的\n细微轨迹').fontSize(26).lineHeight(39).fontWeight(FontWeight.Bold).fontColor('#263C34').margin({left:21})
      }.width('100%').height(154).borderRadius(24).clip(true)
      this.Choice('分析指标',METRICS.map((m:Metric)=>m.label),this.metric().label,(v:string)=>{this.selected=METRICS.findIndex((m:Metric)=>m.label===v)})
      Row({space:8}){
        ForEach(['近 7 天','近 30 天','自选日期'],(v:string,i:number)=>{
          Button(v,{type:ButtonType.Normal}).fontSize(18).fontColor(this.range===i?'#FFFFFF':'#39776B').backgroundColor(this.range===i?'#568174':'#EDF4EB').borderRadius(14).onClick(()=>this.chooseRange(i))
        })
      }.width('100%')
      if(this.range===2){Row({space:8}){
        Button('开始日期 '+this.start).layoutWeight(1).onClick(()=>this.pickDate(true));Button('结束日期 '+this.end).layoutWeight(1).onClick(()=>this.pickDate(false))
      }.width('100%')}
      if(this.metric().kind==='glucose'){this.Choice('血糖单位',['mmol/L','mg/dL'],this.unit,(v:string)=>{this.unit=v});this.Choice('测量情境',['全部','未说明','空腹','餐前','餐后','随机'],this.context,(v:string)=>{this.context=v})}
      if(this.metric().kind==='custom'&&this.names().length){this.Choice('指标名称',this.names(),this.customName||this.names()[0],(v:string)=>{this.customName=v})}
      Column({space:12}){
        Row(){Text(this.metric().label).fontSize(26).fontWeight(FontWeight.Bold).fontColor('#263C34');Blank();Text(this.useUnit()).fontColor('#39776B')}.width('100%')
        Text(this.start+' — '+this.end).fontColor('#6A746A').width('100%')
        if(this.start>this.end){Text('开始日期不能晚于结束日期。').fontColor('#9C443B')}
        else if(this.points().length){
          ForEach([this.chartKey()],(_:string)=>{
            Canvas(this.canvas).width('100%').height(190).onReady(()=>this.chart()).onClick((e:ClickEvent)=>{
              const pts=this.points(),w=this.canvas.width
              const p=pts.reduce((a:Point,b:Point)=>Math.abs(this.coordinate(a.day,w)-e.x)<Math.abs(this.coordinate(b.day,w)-e.x)?a:b)
              this.focusDay=this.focusDay===p.day?'':p.day
            })
          },(k:string)=>k)
          Row(){Text((this.metric().aggregation==='avg'?'期间平均  ':'期间累计  ')+short(this.measure(this.rows()))+' '+this.useUnit()).fontColor('#2D443A').layoutWeight(1);Text('关联记录  '+this.rows().length+' 条').fontColor('#2D443A')}.width('100%')
        }else{
          Image($r('app.media.records_journal')).width(155).height(105).objectFit(ImageFit.Cover).borderRadius(58)
          Text('这段时间还没有相关记录').fontSize(24).fontWeight(FontWeight.Bold).textAlign(TextAlign.Center).fontColor('#263C34')
          Text('切换指标或日期，或先留下一条记录。').fontSize(21).textAlign(TextAlign.Center).fontColor('#6A746A')
          Button('返回生活记录').fontColor('#FFFFFF').backgroundColor('#397C74').onClick(()=>this.onBack())
        }
      }.width('100%').backgroundColor('#FFFDF9').borderRadius(24).border({width:1,color:'#E6E8DF'}).padding(16)
      if(this.rows().length){Text(this.focusDay?this.focusDay+' 的关联记录':'关联记录').fontSize(26).fontColor('#263C34').fontWeight(FontWeight.Bold).width('100%')
        ForEach(this.rows().filter((r:JournalRecord)=>!this.focusDay||r.fields['date']===this.focusDay),(r:JournalRecord)=>{
          Column({space:6}){Text(r.fields['date']+'  '+r.fields['time']).fontColor('#6A746A').fontSize(17);Text(summary(r.category,r.fields)).fontColor('#2D443A').fontSize(21)}
            .width('100%').alignItems(HorizontalAlign.Start).padding(13).backgroundColor('#FFFDF9').borderRadius(18).onClick(()=>this.onOpen(r.key))
        },(r:JournalRecord)=>r.key+r.raw.version)}
    }.width('100%').alignItems(HorizontalAlign.Start)
  }
}

