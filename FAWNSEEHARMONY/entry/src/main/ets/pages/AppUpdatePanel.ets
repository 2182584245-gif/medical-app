import { common } from '@kit.AbilityKit'
import { checkMarketUpdate, installedVersion, openMarketUpdate, updateFailure, UpdateStatus } from '../services/AppUpdate'

@Component
export struct AppUpdatePanel {
  @State private status: UpdateStatus = 'checking'
  @State private detail: string = ''
  @State private version: string = ''
  private active: boolean = false
  private busy: boolean = false

  aboutToAppear(): void {
    this.active = true
    try { this.version = installedVersion() } catch (_) { this.version = '' }
    this.run(false)
  }

  aboutToDisappear(): void { this.active = false }

  private async run(open: boolean): Promise<void> {
    if (this.busy || (open && this.status !== 'available')) return
    this.busy = true
    this.status = open ? 'opening' : 'checking'
    this.detail = ''
    try {
      const context = this.getUIContext().getHostContext() as common.UIAbilityContext
      const status = open ? await openMarketUpdate(context) : await checkMarketUpdate(context)
      if (this.active) this.status = status
    } catch (e) {
      if (this.active) {
        this.status = 'error'
        this.detail = updateFailure(e as Error)
      }
    } finally {
      this.busy = false
    }
  }

  private message(): string {
    switch (this.status) {
      case 'checking': return '正在向华为应用市场检查更新…'
      case 'available': return '有适用于本机的新版本。'
      case 'no-offer': return '华为应用市场暂未提供适用于本机的更高版本。'
      case 'opening': return '正在打开系统更新窗口…'
      case 'opened': return '已打开系统更新窗口。安装完成后可重新查看版本。'
      default: return this.detail
    }
  }

  build() {
    Column({space: 16}) {
      Image($r('app.media.records_logo')).width(64).height(64).objectFit(ImageFit.Contain)
      Text('凡思').fontSize(23).fontWeight(FontWeight.Bold).fontColor('#28473E')
      if (this.version) {
        Text('当前版本 ' + this.version).id('app-update-version').fontSize(16).fontColor('#61756C')
      }
      Text(this.message()).id('app-update-status').fontSize(17).lineHeight(25).fontColor('#344C42').textAlign(TextAlign.Center)
      if (this.status === 'checking' || this.status === 'opening') {
        LoadingProgress().width(27).height(27).color('#287C77')
      } else {
        Button(this.status === 'available' ? '前往更新' : '重新检查', {type: ButtonType.Normal})
          .id('app-update-action').height(46).padding({left: 28, right: 28}).borderRadius(14)
          .fontSize(17).fontColor(Color.White).backgroundColor('#287C77')
          .onClick(() => this.run(this.status === 'available'))
      }
    }.width('100%').padding({left: 24, right: 24, top: 36, bottom: 24})
  }
}
