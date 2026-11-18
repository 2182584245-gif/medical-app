import { common } from '@kit.AbilityKit'
import { util } from '@kit.ArkTS'
import { fileIo as fs, picker, fileUri } from '@kit.CoreFileKit'
import { pdfService } from '@kit.PDFKit'
import { zlib } from '@kit.BasicServicesKit'

const formats:Record<string,string>={'pdf':'application/pdf','txt':'text/plain','md':'text/markdown','markdown':'text/markdown','csv':'text/csv','docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp'}
const codec=new util.Base64Helper()
export class AssistantFile {
  name:string=''
  mime:string=''
  content:string=''
  extracted:string=''
  method:string=''
  fileId:number=0
}
function documentXml(bytes:Uint8Array):DataView {
  return new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength)
}
async function docxText(bytes:Uint8Array):Promise<string>{
  const d=documentXml(bytes), n=bytes.byteLength
  let end=n-22
  while(end>=Math.max(0,n-65557)&&d.getUint32(end,true)!==0x06054b50)end--
  if(end<0||end<Math.max(0,n-65557))throw new Error('Word 文档结构不完整。')
  let head=d.getUint32(end+16,true)
  for(let i=0;i<d.getUint16(end+10,true);i++){
    if(head+46>n||d.getUint32(head,true)!==0x02014b50)break
    const nameLength=d.getUint16(head+28,true), next=head+46+nameLength+d.getUint16(head+30,true)+d.getUint16(head+32,true)
    if(next>n)break
    const name=util.TextDecoder.create('utf-8').decodeWithStream(bytes.slice(head+46,head+46+nameLength))
    if(name==='word/document.xml'){
      const start=d.getUint32(head+42,true), size=d.getUint32(head+24,true), packed=d.getUint32(head+20,true), method=d.getUint16(head+10,true)
      if((d.getUint16(head+8,true)&1)||size<1||size>20*1024*1024||start+30>n||d.getUint32(start,true)!==0x04034b50)throw new Error('Word 文档无法读取。')
      const offset=start+30+d.getUint16(start+26,true)+d.getUint16(start+28,true)
      if(offset+packed>n)throw new Error('Word 文档不完整。')
      let data=bytes.slice(offset,offset+packed)
      if(method===8){
        const zip=await zlib.createZip(), output=new ArrayBuffer(size)
        const stream:zlib.ZStream={nextIn:data.buffer as ArrayBuffer,availableIn:packed,nextOut:output,availableOut:size}
        if(await zip.inflateInit2(stream,-15)!==zlib.ReturnStatus.OK)throw new Error('无法解压 Word 正文。')
        try{
          const status=await zip.inflate(stream,zlib.CompressFlushMode.FINISH)
          if(status!==zlib.ReturnStatus.STREAM_END||stream.totalOut!==size)throw new Error('Word 正文不完整。')
          data=new Uint8Array(stream.nextOut || output)
        }finally{await zip.inflateEnd(stream)}
      }else if(method!==0||packed!==size)throw new Error('暂不能读取此 Word 压缩格式。')
      const xml=util.TextDecoder.create('utf-8').decodeWithStream(data)
      const chunks=xml.match(/<w:t(?:\s[^>]*)?>[\s\S]*?<\/w:t>|<\/w:p>/g)||[]
      return chunks.map((p:string)=>p==='</w:p>'?'\n':p.replace(/^<w:t(?:\s[^>]*)?>/,'').replace(/<\/w:t>$/,'').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&apos;/g,"'").replace(/&amp;/g,'&')).join('').trim()
    }
    head=next
  }
  throw new Error('Word 文档中未找到正文。')
}
async function pdfText(ctx:common.UIAbilityContext, bytes:Uint8Array):Promise<string>{
  const path=ctx.cacheDir+'/chat-'+util.generateRandomUUID(true)+'.pdf'
  const pdf=new pdfService.PdfDocument()
  let loaded=false
  try{
    const file=await fs.open(path,fs.OpenMode.CREATE|fs.OpenMode.WRITE_ONLY)
    try{await fs.write(file.fd,bytes.buffer as ArrayBuffer)}finally{await fs.close(file)}
    if(pdf.loadDocument(path)!==pdfService.ParseResult.PARSE_SUCCESS)throw new Error('无法打开 PDF，请检查文档或密码保护。')
    loaded=true
    const pages:string[]=[];let total=0
    for(let i=0;i<pdf.getPageCount();i++){
      const page=pdf.getPage(i)
      try{const value=page.getTextContent();total+=value.length;if(total>50000)throw new Error('附件超过 50000 字，请拆分文件。');pages.push(value)}
      finally{page.release()}
    }
    return pages.join('\n').trim()
  }finally{if(loaded)pdf.releaseDocument();try{await fs.unlink(path)}catch(_){}}
}
export async function prepareAssistantFile(ctx:common.UIAbilityContext,name:string,base:string,id:number=0):Promise<AssistantFile>{
  const ext=name.split('.').pop()?.toLowerCase()||'',mime=formats[ext]
  if(!mime)throw new Error('请选择图片、PDF、TXT、MD、CSV 或 DOCX。')
  const bytes=codec.decodeSync(base),img=mime.startsWith('image/')
  if(!bytes.byteLength||bytes.byteLength>(img?2:5)*1024*1024)throw new Error('图片最大 2 MB，文档最大 5 MB。')
  const f=new AssistantFile();f.name=name;f.mime=mime;f.content=base;f.fileId=id
  if(img){f.method='native_image';return f}
  if(ext==='docx'){f.method='docx_text';f.extracted=await docxText(bytes)}
  else if(ext==='pdf'){f.method='pdf_text_layer';f.extracted=await pdfText(ctx,bytes)}
  else{f.method='local_text';let text='';try{text=util.TextDecoder.create('utf-8',{fatal:true}).decodeWithStream(bytes)}catch(_){text=util.TextDecoder.create('gb18030').decodeWithStream(bytes)}f.extracted=text.trim()}
  if(!f.extracted)throw new Error('文件没有可读取的文字。扫描件可以用图片发送。')
  if(f.extracted.length>50000)throw new Error('附件超过 50000 字，请拆分文件。')
  return f
}
export async function chooseAssistantFile(ctx:common.UIAbilityContext):Promise<AssistantFile|undefined>{
  const dialog=new picker.DocumentViewPicker(ctx)
  const uris=await dialog.select({maxSelectNumber:1,fileSuffixFilters:['图片及文档|.png,.jpg,.jpeg,.webp,.pdf,.txt,.md,.markdown,.csv,.docx']})
  if(!uris.length)return undefined
  const uri=uris[0],name=new fileUri.FileUri(uri).name
  const file=await fs.open(uri,fs.OpenMode.READ_ONLY)
  try{
    const size=(await fs.stat(file.fd)).size
    if(size<1||size>5*1024*1024)throw new Error('请选择 5 MB 以内的文件。')
    const data=new Uint8Array(size)
    let read=0
    while(read<size){const part=new ArrayBuffer(Math.min(size-read,262144));const count=await fs.read(file.fd,part,{offset:read,length:part.byteLength});if(!count)throw new Error('文件未完整读取。');data.set(new Uint8Array(part,0,count),read);read+=count}
    return await prepareAssistantFile(ctx,name,codec.encodeToStringSync(data))
  }finally{await fs.close(file)}
}
