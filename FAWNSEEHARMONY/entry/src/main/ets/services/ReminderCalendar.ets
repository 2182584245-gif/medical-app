import { abilityAccessCtrl,common,Permissions } from '@kit.AbilityKit'
import { calendarManager } from '@kit.CalendarKit'
import { preferences } from '@kit.ArkData'

export interface DevicePlan {
  key:string;title:string;at:number;repeat:string;index:number;heading?:string;skipped?:Date[]
}
const account:calendarManager.CalendarAccount={name:'fawnsee.life.reminders',type:calendarManager.CalendarType.LOCAL,displayName:'凡思生活提醒'}
const prefix='fawnsee.reminder:'
const access:Permissions[]=['ohos.permission.READ_CALENDAR','ohos.permission.WRITE_CALENDAR']

function prefs(ctx:common.UIAbilityContext):preferences.Preferences {
  return preferences.getPreferencesSync(ctx,{name:'device_reminder_calendar'})
}
export function calendarOwner(ctx:common.UIAbilityContext):number {
  return prefs(ctx).getSync('owner',0) as number
}
export async function chooseCalendar(ctx:common.UIAbilityContext,owner:number):Promise<void>{
  const db=prefs(ctx)
  if(owner){
    const result=await abilityAccessCtrl.createAtManager().requestPermissionsFromUser(ctx,access)
    if(result.authResults.length!==access.length||result.authResults.some(value=>value!==0)){
      throw new Error('需要允许使用日历才能自动提醒。可在系统设置中为凡思开启日历权限。')
    }
  }
  db.putSync('owner',owner)
  await db.flush()
}
async function ownedCalendar(ctx:common.UIAbilityContext,create:boolean):Promise<calendarManager.Calendar|undefined>{
  const manager=calendarManager.getCalendarManager(ctx)
  const calendars=await manager.getAllCalendars()
  const cal=calendars.find((item:calendarManager.Calendar)=>item.getAccount().name===account.name&&item.getAccount().type===account.type)
  if(cal||!create)return cal
  const result=await manager.createCalendar(account)
  return result
}
function frequency(repeat:string):calendarManager.RecurrenceFrequency|undefined{
  if(repeat==='daily')return calendarManager.RecurrenceFrequency.DAILY
  if(repeat==='weekly')return calendarManager.RecurrenceFrequency.WEEKLY
  if(repeat==='monthly')return calendarManager.RecurrenceFrequency.MONTHLY
  return undefined
}
function localDay(at:number):number{
  const date=new Date(at)
  return Date.UTC(date.getFullYear(),date.getMonth(),date.getDate())/86400000
}
function mealSegments(plan:DevicePlan,owner:number):calendarManager.Event[]{
  let first=plan.at
  const days=new Set<number>()
  for(const day of plan.skipped||[]){
    const date=new Date(day.getTime())
    const clock=new Date(plan.at)
    date.setHours(clock.getHours(),clock.getMinutes(),clock.getSeconds(),clock.getMilliseconds())
    if(date.getTime()>=plan.at)days.add(date.getTime())
  }
  const events:calendarManager.Event[]=[]
  for(const skip of Array.from(days).sort((a:number,b:number)=>a-b)){
    const count=localDay(skip)-localDay(first)
    if(count>0){
      const e=calendarEvent(plan,owner)
      e.identifier=e.identifier+'.before.'+localDay(skip)
      e.startTime=first;e.endTime=first+600000
      if(count===1)e.recurrenceRule=undefined
      else e.recurrenceRule={recurrenceFrequency:calendarManager.RecurrenceFrequency.DAILY,count:count}
      events.push(e)
    }
    const next=new Date(skip);next.setDate(next.getDate()+1);first=next.getTime()
  }
  const tail=calendarEvent(plan,owner)
  tail.startTime=first;tail.endTime=first+600000
  events.push(tail)
  return events
}
function calendarEvent(plan:DevicePlan,owner:number):calendarManager.Event {
  const identifier=prefix+owner+':'+plan.key
  const event:calendarManager.Event={type:calendarManager.EventType.NORMAL,identifier,title:plan.heading||plan.title,
    startTime:plan.at,endTime:plan.at+600000,reminderTime:[0]}
  if(plan.heading)event.description=plan.title
  if(!plan.key.startsWith('routine.'))event.timeZone='Asia/Shanghai'
  const freq=frequency(plan.repeat)
  if(freq!==undefined)event.recurrenceRule={recurrenceFrequency:freq}
  return event
}
function reportedFrequency(event:calendarManager.Event):number{
  // The provider reads a one-time event back with a negative frequency.
  return event.recurrenceRule?.recurrenceFrequency??-1
}
function sameEvent(a:calendarManager.Event,b:calendarManager.Event):boolean{
  return a.title===b.title&&a.startTime===b.startTime&&a.endTime===b.endTime&&(a.description||'')===(b.description||'')&&
    reportedFrequency(a)===reportedFrequency(b)&&
    (a.recurrenceRule?.count||0)===(b.recurrenceRule?.count||0)&&!(a.recurrenceRule?.excludedDates?.length)&&
    JSON.stringify(Array.from(a.reminderTime||[]))===JSON.stringify(Array.from(b.reminderTime||[]))
}
export async function managedCalendarEvents(target:calendarManager.Calendar):Promise<calendarManager.Event[]>{
  const rows=await target.getEvents()
  const ids:number[]=[]
  for(const row of rows){if(row.id&&row.identifier?.startsWith(prefix))ids.push(row.id)}
  let events:calendarManager.Event[]=[]
  for(let i=0;i<ids.length;i+=100){
    const filter=calendarManager.EventFilter.filterById(ids.slice(i,i+100))
    events=events.concat(await target.getEvents(filter,['id','title','identifier','startTime','endTime','description','recurrenceRule','reminderTime']))
  }
  return events
}
export async function synchronizeCalendar(ctx:common.UIAbilityContext,owner:number,plans:DevicePlan[],current:()=>boolean):Promise<void>{
  const target=await ownedCalendar(ctx,plans.length>0)
  if(!target||!current())return
  const config=target.getConfig()
  if(plans.length&&!config.enableReminder)await target.setConfig({enableReminder:true,color:'#507963'})
  // A successful calendar write can be followed by a lost UI response. Match the persistent
  // event identifier on every sync, including after process restarts.
  const events=await managedCalendarEvents(target)
  const desired=new Map<string,calendarManager.Event>()
  for(const plan of plans){
    // Splitting at recorded meals also handles the Calendar Kit provider's time-zone
    // conversion of excludedDates, without creating one event per future day.
    const parts=plan.skipped?.length&&plan.repeat==='daily'?mealSegments(plan,owner):[calendarEvent(plan,owner)]
    for(const event of parts)desired.set(event.identifier!,event)
  }
  for(const old of events){
    if(!current())return
    if(!old.id||!old.identifier?.startsWith(prefix))continue
    const event=desired.get(old.identifier)
    if(!event){await target.deleteEvent(old.id);continue}
    if(!sameEvent(old,event)){
      // The provider can retain its recurrence rule on a partial update. Changing the
      // frequency uses replacement so a paused series cannot keep firing.
      if(reportedFrequency(old)!==reportedFrequency(event)||old.recurrenceRule?.excludedDates?.length||
          (old.recurrenceRule?.count||0)!==(event.recurrenceRule?.count||0)){
        await target.deleteEvent(old.id)
        if(!current())return
        await target.addEvent(event)
      }else{
        event.id=old.id
        await target.updateEvent(event)
      }
    }
    desired.delete(old.identifier)
  }
  for(const value of desired.values()){
    if(!current())return
    const id=await target.addEvent(value)
    if(id<=0)throw new Error('系统未保存这条日历提醒，请重新同步。')
  }
}
