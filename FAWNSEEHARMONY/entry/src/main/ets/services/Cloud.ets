import { ChatEventReader, StreamEvent } from '../pages/AssistantStream'
import http from '@ohos.net.http'
import { removeMemberDrafts,clearMemberRecordCache } from './PrivateData'
import { asset } from '@kit.AssetStoreKit'
import { util } from '@kit.ArkTS'
import { fileIo as fs } from '@kit.CoreFileKit'
import { photoAccessHelper } from '@kit.MediaLibraryKit'
import { image } from '@kit.ImageKit'
import { BusinessError } from '@kit.BasicServicesKit'
import { cryptoFramework } from '@kit.CryptoArchitectureKit'

const guestAlias=new util.TextEncoder().encodeInto('fawnsee.harmony.guest.v1')
let guestToken=''
let openingGuest:Promise<string>|null=null
interface GuestCredentials { creation_key:string; guest_token?:string }
async function guestCredential():Promise<string> {
  if(guestToken)return guestToken
  if(openingGuest)return openingGuest
  openingGuest=openGuest().finally(()=>{openingGuest=null})
  return openingGuest
}
async function storedGuest():Promise<GuestCredentials> {
  const query:asset.AssetMap=new Map()
  query.set(asset.Tag.ALIAS,guestAlias); query.set(asset.Tag.RETURN_TYPE,asset.ReturnType.ALL)
  let saved:GuestCredentials={creation_key:''}
  try {
    const results=await asset.query(query)
    if(results.length) saved=JSON.parse(util.TextDecoder.create('utf-8').decodeToString(results[0].get(asset.Tag.SECRET) as Uint8Array)) as GuestCredentials
  } catch(e){if((e as BusinessError).code!==asset.ErrorCode.NOT_FOUND)throw e as Error}
  return saved
}
async function openGuest():Promise<string> {
  const saved=await storedGuest()
  if(!saved.creation_key){
    const random=await cryptoFramework.createRandom().generateRandom(32)
    saved.creation_key=new util.Base64Helper().encodeToStringSync(random.data).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')
    await vaultWrite(JSON.stringify(saved),guestAlias)
  }
  if(!saved.guest_token) {
    const created=await transport<GuestCredentials>('/v1/guest/bootstrap','POST',{'creation_key':saved.creation_key} as Record<string,Object|null>)
    if(!created.guest_token || !/^gs1_[A-Za-z0-9_-]{43}$/.test(created.guest_token))throw new Error('体验服务响应不完整。')
    saved.guest_token=created.guest_token
    await vaultWrite(JSON.stringify(saved),guestAlias)
  }
  guestToken=saved.guest_token
  return guestToken
}
export async function guestAction<T>(action:string,params:Object={} as Record<string,Object>,operation?:string):Promise<T> {
  const epoch=generation
  if(session)throw new Error('请返回当前会员会话。')
  const token=await guestCredential()
  if(epoch!==generation || session)throw new Error('体验会话已切换。')
  const result=await transport<T>('/v1/guest/request','POST',{'action':action,'params':params,'request_id':operation||operationId()} as Record<string,Object|null>,token)
  if(epoch!==generation || session)throw new Error('体验会话已切换。')
  return result
}
export async function guestVoice(body:Object):Promise<Record<string,string>> {
  return transport<Record<string,string>>('/v1/guest/voice/transcribe','POST',body,await guestCredential())
}
export async function purgeGuest():Promise<void> {
  if(openingGuest)await openingGuest
  let token=guestToken
  if(!token){
    const saved=await storedGuest()
    if(saved.creation_key)token=saved.guest_token||await guestCredential()
  }
  if(token) {
    try {await transport<Object>('/v1/guest/purge','POST',{} as Record<string,Object>,token)} catch(e){if(!(e instanceof CloudError) || ![401,410].includes(e.status))throw e as Error}
  }
  await removeMemberDrafts(0); await vaultWrite('',guestAlias); guestToken=''
}
async function guestRpc<T>(service:string, method:string,args:(Object|null)[],kwargs:Object,id?:string):Promise<T> {
  const p=kwargs as Record<string,Object>
  if(service==='preferences'){
    if(method==='get_preferences')return guestAction<T>('preferences.get')
    if(method==='update_preferences')return guestAction<T>('preferences.update',args[1]||p,id)
  }
  if(service==='chat'){
    switch(method){
      case 'create_conversation': return guestAction<T>('chat.conversations.create',{'title':args[1]||'新对话'} as Record<string,Object|null>,id)
      case 'list_conversations': return guestAction<T>('chat.conversations.list')
      case 'ensure_greeting': return guestAction<T>('chat.greeting.ensure',p,id)
      case 'list_messages_page': return guestAction<T>('chat.messages.list',p)
      case 'rename_conversation': return guestAction<T>('chat.conversations.rename',{'conversation_id':args[1],'title':args[2]} as Record<string,Object|null>,id)
      case 'delete_conversations': return guestAction<T>('chat.conversations.delete',{'conversation_ids':args[1]} as Record<string,Object|null>,id)
      case 'list_message_attachments': return guestAction<T>('chat.message.attachments',{'message_id':args[1]} as Record<string,Object|null>)
    }
  }
  if(service==='files'){
    if(method==='list_files')return guestAction<T>('chat.files.list')
    if(method==='get_file')return guestAction<T>('files.get',{'file_id':args[1]} as Record<string,Object|null>)
    if(method==='delete_file')return guestAction<T>('files.delete',{'file_id':args[1]} as Record<string,Object|null>,id)
  }
  throw new CloudError(403,'此功能需要登录会员账号。')
}

const ORIGIN = 'https://39.106.166.15/aliyun'
export interface Member { id: number; username: string; display_name?: string; role_code: string; account_status: string }
interface OfflineLease {version:number;actor_id:number;role_code:string;server_instance_id:string;issued_at:string;expires_at:string}
interface Session { access_token: string; refresh_token?: string; user: Member;offline_lease?:OfflineLease;onboarding_done?:boolean;data_generation?:string }
interface RpcRequest { args: (Object|null)[]; kwargs: Object; request_id: string }
interface WireValue { $__type?: string; value?: string; fields?: Object; items?: Object[] }
interface ErrorEnvelope { detail?: string|Object; message?: string }
let session: Session|null = null
let generation = 0
let remember = false
let refreshing: Promise<void>|null = null
let storageQueue: Promise<void> = Promise.resolve()
const alias = new util.TextEncoder().encodeInto('fawnsee.harmony.member.v1')
export function actorId(): number { return session?.user.id ?? 0 }
export function recordCacheScope(owner:number):string {
  const lease=session?.offline_lease
  if(!lease||owner<=0||actorId()!==owner||lease.version!==1||lease.actor_id!==owner||lease.role_code!=='member'||
    !/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(lease.server_instance_id))return ''
  const issued=Date.parse(lease.issued_at),expires=Date.parse(lease.expires_at),now=Date.now()
  if(!Number.isFinite(issued)||!Number.isFinite(expires)||expires<=issued||issued>now+5000||expires<=now||expires-issued>43200000)return ''
  const dataGeneration=session?.data_generation||''
  return /^[a-f0-9]{64}$/.test(dataGeneration)?lease.server_instance_id+'|'+dataGeneration:''
}
export function currentMember(): Member|null { return session?.user ?? null }
export function canOpenOfflineRecords():boolean {return session?.onboarding_done===true && !!recordCacheScope(actorId())}
export async function rememberOnboarding(owner:number,done:boolean):Promise<void>{
  if(!session||session.user.id!==owner||session.onboarding_done===done)return
  session.onboarding_done=done
  if(remember)await persist(JSON.stringify(session))
}
export function operationId(): string { return util.generateRandomUUID(true) }
export class CloudError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}
export class CloudNetworkError extends Error {
  constructor() {super('暂时无法连接云端，请检查网络后重试。')}
}

async function vaultWrite(value: string, key: Uint8Array = alias): Promise<void> {
  const query: asset.AssetMap = new Map()
  query.set(asset.Tag.ALIAS, key)
  if (!value) {
    try { await asset.remove(query) } catch (e) { if ((e as BusinessError).code !== asset.ErrorCode.NOT_FOUND) throw e as Error }
    return
  }
  query.set(asset.Tag.SECRET, new util.TextEncoder().encodeInto(value))
  query.set(asset.Tag.ACCESSIBILITY, asset.Accessibility.DEVICE_UNLOCKED)
  query.set(asset.Tag.CONFLICT_RESOLUTION, asset.ConflictResolution.OVERWRITE)
  await asset.add(query)
}
function persist(value: string): Promise<void> {
  const write = storageQueue.then(() => vaultWrite(value))
  storageQueue = write.catch(() => {})
  return write
}
function valid(value: Session): boolean {
  return !!value && typeof value.access_token === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value.access_token) &&
    value.user?.id > 0 && value.user.role_code === 'member' && value.user.account_status === 'active'
}
export async function restoreSession(): Promise<boolean> {
  const epoch = generation
  const query: asset.AssetMap = new Map()
  query.set(asset.Tag.ALIAS, alias)
  query.set(asset.Tag.RETURN_TYPE, asset.ReturnType.ALL)
  try {
    const items = await asset.query(query)
    if (!items.length || epoch !== generation) return false
    const bytes = items[0].get(asset.Tag.SECRET) as Uint8Array
    const saved: Session = JSON.parse(util.TextDecoder.create('utf-8').decodeToString(bytes)) as Session
    if (!valid(saved)) { await persist(''); return false }
    session = saved; remember = true; return true
  } catch (e) {
    if ((e as BusinessError).code === asset.ErrorCode.NOT_FOUND) return false
    throw new Error('暂时无法读取此设备保存的登录状态。')
  }
}
function reason(status: number, raw: string): string {
  let detail = ''
  try { const body = JSON.parse(raw) as ErrorEnvelope; if (typeof body.detail === 'string') detail = body.detail } catch (_) {}
  if (detail && /[\u4e00-\u9fff]/.test(detail) && detail.length < 240) return detail
  if (status === 401) return '登录信息无效或已过期，请重新登录。'
  if (status === 403) return '当前账号无法执行此操作。'
  if (status === 409) return '数据已有变化，请重新打开核对。'
  if (status === 422) return '请核对填写的内容。'
  if (status === 429) return '操作过于频繁，请稍后重试。'
  return '云端暂时无法完成本次请求（' + status + '）。'
}
async function transport<T>(path: string, method: string, body?: Object, token: string = '', id: string = ''): Promise<T> {
  if (!path.startsWith('/v1/') || path.includes('..') || path.includes('\\') || path.includes('#')) throw new Error('云端地址无效。')
  const httpRequest = http.createHttp()
  const headers: Record<string,string> = { 'Accept': 'application/json', 'Content-Type': 'application/json' }
  if (token) headers['Authorization'] = (path.startsWith('/v1/guest/') ? 'Guest ' : 'Bearer ') + token
  if (id) headers['X-Request-ID'] = id
  if (path.endsWith('/voice/transcribe')) headers['X-Fawnsee-ASR-Contract'] = '2'
  const options: http.HttpRequestOptions = {
    method: method as http.RequestMethod, header: headers, connectTimeout: 12000, readTimeout: 180000,
    expectDataType: http.HttpDataType.STRING, usingCache: false, usingProxy: false, maxLimit: 48 * 1024 * 1024
  }
  if (body !== undefined) options.extraData = JSON.stringify(body)
  try {
    const response = await httpRequest.request(ORIGIN + path, options)
    const raw = response.result as string
    if (response.responseCode < 200 || response.responseCode > 299) throw new CloudError(response.responseCode, reason(response.responseCode,raw))
    return (raw && raw.trim() ? JSON.parse(raw) : null) as T
  } catch (e) {
    if (e instanceof CloudError) throw e
    if ([2300005,2300006,2300007,2300028,2300052,2300055,2300056].includes((e as BusinessError).code)) throw new CloudNetworkError()
    throw new Error('未能读取云端响应，请检查网络后重试。')
  } finally { httpRequest.destroy() }
}
export async function signIn(path: string, body: Object, auto: boolean): Promise<void> {
  if (!['/v1/auth/login','/v1/auth/phone/quick-entry','/v1/auth/register/complete'].includes(path)) throw new Error('登录方式无效。')
  const epoch = ++generation
  const result = await transport<Session>(path,'POST',body)
  if (epoch !== generation) throw new Error('登录状态已改变。')
  if (!valid(result)) throw new Error('会员登录信息无效。')
  await persist(auto ? JSON.stringify(resultForStorage(result)) : '')
  if (epoch !== generation) throw new Error('登录状态已改变。')
  session = resultForStorage(result); remember = auto
  await purgeGuest().catch(() => {})
}
export async function completeCredentials(body:Object):Promise<void> {
  const epoch=generation
  const result=await cloud<Session>('/v1/auth/credentials/complete','POST',body)
  if(epoch!==generation || !valid(result) || result.user.id!==actorId())throw new Error('登录身份已改变。')
  session=resultForStorage(result)
  if(remember)await persist(JSON.stringify(session))
}
function resultForStorage(value: Session): Session {
  return { access_token: value.access_token, refresh_token: value.refresh_token, user: value.user,offline_lease:value.offline_lease,data_generation:value.data_generation }
}
export async function sendCode(phone: string, purpose: string): Promise<string> {
  const result = await transport<Record<string,string>>('/v1/auth/phone/send','POST',{'phone':phone,'purpose':purpose} as Record<string,Object|null>)
  return result['challenge_id']
}
async function renew(epoch: number): Promise<void> {
  if(epoch!==generation)throw new Error('账号已切换。')
  const old = session
  if (!old?.refresh_token) throw new CloudError(401,'登录已过期，请重新登录。')
  const next = await transport<Session>('/v1/auth/refresh','POST',{'refresh_token':old.refresh_token,'request_id':operationId()} as Record<string,Object|null>)
  if (epoch !== generation || !valid(next) || next.user.id !== old.user.id) throw new Error('账号状态已改变。')
  session = resultForStorage(next)
  session.onboarding_done=old.data_generation===next.data_generation && old.onboarding_done===true
  if (remember) await persist(JSON.stringify(session))
}
export async function cloud<T>(path: string, method: string = 'GET', body?: Object, operation: string = ''): Promise<T> {
  const epoch = generation
  const initial = session
  if (!initial) throw new CloudError(401,'请先登录会员账号。')
  try {
    const value = await transport<T>(path,method,body,initial.access_token,operation)
    if (epoch !== generation) throw new Error('账号已切换。')
    return value
  } catch (e) {
    if (!(e instanceof CloudError) || e.status !== 401 || epoch !== generation) throw e as Error
    if (session === initial) {
      if (!refreshing) {
        initial.offline_lease=undefined
        refreshing=(async()=>{
          if(remember)await persist(JSON.stringify(initial))
          try {
            await renew(epoch)
          }catch(error) {
            if(epoch===generation)await clearMemberRecordCache(initial.user.id)
            throw error as Error
          }
          if(session?.data_generation!==initial.data_generation)await clearMemberRecordCache(initial.user.id)
        })().finally(() => { refreshing = null })
      }
      try { await refreshing } catch (failure) {
        if (epoch === generation && failure instanceof CloudError && (failure.status === 401 || failure.status === 403)) {
          await clearSession()
          AppStorage.setOrCreate('sessionExpired', true)
        }
        if(failure instanceof CloudNetworkError)throw new Error('请联网验证登录状态后再打开记录。')
        throw failure as Error
      }
    }
    if (epoch !== generation || !session) throw new Error('账号已切换。')
    const result = await transport<T>(path,method,body,session.access_token,operation)
    if (epoch !== generation) throw new Error('账号已切换。')
    return result
  }
}
export async function clearSession(): Promise<void> {
  const owner=actorId()
  ++generation; session = null; remember = false
  await persist('')
  if(owner)await clearMemberRecordCache(owner)
}
export async function signOut(): Promise<void> {
  const old = session
  if (old) {
    try { await transport<Object>('/v1/auth/logout','POST',undefined,old.access_token) }
    catch (e) { if (!(e instanceof CloudError) || e.status !== 401) throw e as Error }
  }
  await clearSession()
}
export async function rpc<T>(service: string, method: string, args: (Object|null)[], kwargs: Object = {} as Record<string,Object>, id?: string): Promise<T> {
  if (!session) return guestRpc<T>(service,method,args,kwargs,id)
  const body: RpcRequest = { args, kwargs, request_id: id || operationId() }
  const value = await cloud<Record<string,Object>>('/v1/rpc/'+service+'/'+method,'POST',body,body.request_id)
  if (!Object.keys(value).includes('result')) throw new Error('云端返回的内容不完整。')
  if (value['result'] === null) return value['result'] as T
  return JSON.parse(JSON.stringify(value['result']), (_key: string, item: Object|null) => {
    if (!item || typeof item !== 'object') return item
    const tagged = item as WireValue
    if (!tagged.$__type) return item
    if (['bytes','date','datetime'].includes(tagged.$__type)) return tagged.value
    if (['set','tuple'].includes(tagged.$__type)) return tagged.items
    return tagged.fields
  }) as T
}
export async function pickPhotos(max: number): Promise<string[]> {
  if (max < 1) return []
  const picker = new photoAccessHelper.PhotoViewPicker()
  const result = await picker.select({ MIMEType: photoAccessHelper.PhotoViewMIMETypes.IMAGE_TYPE, maxSelectNumber: Math.min(12,max) })
  const output: string[] = []
  for (const uri of result.photoUris) {
    output.push(await readPhoto(uri))
  }
  return output
}
export async function readPhoto(uri:string):Promise<string> {
    const file = await fs.open(uri,fs.OpenMode.READ_ONLY)
    try {
      const size = (await fs.stat(file.fd)).size
      if (size <= 0 || size > 48*1024*1024) throw new Error('这张图片过大或无内容，请换一张。')
      const source = image.createImageSource(file.fd)
      try {
        const info = await source.getImageInfo()
        if (size <= 2*1024*1024 && ['image/jpeg','image/png','image/webp'].includes(info.mimeType)) {
          const buffer = new ArrayBuffer(size)
          let offset = 0
          while (offset < size) {
            const part = new ArrayBuffer(size-offset)
            const read = await fs.read(file.fd,part,{ offset, length: part.byteLength })
            if (read <= 0) throw new Error('图片读取中断，请重试。')
            new Uint8Array(buffer).set(new Uint8Array(part,0,read),offset); offset += read
          }
          return 'data:'+info.mimeType+';base64,'+new util.Base64Helper().encodeToStringSync(new Uint8Array(buffer))
        } else {
          const scale = Math.min(1,1600/Math.max(info.size.width,info.size.height))
          const pixels = await source.createPixelMap({ desiredSize: { width: Math.max(1,Math.round(info.size.width*scale)), height: Math.max(1,Math.round(info.size.height*scale)) } })
          const packer = image.createImagePacker()
          try {
            const bytes = await packer.packing(pixels,{format:'image/jpeg',quality:85})
            return 'data:image/jpeg;base64,'+new util.Base64Helper().encodeToStringSync(new Uint8Array(bytes))
          } finally { await packer.release(); await pixels.release() }
        }
      } finally { await source.release() }
    } finally { await fs.close(file) }
}

// This transport exposes only the two chat stream routes, never the credential.
export async function assistantStream(body:Object,id:string,emit:(event:StreamEvent)=>void):Promise<StreamEvent>{
  const current=session, epoch=generation, guest=!current
  const token=current?current.access_token:await guestCredential()
  const request=http.createHttp(),decoder=util.TextDecoder.create('utf-8')
  const check=():void=>{if(epoch!==generation || guest && token!==guestToken)throw new Error('会话已经切换。')}
  const reader=new ChatEventReader(guest,(event:StreamEvent):void=>{check();emit(event)})
  let invalid:Error|undefined=undefined,fragment='',bytes=0
  const end=new Promise<void>((resolve:()=>void)=>{request.on('dataEnd',():void=>resolve())})
  request.on('dataReceive',(chunk:ArrayBuffer):void=>{
    if(invalid)return
    try{bytes+=chunk.byteLength;if(bytes>32*1024*1024)throw new Error('单次返回内容过大。')
      const text=decoder.decodeToString(new Uint8Array(chunk),{stream:true})
      if(fragment.length<4096)fragment+=text.slice(0,4096-fragment.length)
      reader.feed(text)
    }catch(e){invalid=e as Error}
  })
  try{
    check()
    const options:http.HttpRequestOptions={method:http.RequestMethod.POST,header:{'Accept':guest?'application/x-ndjson':'text/event-stream','Content-Type':'application/json','Authorization':(guest?'Guest ':'Bearer ')+token,'X-Request-ID':id} as Record<string,string>,extraData:JSON.stringify(body),connectTimeout:12000,readTimeout:180000,usingCache:false,usingProxy:false}
    const status=await request.requestInStream(ORIGIN+(guest?'/v1/guest/chat/stream':'/v1/ai/chat/stream'),options)
    if(status<200||status>=300)throw new CloudError(status,reason(status,fragment))
    await end;check()
    if(invalid)throw invalid as Error
    reader.feed(decoder.decodeToString(new Uint8Array(0),{stream:false}))
    return reader.finish()
  }finally{request.destroy()}
}
