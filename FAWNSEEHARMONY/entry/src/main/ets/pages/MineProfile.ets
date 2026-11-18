import {image} from '@kit.ImageKit';
import {util} from '@kit.ArkTS';
import {rpc,actorId,pickPhotos} from '../services/Cloud';
import {INK,PAPER,MUTED,WHITE,TEAL,Choice,TMField,TMLabel,TMPills,TMNote,TMButton,TMBanner,operationId} from './TodayShared';
export interface MemberProfile {
  user_id:number;display_name:string;ai_preferred_name?:string;gender?:string;age?:number|null;birth_date?:string|null;
  phone?:string;height_cm?:number|null;weight_kg?:number|null;personal_introduction?:string;
  emergency_contact_name?:string;emergency_contact_phone?:string;
}
export interface AccountPrefs {avatar_data?:string;}
export async function photoAvatar():Promise<string>{
  const picked=await pickPhotos(1);if(!picked.length)return '';
  const decoder=new util.Base64Helper();
  const raw=decoder.decodeSync(picked[0].slice(picked[0].indexOf(',')+1));
  const src=image.createImageSource(raw.buffer as ArrayBuffer);
  const packer=image.createImagePacker();
  try {
    const info=await src.getImageInfo();const edge=Math.min(info.size.width,info.size.height);
    const px=await src.createPixelMap({desiredRegion:{x:Math.floor((info.size.width-edge)/2),y:Math.floor((info.size.height-edge)/2),size:{width:edge,height:edge}},desiredSize:{width:Math.min(192,edge),height:Math.min(192,edge)}});
    try{
      const png=await packer.packToData(px,{format:'image/png',quality:95});
      return 'data:image/png;base64,'+decoder.encodeToStringSync(new Uint8Array(png));
    }finally{await px.release();}
  }finally{await packer.release();await src.release();}
}
@Component
export struct MineProfile {
  onNavigate:(r:string)=>void=()=>{};
  @State nameValue:string='';@State nickname:string='';@State gender:string='unspecified';@State age:string='';
  @State birthday:string='';@State phone:string='';@State heightValue:string='';@State weight:string='';
  @State intro:string='';@State emergency:string='';@State emergencyPhone:string='';@State avatar:string='';
  @State error:string='';@State busy:boolean=false;@State ready:boolean=false;
  private serial:number=0;
  aboutToAppear(){this.load();}
  aboutToDisappear(){this.serial++;}
  private async load():Promise<void>{
    const t=++this.serial;this.error='';this.ready=false;
    try{
      const group=await Promise.all([rpc<MemberProfile>('health','get_profile',[actorId()]),rpc<AccountPrefs>('preferences','get_preferences',[actorId()])]);const p=group[0];const prefs=group[1];if(t!==this.serial)return;
      this.nameValue=p.display_name||'';this.nickname=p.ai_preferred_name||'';this.gender=p.gender||'unspecified';this.age=p.age==null?'':String(p.age);
      this.birthday=p.birth_date||'';this.phone=p.phone||'';this.heightValue=p.height_cm==null?'':String(p.height_cm);this.weight=p.weight_kg==null?'':String(p.weight_kg);
      this.intro=p.personal_introduction||'';this.emergency=p.emergency_contact_name||'';this.emergencyPhone=p.emergency_contact_phone||'';this.avatar=prefs.avatar_data||'';this.ready=true;
    }catch(e){if(t===this.serial)this.error=(e as Error).message;}
  }
  private async changeAvatar():Promise<void>{if(this.busy)return;const t=this.serial;this.busy=true;try{
    const uri=await photoAvatar();if(!uri||t!==this.serial)return;
    const result=await rpc<AccountPrefs>('preferences','update_preferences',[actorId(),{'avatar_data':uri} as Record<string, Object|null>],undefined,operationId());
    if(t!==this.serial)return;if(result.avatar_data!==uri)throw Error('头像同步结果尚未确认。');this.avatar=uri;
  }catch(e){if(t===this.serial)this.error=(e as Error).message;}finally{this.busy=false;}}
  private async save():Promise<void>{
    if(this.busy||!this.ready)return;
    if(!this.nameValue.trim()){this.error='请填写姓名。';return;}
    if(this.age&&(!Number.isInteger(Number(this.age))||Number(this.age)<1||Number(this.age)>120)){this.error='年龄应为1至120岁的整数。';return;}
    const t=this.serial;this.error='';this.busy=true;
    try{
      await rpc<Object>('health','save_profile',[actorId(),{'display_name':this.nameValue.trim(),'ai_preferred_name':this.nickname.trim(),'gender':this.gender,'age':this.age?Number(this.age):null,'birth_date':this.birthday||null,'phone':this.phone.trim(),'height_cm':this.heightValue?Number(this.heightValue):null,'weight_kg':this.weight?Number(this.weight):null,'personal_introduction':this.intro.trim(),'emergency_contact_name':this.emergency.trim(),'emergency_contact_phone':this.emergencyPhone.trim()} as Record<string, Object|null>],undefined,operationId());
      if(t===this.serial)this.onNavigate('mine');
    }catch(e){if(t===this.serial)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  build(){Column({space:12}){
    TMBanner({title:'完善个人信息',art:$r('app.media.tm_habit_journal_try1')})
    Row({space:14}){
      Image(this.avatar||$r('app.media.records_logo')).objectFit(ImageFit.Cover).width(72).height(72).borderRadius(17)
      Column({space:5}){TMLabel({label:'个人头像'});TMNote({label:'点击头像更换照片'})}.layoutWeight(1)
    }.padding(15).width('100%').backgroundColor(WHITE).borderRadius(20).onClick(()=>this.changeAvatar())
    TMLabel({label:'基础信息'});TMNote({label:'* 为必填项'})
    TMField({label:'姓名 *',value:this.nameValue,change:(v:string)=>this.nameValue=v})
    TMField({label:'AI 称呼',value:this.nickname,change:(v:string)=>this.nickname=v})
    TMLabel({label:'性别'});TMPills({options:[new Choice('unspecified','未填写'),new Choice('female','女'),new Choice('male','男'),new Choice('other','其他')],selected:this.gender,change:(v:string)=>this.gender=v})
    TMField({label:'年龄（岁）',value:this.age,numeric:true,change:(v:string)=>this.age=v})
    TMField({label:'生日',value:this.birthday,placeholder:'YYYY-MM-DD',change:(v:string)=>this.birthday=v})
    TMField({label:'电话',value:this.phone,change:(v:string)=>this.phone=v})
    TMField({label:'身高（cm）',value:this.heightValue,numeric:true,change:(v:string)=>this.heightValue=v})
    TMField({label:'体重（kg）',value:this.weight,numeric:true,change:(v:string)=>this.weight=v})
    TMLabel({label:'生活与偏好'});TMField({label:'个人情况与生活习惯简介',value:this.intro,multiline:true,change:(v:string)=>this.intro=v})
    TMLabel({label:'安全联络'});TMField({label:'紧急联系人姓名',value:this.emergency,change:(v:string)=>this.emergency=v});TMField({label:'紧急联系人电话',value:this.emergencyPhone,change:(v:string)=>this.emergencyPhone=v})
    TMButton({label:'保存资料',primary:true,blocked:this.busy||!this.ready,action:()=>this.save()})
    TMButton({label:'取消修改',action:()=>this.onNavigate('mine')})
    if(this.error){TMNote({label:this.error});if(!this.ready){TMButton({label:'重新读取',action:()=>this.load()})}}
  }.width('100%').alignItems(HorizontalAlign.Start)}
}
