import { webview } from '@kit.ArkWeb'
import { OptionalUse } from '../services/PrivacyChoices'

const ink = '#30463A'
const teal = '#286F67'
const page = '#FDFBF7'
export function useTitle(use: OptionalUse): string {
  if (use === 'weather') return '按城市显示天气'
  return use === 'ai' ? '使用智能助手' : '使用健康记录'
}
export function useDetail(use: OptionalUse): string {
  if (use === 'weather') return '开启后，ipwho.is 会根据本机网络出口 IP 估算城市，该服务可能在多个国家处理 IP。凡思会把估算坐标发送给挪威气象局 MET，其服务器会在奥斯陆记录 IP 和请求坐标。无需打开系统定位，您可以随时关闭。'
  if (use === 'ai') return '使用助手时，凡思云端会接收您发送的消息和选定的附件。DeepSeek 会处理问题和生成回答所需的资料。输入的餐照、身体指标或健康描述可能涉及敏感个人信息。开启生活资料参考时，还可能使用通义模型检索相关资料。'
  return '凡思会将您填写的健康记录、相关日期和选定的照片与账号关联，用于保存、同步和回看。身体指标、检查结果等属于敏感个人信息。您可以自行选择填写哪些记录。'
}
@Component
export struct PrivacyNotice {
  @Prop busy: boolean = false
  @Prop error: string = ''
  onRead: () => void = () => {}
  onAgree: () => void = () => {}
  onDecline: () => void = () => {}
  build() {
    Column() {
      Scroll() {
        Column({ space: 18 }) {
          Image($r('app.media.records_logo')).width(60).height(60)
          Text('欢迎使用凡思')
            .fontSize(29).fontWeight(FontWeight.Bold).fontColor(ink)
          Text('凡思由吴宇峰个人运营。开始使用前，您可以先在本机阅读我们如何处理登录信息、聊天、照片和健康记录。')
            .fontSize(17).lineHeight(27).fontColor(ink)
          Text('同意后才会连接凡思服务。健康记录和智能助手可直接使用；通知、麦克风、日历等权限由系统单独询问。')
            .fontSize(16).lineHeight(25).fontColor('#53685A')
          Text('阅读《凡思隐私政策》').id('privacy-read').fontColor(teal).fontSize(17)
            .fontWeight(FontWeight.Medium).padding({top:6,bottom:6}).onClick(this.onRead)
          if (this.error) { Text(this.error).fontColor('#944639').fontSize(14) }
        }.width('100%').alignItems(HorizontalAlign.Start).padding({left:24,right:24,top:30,bottom:20})
      }.layoutWeight(1).width('100%').align(Alignment.Top)
      Column({space:10}) {
        Button('同意并继续')
          .id('privacy-accept')
          .width('100%').height(48).backgroundColor(teal).enabled(!this.busy).onClick(this.onAgree)
        Button('不同意，退出应用').id('privacy-decline')
          .width('100%').height(44).backgroundColor('#EBF0E9').fontColor(ink).enabled(!this.busy).onClick(this.onDecline)
      }.width('100%').padding({left:24,right:24,top:8,bottom:16})
    }.width('100%').height('100%').backgroundColor(page)
  }
}

@Component
export struct PrivacyDocument {
  private controller: webview.WebviewController = new webview.WebviewController()
  onBack: () => void = () => {}
  build() {
    Column() {
      Row() {
        Button('返回').id('privacy-policy-back').backgroundColor('#E6EFE9').fontColor(teal).onClick(this.onBack)
        Text('凡思隐私政策').fontColor(ink).fontWeight(FontWeight.Bold).fontSize(20).margin({left:12})
      }.padding(12).width('100%')
      Web({src:$rawfile('fawnsee-privacy.html'), controller:this.controller})
        .id('privacy-policy-text').javaScriptAccess(false).blockNetwork(true).onlineImageAccess(false)
        .onLoadIntercept((event:OnLoadInterceptEvent):boolean=>/^https?:/i.test(event.data.getRequestUrl()))
        .fileAccess(false).domStorageAccess(false).width('100%').layoutWeight(1)
    }.width('100%').height('100%').backgroundColor(page)
  }
}
