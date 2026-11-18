import { reminderAgentManager as manager } from '@kit.BackgroundTasksKit'
import { notificationManager } from '@kit.NotificationKit'
import { common } from '@kit.AbilityKit'
import {calendarManager} from '@kit.CalendarKit'
import {BusinessError} from '@kit.BasicServicesKit'
import { actorId,rpc } from './Cloud'
import { calendarOwner,chooseCalendar,DevicePlan,synchronizeCalendar } from './ReminderCalendar'

interface Reminder {id:number;title:string;scheduled_at:string;repeat_rule?:string;status:string;enabled?:boolean;paused_until?:string}
interface Goal {id:number;title:string;reminder_time?:string;frequency:string;created_at?:string}
interface GoalPage {items:Goal[];total:number}
interface Meal {occurred_at:string;details?:Record<string,Object>}
interface MealPage {items:Meal[];total:number}
interface Routine {kind:string;hour:number;label:string;message:string}
const routines:Routine[]=[
  {kind:'breakfast',hour:8,label:'早餐记录提醒',message:'记下今天的早餐，也别忘了记录昨日睡眠情况。'},
  {kind:'goals',hour:9,label:'目标与习惯提醒',message:'看看今天的目标与习惯，按自己的节奏安排这一天。'},
  {kind:'lunch',hour:12,label:'午餐记录提醒',message:'午饭吃了什么？拍一张照片或说一说，留下这顿饭的记录。'},
  {kind:'dinner',hour:18,label:'晚餐记录提醒',message:'记下今天的晚饭，让这一天的饮食记录更完整。'}]
const aliases:Record<string,string>={'早餐':'breakfast','早饭':'breakfast','午餐':'lunch','午饭':'lunch','晚餐':'dinner','晚饭':'dinner'}
async function routinePlans(owner:number,now:number):Promise<DevicePlan[]> {
  const first=new Date(now)
  first.setHours(0,0,0,0)
  const skipped=new Map<string,Set<number>>()
  let offset=0
  while(owner===actorId()){
    const page=await rpc<MealPage>('expansion','list_records',[owner,{'category':'diet','limit':50,'offset':offset} as Record<string,Object>])
    let older=false
    for(const row of page.items){
      const at=new Date(row.occurred_at)
      if(at.getTime()<first.getTime()){older=true;break}
      const raw=String(row.details?.['meal_type']||row.details?.['meal']||'').trim().toLowerCase()
      const meal=aliases[raw]||raw
      if(!['breakfast','lunch','dinner'].includes(meal))continue
      at.setHours(0,0,0,0)
      const dates=skipped.get(meal)||new Set<number>()
      dates.add(at.getTime())
      skipped.set(meal,dates)
    }
    offset+=page.items.length
    if(older||!page.items.length||offset>=page.total)break
  }
  return routines.map((routine:Routine,index:number)=>{
    const date=new Date(now)
    date.setHours(routine.hour,0,0,0)
    if(date.getTime()<=now)date.setDate(date.getDate()+1)
    const days=Array.from(skipped.get(routine.kind)||new Set<number>()).map((n:number)=>new Date(n))
    const plan:DevicePlan={key:'routine.'+routine.kind,title:routine.message,heading:routine.label,at:date.getTime(),repeat:'daily',index:1800000000+index,skipped:days}
    return plan
  })
}
async function applySkips(id:number,dates:Date[]):Promise<void>{
  const existing=await manager.getExcludeDates(id)
  if(existing.length===dates.length&&existing.every((a:Date)=>dates.some((b:Date)=>a.toDateString()===b.toDateString())))return
  if(existing.length)await manager.deleteExcludeDates(id)
  for(const day of dates)await manager.addExcludeDate(id,day)
}
let queue:Promise<void>=Promise.resolve()
const DAY=86400000
function nextTime(start:number,now:number,repeat:string):number {
  if(start>=now)return start
  if(repeat==='daily'||repeat==='weekly'){const step=repeat==='daily'?DAY:7*DAY;return start+Math.ceil((now-start)/step)*step}
  if(repeat!=='monthly')return NaN
  const a=new Date(start+28800000),b=new Date(now+28800000)
  for(let n=0;n<2;n++){
    const month=b.getUTCMonth()+n,year=b.getUTCFullYear()
    const last=new Date(Date.UTC(year,month+1,0)).getUTCDate()
    const t=Date.UTC(year,month,Math.min(a.getUTCDate(),last),a.getUTCHours(),a.getUTCMinutes())-28800000
    if(t>=now)return t
  }
  return NaN
}
export function clampsMonthlyDate(start:number,repeat:string):boolean{
  return repeat==='monthly'&&new Date(start+28800000).getUTCDate()>28
}
// System calendar reminders repeat on numbered days. Months that end before
// the original day must instead receive their own, correctly dated reminder.
function upcomingMonthEnds(base:number,floor:number):number[]{
  const dates:number[]=[]
  let after=floor
  for(let i=0;i<12;i++){
    const at=nextTime(base,after,'monthly')
    if(!Number.isFinite(at))break
    dates.push(at)
    after=at+1
  }
  return dates
}
export class ReminderScheduleError extends Error {
  constructor(detail:string){super(detail);this.name='ReminderScheduleError'}
}
export async function reviewCalendarReminder(context:common.UIAbilityContext,title:string,start:number,repeat:string):Promise<boolean>{
  const at=nextTime(start,Date.now(),repeat)
  if(!Number.isFinite(at))throw new Error('提醒时间已过，请先调整时间并保存。')
  const event:calendarManager.Event={type:calendarManager.EventType.NORMAL,title,startTime:at,endTime:at+600000,reminderTime:[0]}
  if(repeat==='daily'||repeat==='weekly'||repeat==='monthly'&&!clampsMonthlyDate(start,repeat)){
    const freq=repeat==='daily'?calendarManager.RecurrenceFrequency.DAILY:repeat==='weekly'?calendarManager.RecurrenceFrequency.WEEKLY:calendarManager.RecurrenceFrequency.MONTHLY
    event.recurrenceRule={recurrenceFrequency:freq}
  }
  const id=await calendarManager.getCalendarManager(context).editEvent(event)
  return id>0
}
function request(plan:DevicePlan,context:common.UIAbilityContext):manager.ReminderRequestCalendar {
  const when=new Date(plan.at)
  const time:manager.LocalDateTime={year:when.getFullYear(),month:when.getMonth()+1,day:when.getDate(),hour:when.getHours(),minute:when.getMinutes(),second:when.getSeconds()}
  const value:manager.ReminderRequestCalendar={reminderType:manager.ReminderType.REMINDER_TYPE_CALENDAR,dateTime:time,
    title:plan.heading||'凡思 · 生活提醒',content:plan.title,expiredContent:plan.title,groupId:plan.key,notificationId:plan.index,
    ringDuration:1,slotType:notificationManager.SlotType.CONTENT_INFORMATION,
    wantAgent:{pkgName:context.abilityInfo.bundleName,abilityName:context.abilityInfo.name},
    actionButton:[{type:manager.ActionButtonType.ACTION_BUTTON_TYPE_CLOSE,title:'知道了'}]}
  if(plan.repeat==='daily')value.daysOfWeek=[1,2,3,4,5,6,7]
  if(plan.repeat==='weekly')value.daysOfWeek=[when.getDay()||7]
  if(plan.repeat==='monthly'){value.repeatMonths=[1,2,3,4,5,6,7,8,9,10,11,12];value.repeatDays=[when.getDate()]}
  return value
}
async function reconcile(context:common.UIAbilityContext,ask:boolean,owner:number):Promise<void>{
  if(!owner||owner!==actorId())return
  const selectedOwner=calendarOwner(context)
  if(selectedOwner&&selectedOwner!==owner){
    await synchronizeCalendar(context,selectedOwner,[],()=>owner===actorId())
    if(owner!==actorId())return
    await chooseCalendar(context,0)
  }
  const now=Date.now()
  const rows=await rpc<Reminder[]>('health','list_reminders',[owner])
  const plans:DevicePlan[]=[]
  const extraMonths:DevicePlan[]=[]
  for(const row of rows){
    if(row.status!=='active'||row.enabled===false)continue
    const repeat=row.repeat_rule||'none',base=Date.parse(row.scheduled_at)
    const floor=row.paused_until?Math.max(now,Date.parse(row.paused_until)):now
    if(clampsMonthlyDate(base,repeat)){
      const dates=upcomingMonthEnds(base,floor)
      dates.forEach((at:number,index:number)=>{
        const month=new Date(at+28800000).toISOString().slice(0,7)
        const plan:DevicePlan={key:'r'+row.id+'.'+month,title:row.title,at,repeat:'none',index:row.id*2}
        if(index===0)plans.push(plan);else extraMonths.push(plan)
      })
    }else{
      const at=nextTime(base,floor,repeat)
      if(Number.isFinite(at))plans.push({key:'r'+row.id,title:row.title,at,repeat,index:row.id*2})
    }
  }
  let offset=0
  while(true){
    const page=await rpc<GoalPage>('expansion','list_goals',[owner,{'status':'active','limit':100,'offset':offset} as Record<string,Object>])
    for(const goal of page.items){
      if(!goal.reminder_time||!/^\d{2}:\d{2}/.test(goal.reminder_time))continue
      const date=new Date((goal.created_at?Date.parse(goal.created_at):now)+28800000).toISOString().slice(0,10)
      const repeat=goal.frequency==='weekly'?'weekly':'daily',at=nextTime(Date.parse(date+'T'+goal.reminder_time.slice(0,5)+':00+08:00'),now,repeat)
      if(Number.isFinite(at))plans.push({key:'g'+goal.id,title:goal.title,at,repeat,index:goal.id*2+1})
    }
    offset+=page.items.length;if(!page.items.length||offset>=page.total||page.items.length<100)break
  }
  if(owner!==actorId())return
  if(calendarOwner(context)===owner){
    const routines=await routinePlans(owner,now)
    if(owner!==actorId())return
    try{
      await cancelAgentReminders()
      await synchronizeCalendar(context,owner,plans.concat(extraMonths,routines),()=>owner===actorId())
    }catch(e){throw new ReminderScheduleError('系统日历同步失败。请检查日历权限和通知设置后重试。')}
    return
  }
  if(!await notificationManager.isNotificationEnabled()){
    if(!plans.length)return
    if(!ask)throw new ReminderScheduleError('系统通知未开启。你可以开启通知，或把需要的提醒添加到系统日历。')
    try{await notificationManager.requestEnableNotification(context)}catch(_){throw new ReminderScheduleError('系统通知未开启。你可以开启通知，或把需要的提醒添加到系统日历。')}
  }
  plans.push(...await routinePlans(owner,now))
  if(owner!==actorId())return
  const previous=await manager.getAllValidReminders()
  const otherSlots=previous.filter(item=>!item.reminderReq.groupId?.startsWith('fs:')).length
  const available=Math.max(0,30-plans.length-otherSlots)
  extraMonths.sort((a:DevicePlan,b:DevicePlan)=>a.at-b.at)
  plans.push(...extraMonths.slice(0,available))
  const wanted=new Map<string,manager.ReminderRequestCalendar>()
  const skipped=new Map<string,Date[]>()
  plans.sort((a:DevicePlan,b:DevicePlan)=>a.at-b.at)
  for(const plan of plans){
    const key='fs:'+owner+':'+plan.key
    const value=request(plan,context)
    value.groupId=key
    wanted.set(key,value)
    if(plan.skipped)skipped.set(key,plan.skipped)
  }
  for(const item of previous){
    if(owner!==actorId())return
    const old=item.reminderReq as manager.ReminderRequestCalendar,key=old.groupId||''
    if(!key.startsWith('fs:'))continue
    const replacement=wanted.get(key)
    if(!replacement){await manager.cancelReminder(item.reminderId);continue}
    if(old.content!==replacement.content||JSON.stringify(old.dateTime)!==JSON.stringify(replacement.dateTime)||
       JSON.stringify(old.daysOfWeek||[])!==JSON.stringify(replacement.daysOfWeek||[])||JSON.stringify(old.repeatDays||[])!==JSON.stringify(replacement.repeatDays||[])){
      await manager.updateReminder(item.reminderId,replacement)
    }
    if(skipped.has(key))await applySkips(item.reminderId,skipped.get(key)!)
    wanted.delete(key)
  }
  for(const value of wanted.values()){
    if(owner!==actorId())return
    let id:number
    try{id=await manager.publishReminder(value)}
    catch(e){
      const code=(e as BusinessError).code
      if(code===201||code===1700002)throw new ReminderScheduleError('请在提醒中心开启「系统日历自动提醒」，让本机日历按时通知你。')
      throw new ReminderScheduleError('本机暂时无法安排自动提醒。你可以稍后重试或添加到系统日历。')
    }
    const dates=skipped.get(value.groupId||'')
    if(dates?.length)await applySkips(id,dates)
  }
}
export function syncDeviceReminders(context:common.UIAbilityContext,ask:boolean=true):Promise<void>{
  const owner=actorId()
  const task=queue.then(()=>reconcile(context,ask,owner))
  queue=task.catch(()=>{})
  return task
}
async function cancelAgentReminders():Promise<void>{
  try{
    const current=await manager.getAllValidReminders()
    for(const item of current){if(item.reminderReq.groupId?.startsWith('fs:'))await manager.cancelReminder(item.reminderId)}
  }catch(e){
    const code=(e as BusinessError).code
    if(code!==201&&code!==1700002)throw e as Error
  }
}
export function usesCalendarReminders(context:common.UIAbilityContext):boolean{
  return actorId()>0&&calendarOwner(context)===actorId()
}
export function setCalendarReminders(context:common.UIAbilityContext,enabled:boolean):Promise<void>{
  const owner=actorId()
  const task=queue.then(async()=>{
    if(owner<=0||owner!==actorId())throw new Error('请登录后再设置本机提醒。')
    if(enabled){
      await chooseCalendar(context,owner)
      if(owner===actorId())await reconcile(context,false,owner)
    }else{
      await synchronizeCalendar(context,owner,[],()=>owner===actorId())
      if(owner===actorId())await chooseCalendar(context,0)
    }
  })
  queue=task.catch(()=>{})
  return task
}
export function clearDeviceReminders(context:common.UIAbilityContext):Promise<void>{
  const task=queue.then(async()=>{
    const selected=calendarOwner(context)
    if(selected&&actorId()!==selected){
      await synchronizeCalendar(context,selected,[],()=>actorId()!==selected)
      if(actorId()!==selected)await chooseCalendar(context,0)
    }
    await cancelAgentReminders()
  })
  queue=task.catch(()=>{})
  return task
}
