import { common } from '@kit.AbilityKit'
import { AnswerBlock,AnswerRun,answerBlocks } from './AssistantAnswerFormat'

const ink='#28473E',teal='#287C77'
@Component
export struct AssistantAnswer {
  @Prop @Watch('updateText') source:string=''
  @Prop live:boolean=false
  @State blocks:AnswerBlock[]=[]
  aboutToAppear():void{this.updateText()}
  private updateText():void{this.blocks=answerBlocks(this.source,this.live)}
  private async openLink(target:string):Promise<void>{
    try{await (this.getUIContext().getHostContext() as common.UIAbilityContext).openLink(target)}
    catch(_){this.getUIContext().getPromptAction().showToast({message:'此设备暂时无法打开链接。'})}
  }
  @Builder Body(block:AnswerBlock){
    Text(){
      ForEach(block.runs,(r:AnswerRun)=>{
        if(r.kind==='link'){Span(r.text).fontColor(teal).decoration({type:TextDecorationType.Underline,color:teal}).accessibilityText('链接，'+r.text).onClick(()=>this.openLink(r.target))}
        else if(r.kind==='strong'){Span(r.text).fontWeight(FontWeight.Bold)}
        else if(r.kind==='code'){Span(r.text).fontFamily('monospace').fontColor('#48594D').textBackgroundStyle({color:'#EAF0E7',radius:4})}
        else{Span(r.text)}
      },(r:AnswerRun,i:number)=>String(i)+r.kind+r.text+r.target)
    }.width('100%').fontSize(21).lineHeight(31).fontColor(ink).fontWeight(block.kind.startsWith('h')?FontWeight.Bold:FontWeight.Normal)
    .copyOption(CopyOptions.LocalDevice).textAlign(TextAlign.Start)
  }
  build(){
    Column({space:6}){
      ForEach(this.blocks,(block:AnswerBlock)=>{
        if(block.kind==='li'){
          Row({space:6}){Text(block.marker).fontSize(21).lineHeight(31).fontColor(ink).constraintSize({minWidth:18});Column(){this.Body(block)}.layoutWeight(1).alignItems(HorizontalAlign.Start)}
          .width('100%').alignItems(VerticalAlign.Top).padding({left:4})
        }else if(block.kind==='pre'){
          Text(block.runs[0].text).fontSize(18).lineHeight(26).fontFamily('monospace').fontColor(ink).width('100%').copyOption(CopyOptions.LocalDevice)
          .padding(12).backgroundColor('#EFF3EF').borderRadius(9)
        }else if(block.kind==='quote'){
          Column(){this.Body(block)}.width('100%').padding({left:12,right:4}).border({width:{left:3},color:'#A6CABE'})
        }else{this.Body(block)}
      },(b:AnswerBlock,i:number)=>String(i)+'|'+b.key())
    }.width('100%').alignItems(HorizontalAlign.Start)
  }
}
