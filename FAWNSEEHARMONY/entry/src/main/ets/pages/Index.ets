import { common } from '@kit.AbilityKit'
import { actorId, restoreSession } from '../services/Cloud'
import { cancelVoice } from '../services/Voice'
import { clearDeviceReminders, syncDeviceReminders } from '../services/DeviceReminders'
import { requestHybridPermissionsOnce } from '../services/HybridPermissions'
import { acceptPrivacy, optionalAllowed, privacyAccepted, readPrivacy, setOptional } from '../services/PrivacyChoices'
import { PrivacyDocument, PrivacyNotice } from './Privacy'
import { HybridWeb, hybridBack, hybridSessionExpired, hybridVisibility } from './HybridWeb'

@Entry
@Component
struct Index {
  @State phase: string = 'boot'
  @State documentOpen: boolean = false
  @State settingsOpen: boolean = false
  @State busy: boolean = false
  @State error: string = ''
  @State weatherEnabled: boolean = false
  @StorageLink('sessionExpired') @Watch('expired') expiredFlag: boolean = false

  private context(): common.UIAbilityContext {
    return this.getUIContext().getHostContext() as common.UIAbilityContext
  }

  aboutToAppear(): void {
    this.boot()
  }
  aboutToDisappear(): void {
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
  private async toggleWeather(): Promise<void> {
    if (this.busy) return
    this.busy = true
    const owner = actorId()
    try {
      await setOptional('weather', owner, !this.weatherEnabled)
      if (actorId() === owner) this.weatherEnabled = optionalAllowed('weather', owner)
    }
    catch (e) { this.error = (e as Error).message }
    finally { this.busy = false }
  }
  private openPrivacy(settings: boolean): void {
    if (settings) { this.weatherEnabled = optionalAllowed('weather', actorId()); this.error = ''; this.settingsOpen = true }
    else this.documentOpen = true
  }
  @Builder Settings() {
    Column({space:12}) {
      Button('返回').id('privacy-settings-back').onClick(()=>{this.settingsOpen=false})
      Text('隐私与天气').fontSize(26).fontWeight(FontWeight.Bold)
      Row() {
        Text('按城市显示天气').fontSize(17).layoutWeight(1)
        Button(this.weatherEnabled?'已开启':'未开启').id('privacy-setting-weather')
          .enabled(!this.busy).onClick(()=>this.toggleWeather())
      }.width('100%').padding({top:8,bottom:8})
      Text('天气默认关闭，开启后按网络所在城市显示预报。').fontSize(15).fontColor('#53685A')
      Button('阅读隐私政策').onClick(()=>{this.documentOpen=true})
      if (this.error) { Text(this.error).fontColor('#944639') }
    }.padding(22).width('100%').height('100%').backgroundColor('#FDFBF7').alignItems(HorizontalAlign.Start)
  }
  onBackPress(): boolean {
    if (this.documentOpen) { this.documentOpen = false; return true }
    if (this.settingsOpen) { this.settingsOpen = false; return true }
    return this.phase === 'web' && hybridBack()
  }
  build() {
    Stack() {
      if (this.phase === 'web') {
        HybridWeb({onPrivacy:(settings:boolean):void => this.openPrivacy(settings)})
          .visibility(this.documentOpen || this.settingsOpen ? Visibility.Hidden : Visibility.Visible)
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
      if (this.settingsOpen) { this.Settings() }
      if (this.documentOpen) { PrivacyDocument({onBack:()=>{this.documentOpen=false}}) }
    }.width('100%').height('100%').backgroundColor('#FDFBF7')
  }
}
