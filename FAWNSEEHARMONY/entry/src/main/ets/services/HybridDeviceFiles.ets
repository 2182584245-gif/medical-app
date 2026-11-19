import { common } from '@kit.AbilityKit'
import { fileIo as fs, picker, fileUri } from '@kit.CoreFileKit'
import { photoAccessHelper } from '@kit.MediaLibraryKit'
import { util } from '@kit.ArkTS'
import { readPhoto } from './Cloud'

const formats:Record<string,string>={'pdf':'application/pdf','txt':'text/plain','md':'text/markdown','markdown':'text/markdown','csv':'text/csv',
  'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp','png':'image/png'}
const maxDocument=5*1024*1024

export async function chooseHybridDocument(ctx:common.UIAbilityContext):Promise<Record<string,Object>[]> {
  const uris=await new picker.DocumentViewPicker(ctx).select({maxSelectNumber:1,fileSuffixFilters:['图片及文档|.jpg,.jpeg,.png,.webp,.pdf,.txt,.md,.markdown,.csv,.docx']})
  if(!uris.length)return []
  const uri=uris[0], name=new fileUri.FileUri(uri).name
  const dot=name.lastIndexOf('.')
  const mime=dot<0?'':formats[name.slice(dot+1).toLowerCase()]
  if(!mime)throw new Error('请选择图片、PDF、TXT、Markdown、CSV 或 DOCX 文件。')
  if(mime.startsWith('image/')){
    const photo=await readPhoto(uri), start=photo.indexOf(','), base=photo.slice(start+1)
    const actualMime=photo.slice(5,photo.indexOf(';'))
    const ext=actualMime==='image/jpeg'?'jpg':actualMime==='image/png'?'png':'webp'
    const size=Math.floor(base.length*3/4)-(base.endsWith('==')?2:base.endsWith('=')?1:0)
    if(size>2*1024*1024)throw new Error('请选择 2 MB 以内的图片。')
    return [{'name':name.slice(0,dot)+'.'+ext,'media_type':actualMime,'size_bytes':size,'data_base64':base}]
  }
  const fd=await fs.open(uri,fs.OpenMode.READ_ONLY)
  try {
    const size=(await fs.stat(fd.fd)).size
    if(size<1||size>maxDocument)throw new Error('请选择 5 MB 以内的非空文件。')
    const content=new Uint8Array(size)
    let offset=0
    while(offset<size){
      const buffer=new ArrayBuffer(Math.min(size-offset,262144))
      const read=await fs.read(fd.fd,buffer,{offset:offset,length:buffer.byteLength})
      if(!read)throw new Error('未能读取完整文件，请重新选择。')
      content.set(new Uint8Array(buffer,0,read),offset);offset+=read
    }
    return [{'name':name,'media_type':mime,'size_bytes':size,'data_base64':new util.Base64Helper().encodeToStringSync(content)}]
  }finally{await fs.close(fd)}
}

export async function chooseHybridWebFile(ctx:common.UIAbilityContext,event:OnShowFileSelectorEvent):Promise<void>{
  const count=event.fileSelector.getMode()===FileSelectorMode.FileOpenMultipleMode?4:1
  try{
    const accept=event.fileSelector.getAcceptType()
    const photos=accept.length>0&&accept.every((type:string)=>type.startsWith('image/')||/^\.(jpg|jpeg|png|webp)$/i.test(type))
    if(photos){
      const picked=await new photoAccessHelper.PhotoViewPicker().select({MIMEType:photoAccessHelper.PhotoViewMIMETypes.IMAGE_TYPE,maxSelectNumber:count})
      event.result.handleFileList(picked.photoUris)
    }else{
      const picked=await new picker.DocumentViewPicker(ctx).select({maxSelectNumber:count})
      event.result.handleFileList(picked)
    }
  }catch(_){event.result.handleFileList([])}
}
