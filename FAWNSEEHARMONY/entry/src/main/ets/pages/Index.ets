import { common } from '@kit.AbilityKit'
import { actorId, restoreSession } from '../services/Cloud'
import { cancelVoice } from '../services/Voice'
import { clearDeviceReminders, syncDeviceReminders } from '../services/DeviceReminders'
import { registerHybridConsent } from '../services/HybridConsent'
import { requestHybridPermissionsOnce } from '../services/HybridPermissions'
import { acceptPrivacy, optionalAllowed, OptionalUse, privacyAccepted, readPrivacy, setOptional } from '../services/PrivacyChoices'
import { PrivacyDocument, PrivacyNotice, useDetail, useTitle } from './Privacy'
import { HybridWeb, hybridBack, hybridSessionExpired, hybridVisibility } from './HybridWeb'

@Entry
@Component
struct Index {
  @State phase: string = 'boot'
  @State documentOpen: boolean = false
  @State settingsOpen: boolean = false
  @State use: OptionalUse = 'ai'
  @State promptOpen: boolean = false
  @State busy: boolean = false
  @State error: string = ''
  @State choices: boolean[] = [false, false, false]
  @StorageLink('sessionExpired') @Watch('expired') expiredFlag: boolean = false
  private owner: number = 0
  private settle?: (allowed: boolean) => void

  private context(): common.UIAbilityContext {
    return this.getUIContext().getHostContext() as common.UIAbilityContext
  }

  aboutToAppear(): void {
    registerHybridConsent((use: OptionalUse, owner: number) => this.askUse(use, owner))
    this.boot()
  }
  aboutToDisappear(): void {
    registerHybridConsent()
    this.finish(false)
    cancelVoice().catch(() => {})
  }

  private async boot(): Promise<void> {
    this.error = ''
    try {
      if (!readPrivacy(this.context())) { this.phase = 'welcome'; return }
      this.phase = 'boot'
      await requestHybridPermissionsOnce(this.context())
      const member = await restoreSession()
      this.phase = 'web'
      if (member) this.syncReminders()
      else clearDeviceReminders(this.context()).catch(() => {})
    } catch (e) { this.error = (e as Error).message }
  }

  private syncReminders(): void {
    if (privacyAccepted() && actorId() && optionalAllowed('health', actorId())) {
      syncDeviceReminders(this.context(), false).catch(() => {})
    }
  }
  onPageShow(): void { hybridVisibility(true); if (this.phase === 'web') this.syncReminders() }
  onPageHide(): void { hybridVisibility(false) }
  expired(): void {
    if (!this.expiredFlag) return
    this.expiredFlag = false
    this.finish(false)
    if (this.phase === 'web') {
      this.settingsOpen = false
      clearDeviceReminders(this.context()).catch(() => {})
      hybridSessionExpired()
    }
  }
  private async agreeWelcome(): Promise<void> {
    if (this.busy) return
    this.busy = true
    try { await acceptPrivacy(); await this.boot() }
    catch (e) { this.error = (e as Error).message }
    finally { this.busy = false }
  }
  private askUse(use: OptionalUse, owner: number): Promise<boolean> {
    this.use = use
    this.owner = owner
    this.error = ''
    this.promptOpen = true
    return new Promise<boolean>((resolve) => { this.settle = resolve })
  }
  private finish(allowed: boolean): void {
    const resolve = this.settle
    this.settle = undefined
    this.promptOpen = false
    if (resolve) resolve(allowed)
  }
  private async saveChoice(use: OptionalUse, owner: number, enabled: boolean): Promise<boolean> {
    if (actorId() !== owner) return false
    await setOptional(use, owner, enabled)
    if (actorId() !== owner) return false
    this.refreshChoices()
    if (use === 'health' && owner) {
      if (enabled) this.syncReminders()
      else clearDeviceReminders(this.context()).catch(() => {})
    }
    return true
  }
  private async agreeUse(): Promise<void> {
    if (this.busy) return
    this.busy = true
    try { this.finish(await this.saveChoice(this.use, this.owner, true)) }
    catch (e) { this.error = (e as Error).message }
    finally { this.busy = false }
  }
  private refreshChoices(): void {
    this.choices = [optionalAllowed('health', actorId()), optionalAllowed('ai', actorId()), optionalAllowed('weather', actorId())]
  }
  private openPrivacy(settings: boolean): void {
    if (settings) { this.refreshChoices(); this.error = ''; this.settingsOpen = true }
    else this.documentOpen = true
  }
  private toggle(use: OptionalUse): void {
    const owner = actorId()
    const enabled = optionalAllowed(use, owner)
    this.getUIContext().showAlertDialog({title:useTitle(use), message:enabled?'关闭后，下次使用这项功能时您可以重新选择。':useDetail(use),
      primaryButton:{value:'取消',action:()=>{}}, secondaryButton:{value:enabled?'关闭':'同意开启',action:()=>{
        this.saveChoice(use, owner, !enabled).catch((e: Error) => { this.error = e.message })
      }}})
  }
  @Builder Setting(use: OptionalUse, index: number) {
    Row() {
      Text(useTitle(use)).fontSize(17).layoutWeight(1)
      Button(this.choices[index]?'已开启':'未开启').id('privacy-setting-'+use).onClick(()=>this.toggle(use))
    }.width('100%').padding({top:8,bottom:8})
  }
  @Builder Settings() {
    Column({space:12}) {
      Button('返回').id('privacy-settings-back').onClick(()=>{this.settingsOpen=false})
      Text('隐私选择').fontSize(26).fontWeight(FontWeight.Bold)
      this.Setting('health', 0)
      this.Setting('ai', 1)
      this.Setting('weather', 2)
      Button('阅读隐私政策').onClick(()=>{this.documentOpen=true})
      if (this.error) { Text(this.error).fontColor('#944639') }
    }.padding(22).width('100%').height('100%').backgroundColor('#FDFBF7').alignItems(HorizontalAlign.Start)
  }
  onBackPress(): boolean {
    if (this.documentOpen) { this.documentOpen = false; return true }
    if (this.settingsOpen) { this.settingsOpen = false; return true }
    if (this.promptOpen) { this.finish(false); return true }
    return this.phase === 'web' && hybridBack()
  }
  build() {
    Stack() {
      if (this.phase === 'web') {
        HybridWeb({onPrivacy:(settings:boolean):void => this.openPrivacy(settings)})
          .visibility(this.documentOpen || this.settingsOpen || this.promptOpen ? Visibility.Hidden : Visibility.Visible)
      } else if (this.phase === 'welcome') {
        PrivacyNotice({busy:this.busy,error:this.error,onRead:()=>{this.documentOpen=true},onAgree:()=>{this.agreeWelcome()},
          onDecline:()=>{this.context().terminateSelf().catch(()=>{this.error='请使用系统返回键退出。'})}})
      } else {
        Column({space:16}) {
          Image($r('app.media.records_logo')).height(78).width(78)
          if (this.error) { Text(this.error); Button('重试').onClick(()=>this.boot()) }
          else { LoadingProgress().width(28) }
        }
      }
      if (this.promptOpen) {
        PrivacyNotice({kind:'optional',use:this.use,busy:this.busy,error:this.error,onRead:()=>{this.documentOpen=true},
          onAgree:()=>{this.agreeUse()},onDecline:()=>this.finish(false)})
      }
      if (this.settingsOpen) { this.Settings() }
      if (this.documentOpen) { PrivacyDocument({onBack:()=>{this.documentOpen=false}}) }
    }.width('100%').height('100%').backgroundColor('#FDFBF7')
  }
}