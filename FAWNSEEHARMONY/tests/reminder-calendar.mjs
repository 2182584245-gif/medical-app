import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'

const source = await readFile(new URL('../entry/src/main/ets/services/ReminderCalendar.ets', import.meta.url), 'utf8')
const js = stripTypeScriptTypes(source.replace(/^import .*$/gm,''),{mode:'transform'}).replace(/^export /gm,'')
const rows=new Map()
const writes=[]
let lastId=0
let granted=false
const state=new Map()
const prefs={getSync:(k,v)=>state.get(k)??v,putSync:(k,v)=>state.set(k,v),async flush(){}}
const calendar={
  getAccount:()=>({name:'fawnsee.life.reminders',type:'local'}),
  getConfig:()=>({enableReminder:true}),
  async getEvents(filter){
    const selected=filter?filter.map(id=>rows.get(id)).filter(Boolean):Array.from(rows.values())
    return structuredClone(selected)
  },
  async addEvent(e){const id=++lastId;rows.set(id,{...structuredClone(e),id});writes.push('add');return id},
  async updateEvent(e){assert.ok(rows.has(e.id));rows.set(e.id,structuredClone(e));writes.push('update')},
  async deleteEvent(id){assert.ok(rows.delete(id));writes.push('delete')}
}
let created=false
const provider={getAllCalendars:async()=>created?[calendar]:[],createCalendar:async()=>{created=true;return calendar}}
const manager={CalendarType:{LOCAL:'local'},EventType:{NORMAL:0},RecurrenceFrequency:{DAILY:1,WEEKLY:2,MONTHLY:3},EventFilter:{filterById:x=>x},getCalendarManager:()=>provider}
const access={createAtManager:()=>({requestPermissionsFromUser:async()=>({authResults:granted?[0,0]:[-1,-1]})})}
const api=Function('calendarManager','abilityAccessCtrl','preferences',js+'\nreturn {chooseCalendar,calendarOwner,synchronizeCalendar}')(
  manager,access,{getPreferencesSync:()=>prefs})
const ctx={}
const base=new Date(2026,10,19,8).getTime()
const plan={key:'routine.breakfast',at:base,title:'早餐记录',repeat:'daily',index:9,skipped:[new Date(2026,10,20)]}
const custom={key:'r13',at:base+600000,title:'合成提醒',repeat:'none',index:13}
await assert.rejects(api.chooseCalendar(ctx,10),/需要允许/)
assert.equal(api.calendarOwner(ctx),0)
granted=true
await api.chooseCalendar(ctx,10)
const sync=(owner,plans)=>api.synchronizeCalendar(ctx,owner,plans,()=>true)
await sync(10,[plan,custom])
assert.equal(rows.size,3)
const routine=Array.from(rows.values()).find(r=>r.identifier.endsWith('routine.breakfast'))
assert.equal(routine.startTime,new Date(2026,10,21,8).getTime())
const before=Array.from(rows.values()).find(r=>r.identifier.includes('.before.'))
assert.equal(before.startTime,base)
assert.equal(before.recurrenceRule,undefined)
const savedWrites=writes.length
await sync(10,[plan,custom])
assert.equal(writes.length,savedWrites)
const original=Array.from(rows.values()).find(r=>r.identifier.endsWith('r13'))
await sync(10,[plan,{...custom,title:'改后',at:custom.at+60000}])
assert.equal(rows.get(original.id).title,'改后')
const unrelated=await calendar.addEvent({title:'用户自行创建',identifier:'other'})
await sync(10,[{...plan,repeat:'weekly',skipped:[]}])
assert.equal(rows.size,2)
assert.ok(rows.has(unrelated))
assert.ok(!rows.has(original.id))
await sync(11,[{...custom,title:'另一位用户'}])
assert.ok([...rows.values()].some(r=>r.identifier==='fawnsee.reminder:11:r13'))
assert.ok(![...rows.values()].some(r=>r.identifier?.startsWith('fawnsee.reminder:10:')))
await sync(11,[])
await api.chooseCalendar(ctx,0)
assert.equal(api.calendarOwner(ctx),0)
assert.equal(rows.size,1)
assert.ok(rows.has(unrelated))
await sync(12,[{...plan,skipped:[new Date(2026,10,22),new Date(2026,10,23),new Date(2026,10,22)]}])
const segments=[...rows.values()].filter(r=>r.identifier.startsWith('fawnsee.reminder:12'))
assert.equal(segments.length,2)
assert.equal(segments.find(r=>r.identifier.includes('.before.')).recurrenceRule.count,3)
assert.equal(segments.find(r=>r.identifier.endsWith('routine.breakfast')).startTime,new Date(2026,10,24,8).getTime())
console.log('Passed app-calendar permission denial/grant, create, idempotence, edits, recurrence changes, recorded meal exclusion, unrelated event preservation, account isolation and opt-out.')
