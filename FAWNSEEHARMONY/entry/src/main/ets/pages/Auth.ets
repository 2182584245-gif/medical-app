import { sendCode, signIn } from '../services/Cloud'
@Component
export struct AuthPage {
  @Prop route: string = 'login'
  onNavigate: (route:string)=>void = () => {}
  @State username: string = ''
  @State password: string = ''
  @State phone: string = ''
  @State code: string = ''
  @State terms: boolean = false
  @State autoLogin: boolean = false
  @State showPassword: boolean = false
  @State busy: boolean = false
  @State sending: boolean = false
  @State message: string = ''
  @State challenge: string = ''
  @State seconds: number = 0
  timer: number = -1
  aboutToDisappear() { clearInterval(this.timer) }
  phoneMode(): boolean { return this.route !== 'auth/login' }
  async send() {
    if (this.sending || this.seconds || this.busy) return
    if (!/^1[3-9]\d{9}$/.test(this.phone)) { this.message = '请输入有效的11位手机号。';return }
    this.sending=true
    try {
      const number=this.phone
      const key=await sendCode(number,'login')
      if(number!==this.phone)return
      this.challenge=key;this.seconds=60; this.message='验证码已发送。'
      clearInterval(this.timer);this.timer=setInterval(()=>{ if(--this.seconds<=0)clearInterval(this.timer) },1000)
    } catch(e) { this.message=(e as Error).message }
    finally {this.sending=false}
  }
  async submit() {
    if (this.busy) return
    if (!this.terms) {this.message='请先阅读并同意使用条款。';return}
    if (this.phoneMode() ? (!this.challenge || !/^\d{6}$/.test(this.code)) : (!this.username.trim() || !this.password)) {
      this.message=this.phoneMode()?'请先获取并填写六位短信验证码。':'请输入账号和密码。';return
    }
    this.busy=true;this.message=''
    try {
      if(this.phoneMode()) await signIn('/v1/auth/phone/quick-entry',{'phone':this.phone,'challenge_id':this.challenge,'code':this.code} as Record<string,string>,this.autoLogin)
      else await signIn('/v1/auth/login',{'username':this.username.trim(),'password':this.password} as Record<string,string>,this.autoLogin)
      this.password='';this.code='';this.onNavigate('signed-in')
    } catch(e) {this.message=(e as Error).message} finally {this.busy=false}
  }
  @Builder
  Options(){
    Row({space:5}){
      Checkbox().id('auth-terms').select(this.terms).width(17).height(17).selectedColor('#317C75').onChange((v:boolean)=>{this.terms=v})
      Text('我已阅读并同意使用条款').fontSize(13).fontColor('#435A4F').onClick(()=>{this.terms=!this.terms})
      Blank()
      Checkbox().select(this.autoLogin).width(17).height(17).selectedColor('#317C75').onChange((v:boolean)=>{this.autoLogin=v})
      Text('自动登录').fontSize(13).fontColor('#435A4F').onClick(()=>{this.autoLogin=!this.autoLogin})
    }.width('100%')
  }
  @Builder
  Action(text:string) {
    Button(this.busy?'正在登录…':text).id('auth-submit').width('100%').height(50).borderRadius(14)
      .backgroundColor('#34796F').fontColor(Color.White).fontSize(17).enabled(!this.busy)
      .onClick(()=>this.submit())
  }
  @Builder
  PasswordLogin() {
    Stack() {
      Image($r('app.media.auth_watercolor')).width('100%').height('100%').objectFit(ImageFit.Cover)
      Scroll(){
        Column({space:12}) {
          Image($r('app.media.records_logo')).width(96).height(96).objectFit(ImageFit.Contain)
          Text('●  会员端登录').fontSize(13).fontColor('#196E64').backgroundColor('#E2F3EA').borderRadius(16).padding({left:12,right:12,top:4,bottom:4})
          Text('欢迎回来').fontSize(32).fontColor('#30463A').fontFamily('serif')
          Column({space:12}) {
            Text('账号 *').width('100%').fontSize(15).fontColor('#30463A')
            TextInput({text:this.username,placeholder:'请输入账号'}).id('auth-account').height(48).maxLength(64).borderRadius(12).onChange((v:string)=>{this.username=v})
            Text('密码 *').width('100%').fontSize(15).fontColor('#30463A')
            TextInput({text:this.password,placeholder:'请输入密码'}).id('auth-password').height(48).type(InputType.Password).maxLength(256).borderRadius(12).onChange((v:string)=>{this.password=v})
            this.Options()
            Text('手机号登录或注册  ›').width('100%').textAlign(TextAlign.End).fontColor('#286F67').fontSize(15)
              .onClick(()=>{this.message='';this.onNavigate('auth/phone')})
            if(this.message){Text(this.message).fontSize(14).fontColor('#9A423A')}
            this.Action('登录  →')
          }.padding(20).backgroundColor('#EEFDFBF8').borderRadius(20).width('100%').margin({top:8})
        }.constraintSize({maxWidth:390}).width('100%').padding({left:20,right:20,top:34,bottom:28})
      }.width('100%').height('100%').align(Alignment.Top)
    }.width('100%').height('100%')
  }
  @Builder
  PhoneLogin(){
    Column(){
      Row({space:9}){
        Text('‹  继续体验').fontSize(14).fontColor('#426553').onClick(()=>this.onNavigate('guest'))
        Image($r('app.media.records_logo')).width(40).height(40).objectFit(ImageFit.Contain)
        Column(){Text('凡思').fontSize(17).fontWeight(700);Text('记录日常，照顾自己').fontSize(11)}.alignItems(HorizontalAlign.Start)
      }.width('100%').height(64).padding({left:16,right:16}).border({width:{bottom:1},color:'#E7E6DB'})
      Scroll(){
        Column({space:18}){
          Text('手机号登录或注册').fontSize(28).fontWeight(700).width('100%').fontColor('#2D443A')
          Column({space:12}){
            Text('手机号').width('100%').fontSize(16)
            TextInput({text:this.phone,placeholder:'请输入手机号'}).id('auth-phone').type(InputType.Number).maxLength(11).onChange((v:string)=>{if(v!==this.phone)this.challenge='';this.phone=v})
            Text('短信验证码').width('100%').fontSize(16)
            Row({space:8}){
              TextInput({text:this.code,placeholder:'请输入验证码'}).layoutWeight(1).maxLength(6).type(InputType.Number).onChange((v:string)=>{this.code=v})
              Button(this.seconds?this.seconds+'秒':this.sending?'发送中…':'发送验证码').fontSize(14).backgroundColor('#E1EFE8').fontColor('#286F67').enabled(!this.seconds&&!this.sending).onClick(()=>this.send())
            }
            this.Options()
            if(this.message){Text(this.message).fontSize(14).fontColor('#9A423A')}
            this.Action('验证并继续')
          }.width('100%').padding(18).backgroundColor('#FFFEFB').borderRadius(18).border({width:1,color:'#E6E8DF'})
          Text('账号/密码登录  ›').id('auth-use-password').fontSize(16).fontColor('#286F67').onClick(()=>{this.message='';this.onNavigate('auth/login')})
        }.width('100%').padding({top:28,left:20,right:20,bottom:30})
      }.layoutWeight(1).width('100%').align(Alignment.Top)
    }.height('100%').width('100%').backgroundColor('#FDFBF7')
  }
  build() { if(this.phoneMode()){this.PhoneLogin()}else{this.PasswordLogin()} }
}
