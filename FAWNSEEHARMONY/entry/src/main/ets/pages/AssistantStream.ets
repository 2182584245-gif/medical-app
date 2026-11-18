export type StreamEvent = Record<string,Object>
// Member replies use one SSE data frame per event. Guest replies use JSON lines.
export class ChatEventReader {
  private tail:string=''
  private payload:string[]=[]
  private guest:boolean
  private finished:boolean=false
  private received:number=0
  private emit:(event:StreamEvent)=>void
  result:StreamEvent={}
  constructor(guest:boolean,emit:(event:StreamEvent)=>void){this.guest=guest;this.emit=emit}
  feed(input:string):void{
    this.tail+=input
    let cut=this.tail.indexOf('\n')
    while(cut>=0){const line=this.tail.slice(0,cut).replace(/\r$/,'');this.tail=this.tail.slice(cut+1);this.line(line);cut=this.tail.indexOf('\n')}
    if(this.tail.length>8*1024*1024)throw new Error('单条回答数据过大。')
  }
  private line(line:string):void{
    if(this.guest){if(line.trim())this.frame(line);return}
    if(!line){if(this.payload.length){this.frame(this.payload.join('\n'));this.payload=[]};return}
    if(line.startsWith('data:'))this.payload.push(line.slice(5).replace(/^ /,''))
  }
  private frame(data:string):void{
    const event=JSON.parse(data) as StreamEvent
    if(!event||typeof event!=='object')throw new Error('回答格式无效。')
    if(event['error']||event['type']==='error')throw new Error('回答未能完成（'+String(event['error']||event['code']||'unknown')+'）。')
    if(this.finished)return
    if(typeof event['delta']==='string'){
      this.received+=(event['delta'] as string).length
      if(this.received>2000000)throw new Error('回答过长，请分次提问。')
    }
    if(this.guest && event['type']==='result'){this.result=event['result'] as StreamEvent;this.finished=true}
    if(!this.guest&&event['done']===true){this.result=event;this.finished=true}
    this.emit(event)
  }
  finish():StreamEvent{
    if(this.tail)this.line(this.tail.replace(/\r$/,''));this.tail=''
    if(!this.guest)this.line('')
    if(!this.finished)throw new Error('回答连接中断，请核对原回答进度。')
    return this.result
  }
}
