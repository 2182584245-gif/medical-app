import { common } from '@kit.AbilityKit'
import { camera, cameraPicker } from '@kit.CameraKit'
import { fileIo as fs, fileUri } from '@kit.CoreFileKit'
import { util } from '@kit.ArkTS'
import { window } from '@kit.ArkUI'
import { readPhoto } from './Cloud'

export class CameraUnavailable extends Error {
  constructor(message:string = '这次没有从系统相机取得照片。') { super(message) }
}

function cameraPosition(ctx:common.UIAbilityContext):camera.CameraPosition {
  let devices:camera.CameraDevice[]|undefined
  try { devices=camera.getCameraManager(ctx).getSupportedCameras() } catch (_) {}
  if(!devices)return camera.CameraPosition.CAMERA_POSITION_BACK
  if(!devices.length)throw new CameraUnavailable('当前设备没有可用的系统相机。')
  const back=devices.find((item:camera.CameraDevice)=>item.cameraPosition===camera.CameraPosition.CAMERA_POSITION_BACK)
  return (back||devices[0]).cameraPosition
}

export async function offerCameraGallery(ctx:common.UIAbilityContext, reason:string):Promise<boolean> {
  try {
    const screen=await window.getLastWindow(ctx)
    const answer=await screen.getUIContext().getPromptAction().showDialog({
      title:'拍照未完成', message:reason+'也可以从系统相册选择一张照片。',
      buttons:[{text:'取消',color:'#5D6570'},{text:'从相册选择',color:'#1B655C'}]
    })
    return answer?.index===1
  } catch(e) {
    if(/cancel(?:l?ed)?|取消/i.test((e as Error)?.message||''))return false
    throw new Error('这次没有取得照片。请回到凡思的记录或助手页面，再拍照或从相册选择。')
  }
}

export async function capturePhoto(ctx: common.UIAbilityContext): Promise<string[]> {
  const position=cameraPosition(ctx)
  const path = ctx.cacheDir + '/capture-' + util.generateRandomUUID(true) + '.jpg'
  try {
    const file = await fs.open(path, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE)
    await fs.close(file)
    const target = fileUri.getUriFromPath(path)
    const result = await cameraPicker.pick(ctx, [cameraPicker.PickerMediaType.PHOTO], {
      cameraPosition: position,
      saveUri: target
    })
    if(!result)throw new CameraUnavailable()
    // A dismissed camera commonly returns -1, with no URI. Never read the placeholder file on dismissal.
    if(result.resultCode===-1 && !result.resultUri)return []
    if(result.resultCode!==0 || result.resultUri!==target || result.mediaType!==cameraPicker.PickerMediaType.PHOTO)
      throw new CameraUnavailable()
    return [await readPhoto(result.resultUri)]
  } catch(e) {
    if(e instanceof CameraUnavailable)throw e
    if(/user (?:has )?cancel(?:l?ed)?|用户取消/i.test((e as Error)?.message||''))return []
    throw new CameraUnavailable()
  } finally {
    try { await fs.unlink(path) } catch {}
  }
}
