import { url } from '@kit.ArkTS'

export class AnswerRun {
  kind:string
  text:string
  target:string
  constructor(kind:string,text:string,target:string=''){this.kind=kind;this.text=text;this.target=target}
}
export class AnswerBlock {
  kind:string
  runs:AnswerRun[]
  marker:string
  constructor(kind:string,runs:AnswerRun[],marker:string=''){this.kind=kind;this.runs=runs;this.marker=marker}
  key():string{return this.kind+'|'+this.marker+'|'+this.runs.map((r:AnswerRun)=>r.kind+r.text+r.target).join('\n')}
}
function safeTarget(value:string):string{
  // Only URLs with a web origin can be opened. Other markup stays readable text.
  if(!/^https?:\/\/[^\s\\]+$/i.test(value))return ''
  try{const target=new url.URL(value);return target.hostname&&!target.username&&!target.password?target.href:''}
  catch(_){return ''}
}
function plainTail(text:string,live:boolean):string{
  // A split delimiter at the live cursor is provisional. Completed unmatched markup is literal.
  if(!live)return text
  return text.replace(/(^|[\s，。：:（(])\*\*(?=[^\s*])([^*\n]{0,300})\*?$/,'$1$2')
    .replace(/(^|[\s，。：:（(])\*\*$/,'$1')
}
function inline(text:string,live:boolean=false):AnswerRun[]{
  const runs:AnswerRun[]=[]
  const tokens=/(\*\*[^*\n]{1,300}\*\*|\x60[^\x60\n]{1,300}\x60|\[[^\]\n]{1,160}\]\([^\s)\n]{1,2048}\))/g
  let start=0,match:RegExpExecArray|null=tokens.exec(text)
  while(match!==null){
    if(match.index>start)runs.push(new AnswerRun('plain',text.slice(start,match.index)))
    const value=match[0]
    if(value.startsWith('**'))runs.push(new AnswerRun('strong',value.slice(2,-2)))
    else if(value.startsWith('\x60'))runs.push(new AnswerRun('code',value.slice(1,-1)))
    else{const sep=value.lastIndexOf(']('),target=safeTarget(value.slice(sep+2,-1));runs.push(target?new AnswerRun('link',value.slice(1,sep),target):new AnswerRun('plain',value))}
    start=tokens.lastIndex;match=tokens.exec(text)
  }
  if(start<text.length){const tail=plainTail(text.slice(start),live);if(tail)runs.push(new AnswerRun('plain',tail))}
  return runs
}
function proseGroups(text:string):string[]{
  if(text.length<200||text.includes('\n'))return [text]
  const sentences=text.match(/[^。！？!?]+[。！？!?]?|[。！？!?]/g)||[text]
  const chunks:string[]=[]
  let group=''
  for(const sentence of sentences){
    if(group&&group.length+sentence.length>180){chunks.push(group);group=''}
    group+=sentence
  }
  if(group)chunks.push(group)
  return chunks
}
export function answerBlocks(content:string,live:boolean=false):AnswerBlock[]{
  const lines=content.replace(/\r\n?/g,'\n').split('\n')
  const blocks:AnswerBlock[]=[],prose:string[]=[],code:string[]=[]
  let fenced=false,ordered=-1
  const flush=(last:boolean):void=>{
    if(!prose.length)return
    const groups=proseGroups(prose.join('\n'));prose.length=0
    groups.forEach((group:string,i:number):void=>{blocks.push(new AnswerBlock('p',inline(group,live&&last&&i===groups.length-1)))})
  }
  for(let i=0;i<lines.length;i++){
    const line=lines[i],last=i===lines.length-1
    if(/^\s*\x60{3}/.test(line)){flush(false);ordered=-1
      if(fenced){blocks.push(new AnswerBlock('pre',[new AnswerRun('plain',code.join('\n'))]));code.length=0}
      fenced=!fenced;continue
    }
    if(fenced){code.push(line);continue}
    if(!line.trim()){flush(false);ordered=-1;continue}
    const heading=line.match(/^\s*(#{1,4})\s+(.+)$/),item=line.match(/^\s*(?:(\d{1,3})[.)、]\s+|[-*•]\s+)(.+)$/)
    if(heading){flush(false);ordered=-1;blocks.push(new AnswerBlock(heading[1].length>2?'h4':'h3',inline(heading[2],live&&last)));continue}
    if(item){flush(false);let marker='•'
      if(item[1]){ordered=ordered<0?Number(item[1]):ordered+1;marker=String(ordered)+'.'}else ordered=-1
      blocks.push(new AnswerBlock('li',inline(item[2],live&&last),marker));continue
    }
    ordered=-1
    if(/^\s*>\s?/.test(line)){flush(false);blocks.push(new AnswerBlock('quote',inline(line.replace(/^\s*>\s?/,''),live&&last)));continue}
    prose.push(line)
  }
  flush(true)
  if(fenced)blocks.push(new AnswerBlock('pre',[new AnswerRun('plain',code.join('\n'))]))
  return blocks
}
