import { registerAccount, sendCode, signIn } from '../services/Cloud'

const ink = '#30463A'
const teal = '#286F67'

@Component
export struct AuthPage {
  @Prop route: string = 'auth/phone'
  onNavigate: (route:string)=>void = () => {}
  @State username: string = ''
  @State password: string = ''
  @State confirmation: string = ''
  @State phone: string = ''
  @State code: string = ''
  @State terms: boolean = false
  @State autoLogin: boolean = false
  @State busy: boolean = false
  @State sending: boolean = false
  @State message: string = ''
  @State challenge: string = ''
  @State seconds: number = 0
  private timer: number = -1
  private active: boolean = true
  private submitEpoch: number = 0
  aboutToAppear():void {this.active=true}
  aboutToDisappear():void {this.active=false;this.submitEpoch++;clearInterval(this.timer)}
  private phoneMode():boolean {return this.route==='auth/phone'}
  private registering():boolean {return this.route==='auth/register'}
  private changeMode(next:string):void {
    if(this.busy||this.sending)return
    this.submitEpoch++;this.message='';this.password='';this.confirmation=''
    this.onNavigate(next)
  }
  private changePhone(value:string):void {
    if(this.phone!==value){this.challenge='';this.code='';this.seconds=0;clearInterval(this.timer)}
    this.phone=value
  }
  private async send():Promise<void> {
    if(this.sending||this.busy||this.seconds)return
    if(!this.terms){this.message='请先阅读并勾选隐私政策，再获取验证码。';return}
    if(!/^1[3-9]\d{9}$/.test(this.phone)){this.message='请输入有效的11位手机号。';return}
    const number=this.phone
    this.sending=true
    try {
      const challenge=await sendCode(number,'login')
      if(!this.active||!this.phoneMode()||number!==this.phone)return
      this.challenge=challenge;this.seconds=60;this.message='验证码已发送。'
      clearInterval(this.timer)
      this.timer=setInterval(()=>{if(--this.seconds<=0)clearInterval(this.timer)},1000)
    }catch(e){if(this.active&&number===this.phone)this.message=(e as Error).message}
    finally{this.sending=false}
  }
  private async submit():Promise<void> {
    if(this.busy||this.sending)return
    if(!this.terms){this.message='请先阅读并勾选隐私政策。';return}
    const phoneMode=this.phoneMode(), registration=this.registering()
    const name=this.username.trim()
    if(phoneMode){
      if(!this.challenge||!/^\d{6}$/.test(this.code)){this.message='请先获取并填写六位短信验证码。';return}
    }else{
      if(!name||!this.password){this.message='请输入账号和密码。';return}
      if(registration){
        if(Array.from(name.normalize('NFKC')).length>64){this.message='账号名最多64个字符。';return}
        const length=Array.from(this.password).length
        if(length<8||length>1024){this.message='密码长度应为8至1024个字符。';return}
        if(this.password!==this.confirmation){this.message='两次输入的密码不一致。';return}
      }
    }
    const epoch=++this.submitEpoch
    const password=this.password
    this.busy=true;this.message=''
    let created=false
    try{
      if(phoneMode){
        await signIn('/v1/auth/phone/quick-entry',{'phone':this.phone,'challenge_id':this.challenge,'code':this.code} as Record<string,string>,this.autoLogin)
      }else{
        if(registration){
          await registerAccount(name,password)
          created=true
          if(!this.active||this.submitEpoch!==epoch)return
        }
        await signIn('/v1/auth/login',{'username':name,'password':password} as Record<string,string>,this.autoLogin)
      }
      if(this.active&&this.submitEpoch===epoch){
        this.password='';this.confirmation='';this.code='';this.onNavigate('signed-in')
      }
    }catch(e){
      if(!this.active||this.submitEpoch!==epoch)return
      if(created){
        this.password='';this.confirmation='';this.onNavigate('auth/login')
        this.message='账号已创建，请用刚设置的密码登录。'
      }else{this.message=(e as Error).message}
    }finally{this.busy=false}
  }
  @Builder
  Options(){
    Row({space:4}){
      Checkbox().id('auth-terms').select(this.terms).width(17).height(17).selectedColor('#317C75').onChange((v:boolean)=>{this.terms=v})
      Text('我已阅读并同意').fontSize(13).fontColor('#435A4F').onClick(()=>{this.terms=!this.terms})
      Text('《隐私政策》').id('auth-read-privacy').fontSize(13).fontColor(teal).onClick(()=>this.onNavigate('privacy/document'))
      Blank()
      Checkbox().id('auth-auto-login').select(this.autoLogin).width(17).height(17).selectedColor('#317C75').onChange((v:boolean)=>{this.autoLogin=v})
      Text('自动登录').fontSize(13).fontColor('#435A4F').onClick(()=>{this.autoLogin=!this.autoLogin})
    }.width('100%')
  }
  @Builder
  PhoneFields(){
    Text('手机号 *').width('100%').fontSize(15).fontColor(ink)
    TextInput({text:this.phone,placeholder:'请输入手机号'}).id('auth-phone').type(InputType.Number)
      .height(48).maxLength(11).borderRadius(12).onChange((v:string)=>this.changePhone(v))
    Text('短信验证码 *').width('100%').fontSize(15).fontColor(ink)
    Row({space:8}){
      TextInput({text:this.code,placeholder:'请输入验证码'}).id('auth-code').height(48).layoutWeight(1)
        .type(InputType.Number).maxLength(6).borderRadius(12).onChange((v:string)=>{this.code=v})
      Button(this.seconds?this.seconds+'秒':this.sending?'发送中…':'发送验证码').id('auth-send-code').height(44)
        .fontSize(14).backgroundColor('#E1EFE8').fontColor(teal).enabled(!this.seconds&&!this.sending&&!this.busy)
        .onClick(()=>this.send())
    }
  }
  @Builder
  AccountFields(){
    Text('账号 *').width('100%').fontSize(15).fontColor(ink)
    TextInput({text:this.username,placeholder:this.registering()?'设置账号名（最多64个字符）':'请输入账号'}).id('auth-account')
      .height(48).borderRadius(12).onChange((v:string)=>{this.username=v})
    Text('密码 *').width('100%').fontSize(15).fontColor(ink)
    TextInput({text:this.password,placeholder:this.registering()?'设置密码（至少8个字符）':'请输入密码'}).id('auth-password')
      .height(48).type(InputType.Password).borderRadius(12).onChange((v:string)=>{this.password=v})
    if(this.registering()){
      Text('确认密码 *').width('100%').fontSize(15).fontColor(ink)
      TextInput({text:this.confirmation,placeholder:'请再次输入密码'}).id('auth-confirm-password')
        .height(48).type(InputType.Password).borderRadius(12).onChange((v:string)=>{this.confirmation=v})
    }
  }
  @Builder
  ModeLink(label:string,next:string,id:string){
    Text(label).id(id).fontColor(teal).fontSize(14).padding({top:5,bottom:5})
      .enabled(!this.busy&&!this.sending).onClick(()=>this.changeMode(next))
  }
  build(){
    Stack(){
      Image($r('app.media.auth_watercolor')).width('100%').height('100%').objectFit(ImageFit.Cover)
      Scroll(){
        Column({space:12}){
          Image($r('app.media.records_logo')).width(96).height(96).objectFit(ImageFit.Contain)
          Text(this.registering()?'●  会员端注册':'●  会员端登录').fontSize(13).fontColor('#196E64').backgroundColor('#E2F3EA')
            .borderRadius(16).padding({left:12,right:12,top:4,bottom:4})
          Text(this.registering()?'创建会员账号':'欢迎回来').fontSize(32).fontColor(ink).fontFamily('serif')
          Column({space:12}){
            if(this.phoneMode()){ this.PhoneFields() }
            else{ this.AccountFields() }
            this.Options()
            if(this.message){Text(this.message).id('auth-message').width('100%').fontSize(14).fontColor('#92473E')}
            Button(this.busy?'正在处理…':this.registering()?'注册并登录  →':this.phoneMode()?'验证并继续  →':'登录  →').id('auth-submit')
              .width('100%').height(50).borderRadius(14).backgroundColor('#34796F').fontColor(Color.White).fontSize(17)
              .enabled(!this.busy&&!this.sending).onClick(()=>this.submit())
            Flex({wrap:FlexWrap.Wrap,justifyContent:FlexAlign.SpaceBetween}){
              if(!this.phoneMode()){this.ModeLink('手机号登录或注册  ›','auth/phone','auth-use-phone')}
              if(this.route!=='auth/login'){this.ModeLink('账号密码登录  ›','auth/login','auth-use-password')}
              if(!this.registering()){this.ModeLink('账号密码注册  ›','auth/register','auth-register-account')}
            }.width('100%')
          }.padding(20).backgroundColor('#EEFDFBF8').borderRadius(20).width('100%').margin({top:8})
          Row({space:16}){
            this.ModeLink('继续体验','guest','auth-guest')
            Text('管理本机隐私选择').id('auth-privacy-settings').fontSize(14).fontColor(teal).onClick(()=>this.onNavigate('privacy/settings'))
          }
        }.constraintSize({maxWidth:390}).width('100%').padding({left:20,right:20,top:34,bottom:28})
      }.width('100%').height('100%').align(Alignment.Top)
    }.width('100%').height('100%')
  }
}
