import { util } from '@kit.ArkTS';
import { actorId, rpc } from '../services/Cloud';

export const PAPER = '#FDFBF7';
export const INK = '#2D443A';
export const TEAL = '#286D60';
export const MUTED = '#65766D';
export const EDGE = '#DEE8E1';
export const WHITE = '#FFFEFC';
export const GREEN = '#E5F0E9';
export const DAYS: number[] = [-6,-5,-4,-3,-2,-1,0];

export interface PageResult<T> { items: T[]; total: number; }
export function expansion<T>(method: string, body: Object, operation?: string): Promise<T> {
  return rpc<T>('expansion', method, [actorId(), body], undefined, operation);
}
export function operationId(): string { return util.generateRandomUUID(true); }
export function message(e: Error): string { return e.message || '读取失败，请重试。'; }
export function pad(n: number): string { return n.toString().padStart(2,'0'); }
export function localTime(d: Date): string { return pad(d.getHours())+':'+pad(d.getMinutes()); }
export function dayAt(offset: number = 0): string {
  const bj = new Date(Date.now() + 28800000 + offset*86400000);
  return bj.toISOString().substring(0,10);
}
export function weekday(offset: number): string {
  return ['日','一','二','三','四','五','六'][new Date(dayAt(offset)+'T12:00:00+08:00').getUTCDay()];
}
export class Choice {
  key: string;
  label: string;
  constructor(key: string, label: string) { this.key=key; this.label=label; }
}
@Component
export struct TMHeader {
  @Prop title: string = '';
  @Prop parentTitle: string = '返回';
  back: () => void = () => {};
  build() {
    Row({space:8}) {
      Text('‹ '+this.parentTitle).fontSize(17).fontColor(TEAL).constraintSize({minHeight:44}).onClick(this.back)
      Text(this.title).fontSize(23).fontWeight(FontWeight.Bold).textAlign(TextAlign.Center).layoutWeight(1).fontColor(INK)
      Image($r('app.media.records_logo')).width(32).height(32).objectFit(ImageFit.Contain)
    }.height(56).width('100%').padding({left:12,right:16}).backgroundColor(PAPER)
  }
}
@Component
export struct TMBanner {
  @Prop title: string='';
  @Prop subtitle: string='';
  @Prop art: Resource=$r('app.media.records_journal');
  build() {
    Row({space:12}) {
      Column({space:6}) {
        Text(this.title).fontSize(25).fontWeight(FontWeight.Bold).fontColor(INK)
        if (this.subtitle) { Text(this.subtitle).fontSize(16).fontColor(MUTED) }
      }.alignItems(HorizontalAlign.Start).layoutWeight(1)
      Image(this.art).width(74).height(74).borderRadius(16).objectFit(ImageFit.Cover)
    }.padding(16).width('100%').backgroundColor('#F2F5EB').borderRadius(22)
  }
}
@Component
export struct TMButton {
  @Prop label:string='';
  @Prop primary:boolean=false;
  @Prop danger:boolean=false;
  @Prop blocked:boolean=false;
  action:()=>void=()=>{};
  build() {
    Button(this.label,{type:ButtonType.Normal}).width('100%').constraintSize({minHeight:46})
      .fontSize(18).fontWeight(FontWeight.Medium).fontColor(this.primary?'#FFFFFF':this.danger?'#913F36':TEAL)
      .backgroundColor(this.primary?(this.danger?'#934A41':TEAL):WHITE)
      .border({width:1,color:EDGE}).borderRadius(15).enabled(!this.blocked).onClick(this.action)
  }
}
@Component
export struct TMLabel {
  @Prop label:string='';
  build() { Text(this.label).width('100%').fontSize(21).fontWeight(FontWeight.Bold).fontColor(INK) }
}
@Component
export struct TMNote {
  @Prop label:string='';
  build() { Text(this.label).width('100%').fontSize(17).fontColor(MUTED).lineHeight(25) }
}
@Component
export struct TMPills {
  @Prop options: Choice[]=[];
  @Prop selected:string='';
  change:(key:string)=>void=()=>{};
  build() {
    Scroll() {
      Row({space:8}) {
        ForEach(this.options,(c:Choice)=>{
          Button(c.label,{type:ButtonType.Capsule}).fontSize(16).fontColor(c.key===this.selected?'#FFF':INK)
            .backgroundColor(c.key===this.selected?TEAL:GREEN).height(38).onClick(()=>this.change(c.key))
        },(c:Choice)=>c.key)
      }
    }.scrollable(ScrollDirection.Horizontal).scrollBar(BarState.Off).width('100%').align(Alignment.Start)
  }
}
@Component
export struct TMField {
  @Prop fieldId:string='';
  @Prop label:string='';
  @Prop value:string='';
  @Prop placeholder:string='';
  @Prop numeric:boolean=false;
  @Prop password:boolean=false;
  @Prop multiline:boolean=false;
  change:(v:string)=>void=()=>{};
  build() {
    Column({space:6}){
      Text(this.label).fontSize(18).fontColor(INK).fontWeight(FontWeight.Medium)
      if(this.multiline){
        TextArea({text:this.value,placeholder:this.placeholder}).fontSize(18).minLines(3).onChange(this.change)
          .backgroundColor(WHITE).border({width:1,color:EDGE}).borderRadius(13)
      } else {
        TextInput({text:this.value,placeholder:this.placeholder}).id(this.fieldId).type(this.password?InputType.Password:this.numeric?InputType.NUMBER_DECIMAL:InputType.Normal)
          .fontSize(18).height(48).onChange(this.change).backgroundColor(WHITE).border({width:1,color:EDGE}).borderRadius(13)
      }
    }.alignItems(HorizontalAlign.Start).width('100%')
  }
}
