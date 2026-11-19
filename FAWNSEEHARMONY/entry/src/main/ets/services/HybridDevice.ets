import { abilityAccessCtrl, bundleManager, common } from '@kit.AbilityKit'
import { reminderAgentManager } from '@kit.BackgroundTasksKit'
import { notificationManager } from '@kit.NotificationKit'
import { pasteboard } from '@kit.BasicServicesKit'
import { url } from '@kit.ArkTS'
import { actorId, pickPhotos } from './Cloud'
import { capturePhoto, CameraUnavailable, offerCameraGallery } from './Camera'
import { shareText } from './TextShare'
import { checkMarketUpdate, openMarketUpdate, updateFailure } from './AppUpdate'
import { setCalendarReminders, syncDeviceReminders, usesCalendarReminders } from './DeviceReminders'
import { chooseHybridDocument } from './HybridDeviceFiles'
import { devicePermissionState, explicitDevicePermission } from './HybridPermissions'
import { abortHybridVoice, beginHybridVoice, DeviceEmit, endHybridVoice, hybridVoiceStamp } from './HybridDeviceVoice'
import { requirePrivacy } from './PrivacyChoices'
import { requestHybridConsent } from './HybridConsent'

type Values = Record<string,Object>
let pickerOpen:boolean=false
let phase:string='idle'
let issue:string=''

export class HybridError extends Error {
  code:string
  constructor(code:string, message:string) { super(message); this.code=code }
}

function integerParam(params:Values, key:string, fallback:number, max:number):number {
  const value = params[key] ?? fallback
  if (typeof value!=='number'||!Number.isInteger(value)||value<1||value>max) throw new HybridError('invalid_request','请求的数量无效。')
  return value
}

function textParam(params:Values,key:string,limit:number):string {
  const value=params[key]
  if (typeof value!=='string'||!value.length||value.length>limit) throw new HybridError('invalid_request','内容为空或超出限制。')
  return value
}

function media(name:string, mime:string, base:string):Values {
  const pad=base.endsWith('==')?2:base.endsWith('=')?1:0
  return {'name':name,'media_type':mime,'data_base64':base,'size_bytes':Math.floor(base.length*3/4)-pad}
}

async function pick(ctx:common.UIAbilityContext, action:string, p:Values):Promise<Values> {
  if (pickerOpen) throw new HybridError('picker_busy','请先完成当前选择。')
  pickerOpen=true
  const owner=actorId()
  try {
    if (action==='native.pickFile') {
      integerParam(p,'max_count',1,1)
      if (p['purpose']!==undefined && p['purpose']!=='ai') throw new Error('文件用途无效。')
      const files=await chooseHybridDocument(ctx)
      if (actorId()!==owner) throw new HybridError('account_changed','账号已切换。')
      return {'files':files,'cancelled':files.length===0}
    }
    const count=integerParam(p,'max_count',1,p['purpose']==='ai'?2:4)
    const source=p['source']??'gallery'
    if(source!=='gallery'&&source!=='camera') throw new Error('请选择相册或相机。')
    let actualSource=String(source)
    let images:string[]=[]
    if(source==='camera'){
      try{images=await capturePhoto(ctx)}
      catch(e){
        if(!(e instanceof CameraUnavailable))throw e as Error
        if(actorId()!==owner)throw new HybridError('account_changed','账号已切换。')
        const useGallery=await offerCameraGallery(ctx,e.message)
        if(actorId()!==owner)throw new HybridError('account_changed','账号已切换。')
        if(useGallery)actualSource='gallery'
      }
    }
    if(actualSource==='gallery'){
      try{images=await pickPhotos(source==='camera'?1:count)}
      catch(e){
        if(!/cancel(?:l?ed)?|取消/i.test((e as Error)?.message||''))throw new HybridError('picker_unavailable','这次没有从系统相册取得照片。请回到相册重新选择。')
      }
    }
    if (actorId()!==owner) throw new HybridError('account_changed','账号已切换。')
    const output:Values[]=images.map((uri:string,index:number):Values=>{
      const comma=uri.indexOf(',')
      const mime=uri.slice(5,uri.indexOf(';'))
      const ext=mime==='image/png'?'png':mime==='image/webp'?'webp':'jpg'
      return media('凡思照片-'+(index+1)+'.'+ext,mime,uri.slice(comma+1))
    })
    const reply:Values={'images':output,'cancelled':output.length===0}
    if(output.length)reply['source']=actualSource
    return reply
  } finally {pickerOpen=false}
}

async function update(ctx:common.UIAbilityContext,action:string):Promise<Values> {
  const b=bundleManager.getBundleInfoForSelfSync(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT)
  try {
    if(action==='native.update.check') {phase=await checkMarketUpdate(ctx)==='available'?'available':'up_to_date';issue=''}
    else if(action==='native.update.start'||action==='native.update.install') {
      phase=await openMarketUpdate(ctx)==='opened'?'installer_opened':'up_to_date';issue=''
    }else if(action==='native.update.cancel'){phase='cancelled';issue=''}
    else if(action!=='native.update.status') throw new Error('未知的更新操作。')
  }catch(e){phase='error';issue=updateFailure(e as Error)}
  return {'phase':phase,'current_version_code':b.versionCode,'current_version_name':b.versionName,
    'downloaded_bytes':0,'total_bytes':0,'percent':0,
    'can_install':phase==='available','error_message':issue,'distribution':'huawei_appgallery'}
}

export async function hybridDevice(ctx:common.UIAbilityContext, action:string, p:Values, emit:DeviceEmit):Promise<Object|null> {
  requirePrivacy()
  if (action==='native.getInfo') {
    const info=bundleManager.getBundleInfoForSelfSync(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT)
    return {'platform':'HarmonyOS','version':info.versionName,'application_id':info.name,'offline_voice':false,'cloud_voice':true,
      'voice_processing_events':true,'max_voice_seconds':60,'max_image_bytes':2097152,'max_images':4,'max_document_bytes':5242880,
      'file_picker':true,'cloud_personal_backup':false} as Values
  }
  if (action==='native.pickImage'||action==='native.pickFile') return pick(ctx,action,p)
  if (action==='native.shareText') {
    const shared=await shareText(ctx,textParam(p,'title',120),textParam(p,'text',16000))
    return {'opened':true,'shared':shared} as Values
  }
  if (action==='native.copyText') {
    await pasteboard.getSystemPasteboard().setData(pasteboard.createData(pasteboard.MIMETYPE_TEXT_PLAIN,textParam(p,'text',16000)))
    return null
  }
  if (action==='native.openLink') {
    const raw=textParam(p,'url',4096)
    const link=new url.URL(raw)
    const host=link.hostname.toLowerCase()
    if (!['https:','http:'].includes(link.protocol)||link.username||link.password||!host.includes('.')||
      host.endsWith('.local')||host.endsWith('.localhost')||/^\d+[.\d]*$/.test(host)||host.includes(':')) {
      throw new HybridError('invalid_link','原文链接格式无效。')
    }
    await ctx.startAbility({action:'ohos.want.action.viewData',uri:raw,entities:['entity.system.browsable']})
    return null
  }
  if (action==='native.startVoice') {
    const stamp=hybridVoiceStamp(), owner=actorId()
    if (!await requestHybridConsent('ai')) throw new HybridError('consent_declined','语音转写尚未授权，可以直接输入文字。')
    if (!(await devicePermissionState(ctx)).microphone_granted) throw new HybridError('permission_denied','请在系统设置中允许凡思使用麦克风。')
    if(stamp!==hybridVoiceStamp()||owner!==actorId()) throw new HybridError('cancelled','录音操作已结束。')
    return beginHybridVoice(integerParam(p,'max_seconds',60,60),emit)
  }
  if (action==='native.stopVoice') return endHybridVoice(emit)
  if (action==='native.cancelVoice') {await abortHybridVoice();return {'text':'','cancelled':true} as Values}
  if (action.startsWith('native.update.')) return update(ctx,action)
  if (action==='native.permissions.request') {
    const kinds=p['kinds']
    if (!Array.isArray(kinds)) throw new HybridError('invalid_request','权限类型无效。')
    await explicitDevicePermission(ctx,kinds as string[])
  }
  if (action==='native.permissions.status'||action==='native.permissions.request') return devicePermissionState(ctx)
  if (action==='native.permissions.openSettings') {
    await abilityAccessCtrl.createAtManager().requestPermissionOnSetting(ctx,['ohos.permission.MICROPHONE','ohos.permission.READ_CALENDAR','ohos.permission.WRITE_CALENDAR'])
    return {'opened':true} as Values
  }
  if (action==='native.local149.notifications') {await notificationManager.openNotificationSettings(ctx);return {'requested':true} as Values}
  if (action==='native.local149.capabilities') {
    let scheduled=false
    try {scheduled=(await reminderAgentManager.getValidReminders()).length>0} catch (_) {}
    return {'notifications_enabled':await notificationManager.isNotificationEnabled(),'system_notification_settings':true,
      'scheduled_reminders_present':scheduled,'calendar_enabled':usesCalendarReminders(ctx),'exact_alarms_enabled':false} as Values
  }
  if (action==='native.calendar.status'||action==='native.calendar.set') {
    if(actorId()<=0)throw new HybridError('not_authenticated','请登录后再设置本机提醒。')
    if(action==='native.calendar.status')return {'enabled':usesCalendarReminders(ctx)} as Values
    if(typeof p['enabled']!=='boolean') throw new Error('请选择日历提醒设置。')
    await setCalendarReminders(ctx,p['enabled'] as boolean)
    return {'enabled':usesCalendarReminders(ctx)} as Values
  }
  if (action==='native.reminders.sync') {await syncDeviceReminders(ctx,false);return null}
  if (action==='native.local149.exactAlarms') throw new HybridError('feature_unavailable','鸿蒙通过系统提醒和日历投递，请在通知设置中允许提醒。')
  if (action==='native.exportBackup'||action==='native.importBackup') throw new HybridError('feature_unavailable','当前云服务未提供会员个人备份与恢复。')
  throw new HybridError('unsupported_action','当前版本不支持该设备操作。')
}
