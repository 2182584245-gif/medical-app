import { abilityAccessCtrl, bundleManager, common, Permissions } from '@kit.AbilityKit'
import { notificationManager } from '@kit.NotificationKit'
import { preferences } from '@kit.ArkData'
import { requirePrivacy } from './PrivacyChoices'

const mic: Permissions = 'ohos.permission.MICROPHONE'
const calendar: Permissions[] = ['ohos.permission.READ_CALENDAR', 'ohos.permission.WRITE_CALENDAR']
let firstRequest: Promise<void> | undefined

function granted(permission: Permissions): boolean {
  const token = bundleManager.getBundleInfoForSelfSync(bundleManager.BundleFlag.GET_BUNDLE_INFO_WITH_APPLICATION).appInfo.accessTokenId
  return abilityAccessCtrl.createAtManager().checkAccessTokenSync(token, permission) === abilityAccessCtrl.GrantStatus.PERMISSION_GRANTED
}

async function requestGroup(ctx: common.UIAbilityContext, kind: string): Promise<void> {
  if (kind === 'notifications') {
    if (!await notificationManager.isNotificationEnabled()) await notificationManager.requestEnableNotification(ctx)
    return
  }
  const permissions: Permissions[] = kind === 'microphone' ? [mic] : calendar
  if (!permissions.every((p: Permissions) => granted(p))) {
    await abilityAccessCtrl.createAtManager().requestPermissionsFromUser(ctx, permissions)
  }
}

async function initial(ctx: common.UIAbilityContext): Promise<void> {
  requirePrivacy()
  const prefs = preferences.getPreferencesSync(ctx, { name: 'fawnsee_hybrid_permissions' })
  for (const group of ['notifications', 'microphone', 'calendar']) {
    if (prefs.getSync(group, false) === true) continue
    await prefs.put(group, true)
    await prefs.flush()
    try { await requestGroup(ctx, group) } catch (_) {}
  }
}

export function requestHybridPermissionsOnce(ctx: common.UIAbilityContext): Promise<void> {
  if (!firstRequest) firstRequest = initial(ctx).finally(() => { firstRequest = undefined })
  return firstRequest
}

export async function devicePermissionState(ctx: common.UIAbilityContext): Promise<Record<string, boolean>> {
  const prefs = preferences.getPreferencesSync(ctx, { name: 'fawnsee_hybrid_permissions' })
  return {
    'microphone_granted': granted(mic), 'microphone_requestable': prefs.getSync('microphone', false) !== true,
    'camera_granted': false, 'camera_delegated': true, 'camera_requestable': false,
    'notifications_enabled': await notificationManager.isNotificationEnabled(),
    'notifications_requestable': prefs.getSync('notifications', false) !== true,
    'notification_runtime_permission_required': true, 'calendar_granted': calendar.every((p: Permissions) => granted(p))
  }
}

export async function explicitDevicePermission(ctx: common.UIAbilityContext, kinds: string[]): Promise<void> {
  requirePrivacy()
  if (firstRequest) await firstRequest
  for (const group of ['notifications', 'microphone', 'calendar']) {
    if (kinds.includes(group)) await requestGroup(ctx, group)
  }
}
