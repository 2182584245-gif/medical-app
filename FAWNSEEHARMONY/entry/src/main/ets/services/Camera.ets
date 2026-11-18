import { common } from '@kit.AbilityKit'
import { camera, cameraPicker } from '@kit.CameraKit'
import { fileIo as fs, fileUri } from '@kit.CoreFileKit'
import { util } from '@kit.ArkTS'
import { readPhoto } from './Cloud'

export async function capturePhoto(ctx: common.UIAbilityContext): Promise<string[]> {
  const path = ctx.cacheDir + '/capture-' + util.generateRandomUUID(true) + '.jpg'
  const file = await fs.open(path, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE)
  await fs.close(file)
  try {
    const target = fileUri.getUriFromPath(path)
    const result = await cameraPicker.pick(ctx, [cameraPicker.PickerMediaType.PHOTO], {
      cameraPosition: camera.CameraPosition.CAMERA_POSITION_BACK,
      saveUri: target
    })
    if (result.resultCode !== 0 || !result.resultUri) return []
    return [await readPhoto(result.resultUri)]
  } finally {
    try { await fs.unlink(path) } catch {}
  }
}
