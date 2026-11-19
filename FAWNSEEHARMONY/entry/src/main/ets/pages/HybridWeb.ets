import { webview } from '@kit.ArkWeb'
import { common, bundleManager } from '@kit.AbilityKit'
import { util } from '@kit.ArkTS'
import { hybridBusiness } from '../services/HybridBusiness'
import { hybridDevice, HybridError } from '../services/HybridDevice'
import { abortHybridVoice } from '../services/HybridDeviceVoice'
import { chooseHybridWebFile } from '../services/HybridDeviceFiles'
import { requirePrivacy } from '../services/PrivacyChoices'
import { requestHybridConsent } from '../services/HybridConsent'
import { actorId } from '../services/Cloud'

type Values = Record<string,Object>
interface RequestEnvelope {id:string; action:string; params:Values; scope_id?:string; session_epoch?:string|number}
const entry='resource://rawfile/fawnsee-web/index.html'
const localEntry=(v:string):boolean=>v.split('#')[0]===entry
const queueLimit=32
const messageLimit=24*1024*1024
let current:Transport|undefined

class Transport {
  controller:webview.WebviewController
  ctx:common.UIAbilityContext
  privacy:(settings:boolean)=>void
  key:string=util.generateRandomUUID()
  generation:number=0
  live:boolean=true
  pending:Set<string>=new Set()
  constructor(controller:webview.WebviewController,ctx:common.UIAbilityContext,privacy:(settings:boolean)=>void) {
    this.controller=controller; this.ctx=ctx; this.privacy=privacy
  }
  reset():void {this.generation++; this.pending.clear(); abortHybridVoice().catch(()=>{})}
  evaluate(js:string):Promise<string>{
    if(!this.live) return Promise.resolve('')
    return this.controller.runJavaScript(js)
  }
  postMessage(key:string, serialized:string):void {
    if(!this.live||key!==this.key||typeof serialized!=='string'||serialized.length>messageLimit)return
    this.dispatch(serialized).catch(()=>{})
  }
  async deliver(value:Values, epoch:number):Promise<void> {
    if(!this.live||epoch!==this.generation)return
    const json=JSON.stringify(value)
    if(json.length<=192000){
      await this.evaluate('window.FAWNSEE_NATIVE?.receive('+JSON.stringify(json)+')')
      return
    }
    // Keep each ArkWeb IPC message small, including an image selection with several photos.
    const id=JSON.stringify(util.generateRandomUUID())
    try {
      for(let at=0;at<json.length;at+=192000){
        if(!this.live||epoch!==this.generation)return
        const part=JSON.stringify(json.slice(at,at+192000))
        await this.evaluate('window.__fawnseeInbox.append('+id+','+part+')')
      }
      if(this.live&&epoch===this.generation)await this.evaluate('window.__fawnseeInbox.finish('+id+')')
    }finally{if(this.live&&epoch===this.generation)await this.evaluate('window.__fawnseeInbox.clear('+id+')')}
  }
  async dispatch(raw:string):Promise<void>{
    const req=JSON.parse(raw) as RequestEnvelope
    if(!req||typeof req.id!=='string'||req.id.length>100||typeof req.action!=='string'||req.action.length>120||
      !req.params||typeof req.params!=='object'||Array.isArray(req.params)||this.pending.has(req.id))return
    const epoch=this.generation
    if(this.pending.size>=queueLimit){await this.deliver({'id':req.id,'ok':false,'error':{'code':'busy','message':'请等待当前操作完成。'} as Values},epoch);return}
    this.pending.add(req.id)
    const owner=actorId()
    const emit=(name:string,data:Values):void=>{
      if(owner!==actorId())return
      if(req.scope_id?.startsWith('guest:')){data['scope_id']=req.scope_id;data['session_epoch']=req.session_epoch??0}
      this.deliver({'event':name,'result':data},epoch).catch(()=>{})
    }
    try{
      requirePrivacy()
      let result:Object|null=null
      if(req.action==='privacy.document'||req.action==='privacy.settings') {
        this.privacy(req.action==='privacy.settings')
      }else if(req.action==='privacy.health'){
        result={'allowed':await requestHybridConsent('health')} as Values
      }else if(req.action.startsWith('native.')&&req.action!=='native.cancelChat'){
        result=await hybridDevice(this.ctx,req.action,req.params,emit)
      }else{
        result=await hybridBusiness(req.action,req.params,this.ctx,emit)
      }
      if(!this.live||epoch!==this.generation)return
      const answer:Record<string,Object|null>={'id':req.id,'ok':true,'result':result}
      await this.deliver(answer as Values,epoch)
    }catch(error){
      const problem=error as HybridError
      const fault:Values={'code':typeof problem.code==='string'?problem.code:'request_failed','message':problem.message||'操作未完成，请重试。'}
      await this.deliver({'id':req.id,'ok':false,'error':fault},epoch)
    }finally{if(epoch===this.generation)this.pending.delete(req.id)}
  }
  ready():void {
    // Never return the key to a JS caller. ArkWeb runs this script only in the main document.
    setTimeout(()=>{
      if(!this.live)return
      try {
        if(!localEntry(this.controller.getUrl()))return
        this.evaluate('if(window===top&&location.href.split("#")[0]==='+JSON.stringify(entry)+
          ')window.__activateFawnseeBridge?.('+JSON.stringify(this.key)+')').catch(()=>{})
      } catch (_) {}
    },0)
  }
}

export function hybridVisibility(foreground:boolean):void {
  const host=current
  if(!host)return
  if(!foreground)abortHybridVoice().catch(()=>{})
  host.deliver({'event':'appVisibility','result':{'foreground':foreground} as Values},host.generation).catch(()=>{})
}

export function hybridBack():boolean {
  const host=current
  if(!host||!host.live)return false
  host.evaluate('(()=>{if(window.FAWNSEE_APP_UPDATE?.isOpen){window.FAWNSEE_APP_UPDATE.close();return true}return Boolean(window.FAWNSEE_NATIVE?.onBack?.())})()')
    .then((handled:string)=>{if(handled!=='true'&&current===host)host.ctx.moveAbilityToBackground().catch(()=>{})}).catch(()=>{})
  return true
}

export function hybridNavigate(route:string):void {
  const hash=route.startsWith('#')?route:'#'+route
  if(!/^#[a-zA-Z0-9_/?=&.%-]+$/.test(hash))return
  current?.evaluate('typeof go==="function"?go('+JSON.stringify(hash)+',true):(location.hash='+JSON.stringify(hash)+')').catch(()=>{})
}

export function hybridSessionExpired():void {
  if(!current)return
  current.reset()
  current.evaluate('location.replace(location.href.split("#")[0]+"#auth/login");location.reload()').catch(()=>{})
}

@Component
export struct HybridWeb {
  onPrivacy:(settings:boolean)=>void=()=>{}
  private controller:webview.WebviewController=new webview.WebviewController()
  private transport:Transport=new Transport(this.controller,this.getUIContext().getHostContext() as common.UIAbilityContext,
    (settings:boolean):void=>this.onPrivacy(settings))
  aboutToAppear():void{
    this.transport.live=true
    current=this.transport
    const app=bundleManager.getBundleInfoForSelfSync(bundleManager.BundleFlag.GET_BUNDLE_INFO_WITH_APPLICATION).appInfo
    webview.WebviewController.setWebDebuggingAccess(app.debug===true)
  }
  aboutToDisappear():void{
    this.transport.live=false
    this.transport.reset()
    if(current===this.transport)current=undefined
  }
  build(){
    Web({src:$rawfile('fawnsee-web/index.html'),controller:this.controller})
      .id('fawnsee-hybrid-web').width('100%').height('100%')
      .javaScriptAccess(true).domStorageAccess(true).databaseAccess(false)
      .fileAccess(false).blockNetwork(true).onlineImageAccess(true)
      .mixedMode(MixedMode.None).multiWindowAccess(false).zoomAccess(false)
      .keyboardAvoidMode(WebKeyboardAvoidMode.RESIZE_CONTENT)
      .javaScriptProxy({object:this.transport,name:'FawnseeArkTransport',methodList:['ready','postMessage'],controller:this.controller})
      .onPageBegin((event:OnPageBeginEvent):void=>{
        if(localEntry(event.url)){this.transport.reset();this.transport.key=util.generateRandomUUID()}
      })
      .onPageEnd((event:OnPageEndEvent):void=>{if(localEntry(event.url))this.transport.ready()})
      .onLoadIntercept((e:OnLoadInterceptEvent):boolean=>{
        const uri=e.data.getRequestUrl()
        if(e.data.isMainFrame())return !localEntry(uri)
        return !(uri==='about:blank'||uri==='about:srcdoc'||uri.startsWith('resource://rawfile/fawnsee-web/')||uri.startsWith('data:')||uri.startsWith('blob:'))
      })
      .onPermissionRequest((event:OnPermissionRequestEvent):void=>event.request.deny())
      .onShowFileSelector((event:OnShowFileSelectorEvent):boolean=>{
        chooseHybridWebFile(this.transport.ctx,event)
        return true
      })
  }
}
