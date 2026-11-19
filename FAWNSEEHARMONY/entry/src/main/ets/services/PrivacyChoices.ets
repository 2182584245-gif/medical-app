import { common } from '@kit.AbilityKit'
import { preferences } from '@kit.ArkData'

const version = '2026-11-18'
let store: preferences.Preferences | undefined
let accepted = false
export type OptionalUse = 'ai' | 'health' | 'weather'

export function readPrivacy(context: common.UIAbilityContext): boolean {
  store = preferences.getPreferencesSync(context, { name: 'fawnsee_privacy' })
  accepted = store.getSync('accepted_version', '') === version
  return accepted
}

export function privacyAccepted(): boolean { return accepted }
export function requirePrivacy(): void {
  if (!accepted) throw new Error('请先阅读并同意凡思隐私政策。')
}

export async function acceptPrivacy(): Promise<void> {
  if (!store) throw new Error('无法读取本机隐私选择，请重新打开。')
  await store.put('accepted_version', version)
  await store.flush()
  accepted = true
}

function key(use: OptionalUse, owner: number): string {
  return version + '_' + use + '_' + owner
}

export function optionalAllowed(use: OptionalUse, owner: number): boolean {
  if (!accepted || !store) return false
  // AI and records are built-in features. Legacy per-purpose choices no longer gate them.
  if (use === 'ai' || use === 'health') return true
  return store.getSync(key(use, owner), false) === true
}

export async function setOptional(use: OptionalUse, owner: number, enabled: boolean): Promise<void> {
  requirePrivacy()
  if (!store) throw new Error('无法保存本机隐私选择。')
  await store.put(key(use, owner), enabled)
  await store.flush()
}
