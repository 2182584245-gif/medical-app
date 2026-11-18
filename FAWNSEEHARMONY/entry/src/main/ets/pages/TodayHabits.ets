import {common} from '@kit.AbilityKit';
import {syncDeviceReminders,ReminderScheduleError,reviewCalendarReminder,clampsMonthlyDate,setCalendarReminders,usesCalendarReminders} from '../services/DeviceReminders';
import { actorId,rpc } from '../services/Cloud';
import {PAPER,INK,MUTED,TEAL,WHITE,GREEN,DAYS,PageResult,Choice,dayAt,weekday,expansion,operationId,pad,TMHeader,TMBanner,TMField,TMButton,TMNote,TMLabel,TMPills} from './TodayShared';

interface Entry { day:string; }
interface Progress { today_checked:boolean; checkin_count:number; }
export interface Goal {id:number; title:string; status:string; version:number; kind:string; frequency:string; created_at?:string; reminder_time?:string; target?:number; unit?:string; entries:Entry[]; progress:Progress;}
export interface Reminder { id:number; title:string; scheduled_at:string; reminder_type:string; repeat_rule?:string; status:string; }
const frequency:Choice[]=[new Choice('daily','每天'),new Choice('weekly','每周')];
const repeats:Choice[]=[new Choice('none','仅一次'),new Choice('daily','每天'),new Choice('weekly','每周'),new Choice('monthly','每月')];
@Component
export struct TodayHabits {
  @Prop @Watch('changed') route:string='';
  onNavigate:(route:string)=>void=()=>{};
  @State goals:Goal[]=[];
  @State reminders:Reminder[]=[];
  @State selectedGoal:Goal|null=null;
  @State selectedReminder:Reminder|null=null;
  @State archived:boolean=false;
  @State busy:boolean=false;
  @State error:string='';
  @State calendarReason:string='';
  @State calendarStatus:string='';
  @State calendarOpen:boolean=false;
  @State calendarAuto:boolean=false;
  @State nameValue:string='';
  @State pickedTime:string='';
  @State rule:string='daily';
  @State pickedDay:string=dayAt();
  private serial:number=0;
  private savedKey:string='';
  private savedOp:string='';
  private getBase():string {return this.route.startsWith('today/goals')?'today/goals':'today/reminders';}
  private getGoalMode():boolean {return this.getBase()==='today/goals';}
  private getForm():boolean {return this.route.endsWith('/new')||this.route.endsWith('/edit');}
  private getEntityId():number {return Number(this.route.split('/')[2])||0;}
  aboutToAppear(){this.changed();}
  aboutToDisappear(){this.serial++;}
  changed():void{this.calendarReason='';this.calendarStatus='';this.calendarOpen=false;this.savedKey='';this.savedOp='';this.error='';this.nameValue='';this.pickedTime=this.getGoalMode()?'':new Date(Date.now()+28800000).toISOString().slice(11,16);this.rule=this.getGoalMode()?'daily':'none';this.pickedDay=dayAt();this.selectedGoal=null;this.selectedReminder=null;this.load(++this.serial);}
  private async load(token:number):Promise<void>{
    try{
      if(this.getGoalMode()){
        if(this.getEntityId()){
          const row=await expansion<Goal>('get_goal',{'goal_id':this.getEntityId()} as Record<string, Object|null>); if(token!==this.serial)return;
          this.selectedGoal=row;this.nameValue=row.title;this.pickedTime=row.reminder_time||'';this.rule=row.frequency;
        }else if(!this.getForm()){
          let offset=0;let rows:Goal[]=[];
          while(true){const page=await expansion<PageResult<Goal>>('list_goals',{'limit':100,'offset':offset} as Record<string, Object|null>);rows=rows.concat(page.items);offset+=page.items.length;if(token!==this.serial)return;if(offset>=page.total||!page.items.length)break;}
          this.goals=rows;
        }
      }else{
        const rows=await rpc<Reminder[]>('health','list_reminders',[actorId()]);if(token!==this.serial)return;
        this.reminders=rows;
        if(this.getEntityId()){
          const row=rows.find((r:Reminder)=>r.id===this.getEntityId());if(!row)throw Error('提醒已不存在。');
          this.selectedReminder=row;this.nameValue=row.title;this.rule=row.repeat_rule||'none';
          const bj=new Date(new Date(row.scheduled_at).getTime()+28800000).toISOString();
          this.pickedDay=bj.slice(0,10);this.pickedTime=bj.slice(11,16);
        }
      }
      if(token===this.serial)await this.syncNotifications(token,false);
    }catch(e){if(token===this.serial)this.error=(e as Error).message;}
  }
  private async syncNotifications(token:number,afterSave:boolean):Promise<void>{
    const context=this.getUIContext().getHostContext() as common.UIAbilityContext;
    this.calendarAuto=usesCalendarReminders(context);
    try{
      await syncDeviceReminders(context,afterSave);
      if(token===this.serial)this.calendarReason='';
    }catch(e){if(token===this.serial){
      const detail=(e as Error).message||'请在「我的 → 隐私与安全中心」开启系统通知。';
      if(e instanceof ReminderScheduleError)this.calendarReason=detail;
      else this.error=detail;
      if(afterSave)this.getUIContext().getPromptAction().showToast({message:'云端已保存。'+detail,duration:3500});
    }}
  }
  private async changeCalendarMode(enabled:boolean):Promise<void>{
    if(this.calendarOpen)return;
    const context=this.getUIContext().getHostContext() as common.UIAbilityContext;
    const token=this.serial;
    this.calendarOpen=true;this.calendarAuto=enabled;this.calendarStatus='';
    try{
      await setCalendarReminders(context,enabled);
      if(token===this.serial){this.calendarReason='';this.calendarStatus=enabled?'已同步到本机日历。':'本机日历中的自动提醒已清除。';}
    }catch(e){if(token===this.serial)this.calendarStatus=(e as Error).message;}
    finally{if(token===this.serial){this.calendarAuto=usesCalendarReminders(context);this.calendarOpen=false;}}
  }
  @Builder calendarControl(){
    Column({space:6}){
      Row({space:8}){
        Text('系统日历自动提醒').fontSize(17).fontColor(INK).layoutWeight(1)
        Toggle({type:ToggleType.Switch,isOn:this.calendarAuto}).id('calendar-reminder-mode').enabled(!this.calendarOpen&&!this.busy)
          .onChange((value:boolean)=>{if(value!==this.calendarAuto)this.changeCalendarMode(value);})
      }.width('100%')
      TMNote({label:this.calendarAuto?'提醒由系统「日历」发送。请在系统设置的「通知和状态栏 → 日历」允许通知；需要锁屏可见时，请同时开启锁屏通知。':'授权后，生活提醒、目标和每日记录提示会写入本机的「凡思生活提醒」日历。到点接收提醒需允许系统「日历」发送通知。'})
      if(this.calendarStatus){TMNote({label:this.calendarStatus})}
    }.width('100%').padding(14).backgroundColor(WHITE).borderRadius(18)
  }
  private async calendar(title:string,start:number,repeat:string):Promise<void>{
    if(this.calendarOpen)return;
    this.calendarOpen=true;const token=this.serial;
    try{
      const saved=await reviewCalendarReminder(this.getUIContext().getHostContext() as common.UIAbilityContext,title,start,repeat);
      if(token===this.serial)this.calendarStatus=saved?'已添加到系统日历。后续如需修改这份日历提醒，请在系统日历中操作。':'已取消添加到系统日历。';
    }catch(e){if(token===this.serial)this.calendarStatus=(e as Error).message||'暂时无法打开系统日历。';}
    finally{if(token===this.serial)this.calendarOpen=false;}
  }
  @Builder calendarOption(title:string,start:number,repeat:string){
    if(this.calendarReason&&!this.calendarAuto){
      TMNote({label:this.calendarReason});
      if(clampsMonthlyDate(start,repeat)){TMNote({label:'这条按月提醒会在短月取月底，系统日历本次仅添加下一次提醒。'})}
      TMButton({label:'添加到系统日历',blocked:this.calendarOpen,action:()=>this.calendar(title,start,repeat)});
      if(this.calendarStatus){TMNote({label:this.calendarStatus})}
    }
  }
  private async write(key:string,work:(op:string)=>Promise<Object|null>,next:string=''):Promise<void>{
    if(this.busy)return;
    this.busy=true;this.error='';
    if(this.savedKey!==key){this.savedKey=key;this.savedOp=operationId();}
    const token=this.serial;
    try{await work(this.savedOp);if(token!==this.serial)return;this.savedKey='';this.savedOp='';
      if(next){await this.syncNotifications(token,true);if(this.serial===token)this.onNavigate(next);}else {await this.syncNotifications(token,true);if(this.serial===token)await this.load(token);}
    }catch(e){if(token===this.serial)this.error=(e as Error).message;}finally{this.busy=false;}
  }
  private save():void{
    const title=this.nameValue.trim();
    if(!title){this.error=this.getGoalMode()?'请填写目标名称。':'请填写提醒内容。';return;}
    const key=[this.route,title,this.pickedTime,this.pickedDay,this.rule].join('|');
    if(this.getGoalMode()){
      const old=this.selectedGoal; const changes={'title':title,'target':old?.target??null,'unit':old?.unit||null,'frequency':this.rule,'reminder_time':this.pickedTime||null} as Record<string, Object|null>;
      this.write(key,(id:string)=> old?expansion<Goal>('update_goal',{'goal_id':old.id,'base_version':old.version,'changes':changes} as Record<string, Object|null>,id):expansion<Goal>('create_goal',{'title':title,'kind':'custom','target':null,'unit':null,'frequency':this.rule,'reminder_time':this.pickedTime||null} as Record<string, Object|null>,id),this.getBase());
    }else{
      const old=this.selectedReminder;let date=this.pickedDay;
      if(!old && date===dayAt() && this.pickedTime<=new Date(Date.now()+28800000).toISOString().slice(11,16))date=dayAt(1);
      const fields={'title':title,'scheduled_at':date+'T'+this.pickedTime+':00+08:00','reminder_type':old?.reminder_type||'custom','repeat_rule':this.rule} as Record<string, Object|null>;
      this.write(key,(id:string)=>rpc<Object>('health',old?'update_reminder':'add_reminder',old?[actorId(),old.id]:[actorId()],fields,id),this.getBase());
    }
  }
  private confirm(title:string,details:string,act:()=>void):void{
    this.getUIContext().showAlertDialog({title:title,message:details,primaryButton:{value:'取消',action:()=>{}},secondaryButton:{value:'确认',action:act}});
  }
  private checkin(g:Goal,date:string):void{
    const undo=g.entries.some((e:Entry)=>e.day===date);
    const task=()=>this.write((undo?'undo':'check')+g.id+date,(op:string)=>expansion<Goal>(undo?'uncheckin_goal':'checkin_goal',{'goal_id':g.id,'day':date} as Record<string, Object|null>,op));
    if(undo||date!==dayAt())this.confirm(undo?'撤销这天的打卡？':'补记这一天？',g.title+' · '+date,task);else task();
  }
  private pickTime():void{
    const selected=new Date();if(this.pickedTime){const t=this.pickedTime.split(':');selected.setHours(Number(t[0]),Number(t[1]));}
    this.getUIContext().showTimePickerDialog({selected:selected,useMilitaryTime:true,onAccept:(x:TimePickerResult)=>this.pickedTime=pad(x.hour??0)+':'+pad(x.minute??0)});
  }
  private title():string {if(this.getForm())return this.getGoalMode()?(this.getEntityId()?'调整生活目标':'新建'):(this.getEntityId()?'编辑生活提醒':'新建生活提醒');return this.getGoalMode()?(this.getEntityId()?'目标详情':'目标与习惯'):'提醒中心';}
  @Builder formView(){
    TMBanner({title:this.getGoalMode()?'设定温和持续的日常节奏':'设置生活提醒',art:this.getGoalMode()?$r('app.media.tm_today_habits'):$r('app.media.tm_today_reminders')})
    if(this.getGoalMode()){
      TMField({label:'目标名称 *',fieldId:'goal-title',value:this.nameValue,placeholder:'为自己留一个容易开始的小目标',change:(v:string)=>this.nameValue=v})
      TMLabel({label:'频率'})
      TMPills({options:frequency,selected:this.rule,change:(v:string)=>this.rule=v})
      TMLabel({label:'提醒时间'})
      TMButton({label:this.pickedTime||'选择时间',action:()=>this.pickTime()})
      if(this.pickedTime){Text('移除提醒时间').fontColor(TEAL).onClick(()=>this.pickedTime='')}
    }else{
      TMLabel({label:'提醒时刻 *'})
      TMButton({label:this.pickedTime,action:()=>this.pickTime()})
      TMField({label:'提醒内容 *',fieldId:'reminder-title',value:this.nameValue,placeholder:'提醒自己做什么？',change:(v:string)=>this.nameValue=v})
      TMLabel({label:'提醒频率'})
      TMPills({options:repeats,selected:this.rule,change:(v:string)=>this.rule=v})
    }
    TMButton({label:'保存',primary:true,blocked:this.busy||!!this.getEntityId()&&!(this.selectedGoal||this.selectedReminder),action:()=>this.save()})
    TMButton({label:'取消',action:()=>this.onNavigate(this.getBase())})
    if(this.selectedReminder&&this.selectedReminder.status==='active'){this.calendarOption(this.selectedReminder.title,Date.parse(this.selectedReminder.scheduled_at),this.selectedReminder.repeat_rule||'none')}
    if(this.selectedReminder){TMButton({label:'删除提醒',danger:true,action:()=>this.confirm('删除这个提醒？','删除后不再显示该提醒。',()=>this.write('delete'+this.getEntityId(),(op:string)=>rpc<Object>('health','delete_reminders',[actorId(),[this.getEntityId()]],undefined,op),this.getBase()))})}
  }
  @Builder week(g:Goal|null){
    Row({space:4}){ForEach(DAYS,(n:number)=>{
      Column({space:6}){
        Text(weekday(n)).fontSize(15).fontColor(MUTED)
        Text(dayAt(n).slice(8)).fontSize(18).fontColor(INK)
      }.layoutWeight(1).padding({top:8,bottom:8}).borderRadius(12).backgroundColor(g?.entries.some((e:Entry)=>e.day===dayAt(n))?'#B5E4D3':n===0?GREEN:WHITE)
        .onClick(()=>{if(g&&g.status==='active')this.checkin(g,dayAt(n));else this.pickedDay=dayAt(n);})
    },(n:number)=>String(n))}
  }
  @Builder detail(g:Goal){
    TMBanner({title:g.title,subtitle:g.status==='active'?'进行中':'已归档',art:$r('app.media.tm_today_habits')})
    TMNote({label:'执行频率　'+(g.frequency==='weekly'?'每周':'每天')+'\n提醒　'+(g.reminder_time||'未设置')})
    if(g.status==='active'&&g.reminder_time){this.calendarOption(g.title,Date.parse((g.created_at?new Date(Date.parse(g.created_at)+28800000).toISOString().slice(0,10):dayAt())+'T'+g.reminder_time.slice(0,5)+':00+08:00'),g.frequency==='weekly'?'weekly':'daily')}
    TMLabel({label:'今日状态'})
    if(g.status==='active'){TMButton({label:g.progress.today_checked?'撤销今日打卡':'今日打卡',primary:true,blocked:this.busy,action:()=>this.checkin(g,dayAt())})}
    TMLabel({label:'打卡日历'})
    this.week(g)
    Button('选择其他日期').onClick(()=>this.getUIContext().showDatePickerDialog({selected:new Date(),end:new Date(),onDateAccept:(d:Date)=>{if(g.status==='active')this.checkin(g,d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()));}}))
    TMNote({label:'累计打卡 '+g.entries.length+' 天'})
    ForEach(g.entries,(e:Entry)=>{Text(e.day+'　已打卡').fontColor(INK)},(e:Entry)=>e.day)
    if(g.status==='active'){
      TMButton({label:'调整目标',action:()=>this.onNavigate(this.getBase()+'/'+g.id+'/edit')})
      TMButton({label:'归档目标',action:()=>this.confirm('归档这个目标？','保留打卡历史，关联提醒将暂停。',()=>this.write('archive'+g.id,(op:string)=>expansion<Object>('close_goal',{'goal_id':g.id,'base_version':g.version} as Record<string, Object|null>,op),this.getBase()))})
    }
    TMButton({label:'删除目标',danger:true,action:()=>this.confirm('删除这个目标？','该目标和打卡历史将永久删除。',()=>this.write('delete'+g.id,(op:string)=>expansion<Object>('delete_goal',{'goal_id':g.id,'base_version':g.version} as Record<string, Object|null>,op),this.getBase()))})
  }
  @Builder goalsView(){
    TMBanner({title:'FAWNSEE 凡思 · 凡思生活韵律',subtitle:'在舒缓节奏中，守候每一天细微生机',art:$r('app.media.tm_today_habits')})
    TMLabel({label:dayAt().slice(0,4)+' 年 '+Number(dayAt().slice(5,7))+' 月'})
    this.week(null)
    TMPills({options:[new Choice('active','进行中 '+this.goals.filter((g:Goal)=>g.status==='active').length),new Choice('archived','已归档 '+this.goals.filter((g:Goal)=>g.status==='archived').length)],selected:this.archived?'archived':'active',change:(v:string)=>this.archived=v==='archived'})
    ForEach(this.goals.filter((g:Goal)=>(g.status==='archived')===this.archived),(g:Goal)=>{
      Column({space:8}){
        TMLabel({label:g.title}).onClick(()=>this.onNavigate(this.getBase()+'/'+g.id))
        TMNote({label:(g.frequency==='weekly'?'每周':'每天')+' · '+(g.reminder_time||'未设提醒')})
        if(g.status==='active'){TMButton({label:g.entries.some((e:Entry)=>e.day===this.pickedDay)?'已打卡':'打卡',blocked:this.busy,action:()=>this.checkin(g,this.pickedDay)})}
        TMButton({label:'查看目标',action:()=>this.onNavigate(this.getBase()+'/'+g.id)})
      }.padding(15).backgroundColor(WHITE).borderRadius(18)
    },(g:Goal)=>String(g.id)+g.version+g.entries.length)
    if(!this.goals.some((g:Goal)=>(g.status==='archived')===this.archived)){ TMLabel({label:this.archived?'还没有归档目标':'留一个小目标给自己'});TMNote({label:'从一个容易做到的小习惯开始。'}) }
    TMButton({label:'新建生活目标',primary:true,action:()=>this.onNavigate(this.getBase()+'/new')})
  }
  @Builder remindersView(){
    TMBanner({title:'循序律动，\n温和而坚定的陪伴',art:$r('app.media.tm_today_reminders')})
    this.calendarControl()
    TMPills({options:[new Choice('active','启用中 '+this.reminders.filter((r:Reminder)=>r.status==='active').length),new Choice('paused','已关闭 '+this.reminders.filter((r:Reminder)=>r.status!=='active').length)],selected:this.archived?'paused':'active',change:(v:string)=>this.archived=v==='paused'})
    if(this.calendarReason&&this.reminders.some((r:Reminder)=>r.status==='active')){TMNote({label:this.calendarReason})}
    TMButton({label:'新建提醒',primary:true,action:()=>this.onNavigate(this.getBase()+'/new')})
    ForEach(this.reminders.filter((r:Reminder)=>(r.status!=='active')===this.archived),(r:Reminder)=>{
      Row({space:8}){
        Column({space:5}){
          TMLabel({label:r.title})
          TMNote({label:new Date(new Date(r.scheduled_at).getTime()+28800000).toISOString().slice(0,16).replace('T',' ')})
        }.layoutWeight(1).onClick(()=>this.onNavigate(this.getBase()+'/'+r.id+'/edit'))
        Toggle({type:ToggleType.Switch,isOn:r.status==='active'}).enabled(!this.busy)
          .onChange((v:boolean)=>{if(v!==(r.status==='active'))this.write('toggle'+r.id+v,(op:string)=>rpc<Object>('health','toggle_reminder',[actorId(),r.id,v],undefined,op));})
      }.width('100%').padding(14).backgroundColor(WHITE).borderRadius(18)
    },(r:Reminder)=>String(r.id)+r.status+r.title)
    if(!this.reminders.some((r:Reminder)=>(r.status!=='active')===this.archived)){TMLabel({label:this.archived?'暂时没有关闭的提醒':'暂时没有启用的提醒'});TMNote({label:'为生活中重要的小事留一个时刻。'})}
  }
  build(){
    Column(){
      TMHeader({title:this.title(),back:()=>this.onNavigate(this.route===this.getBase()?'today':this.getBase())})
      Scroll(){Column({space:14}){
        if(this.getForm()){this.formView()}else if(this.getGoalMode()){if(this.selectedGoal&&this.getEntityId()){ForEach([this.selectedGoal],(g:Goal)=>{this.detail(g)},(g:Goal)=>String(g.id)+'-'+g.version+'-'+g.progress.today_checked+'-'+g.entries.map((e:Entry)=>e.day).join(','))}else if(!this.getEntityId()){this.goalsView()}}else{this.remindersView()}
        if(this.error){TMNote({label:this.error});TMButton({label:'重新读取',action:()=>this.load(this.serial)})}
      }.padding(16).width('100%').alignItems(HorizontalAlign.Start)}.layoutWeight(1).scrollBar(BarState.Off)
    }.width('100%').height('100%').backgroundColor(PAPER)
  }
}
