import { bundleManager, common } from '@kit.AbilityKit'
import { updateManager } from '@kit.AppGalleryKit'
import type { BusinessError } from '@kit.BasicServicesKit'
import { requirePrivacy } from './PrivacyChoices'

export type UpdateStatus = 'checking' | 'available' | 'no-offer' | 'opening' | 'opened' | 'error'

export function installedVersion(): string {
  const info = bundleManager.getBundleInfoForSelfSync(bundleManager.BundleFlag.GET_BUNDLE_INFO_DEFAULT)
  return info.versionName + '（' + info.versionCode + '）'
}

// AppGallery compares the installed phone application with the eligible signed release.
// Huawei only returns versionName/versionCode in this response for atomic services.
export async function checkMarketUpdate(context: common.UIAbilityContext): Promise<UpdateStatus> {
  requirePrivacy()
  const result = await updateManager.checkAppUpdate(context)
  if (result.updateAvailable === updateManager.UpdateAvailableCode.LATER_VERSION_EXIST) return 'available'
  if (result.updateAvailable === updateManager.UpdateAvailableCode.LATER_VERSION_NOT_EXIST) return 'no-offer'
  throw new Error('应用市场返回了未知的版本状态。')
}

export async function openMarketUpdate(context: common.UIAbilityContext): Promise<UpdateStatus> {
  requirePrivacy()
  const result = await updateManager.showUpdateDialog(context)
  if (result === updateManager.ShowUpdateResultCode.SHOW_DIALOG_SUCCESS) return 'opened'
  throw new Error('更新窗口未能打开，请重新检查。')
}

export function updateFailure(error: Error): string {
  const code = (error as BusinessError).code
  switch (code) {
    case 1009400001: return '本机暂时无法连接华为应用市场更新服务。'
    case 1009400002: return '应用市场暂时无法处理这次查询，请稍后重试。'
    case 1009400003: return '无法连接应用市场，请检查网络后重试。'
    case 1009400004: return '请返回凡思前台后重新检查。'
    case 1009400005: return '请先打开华为应用市场，查看并确认其隐私设置，再返回检查。'
    case 1009400006: return '检查次数较多，请稍后再试。'
    default:
      if (error.message === '更新窗口未能打开，请重新检查。' || error.message === '应用市场返回了未知的版本状态。') {
        return error.message
      }
      return '暂时无法获得更新结果，请稍后重试。'
  }
}
