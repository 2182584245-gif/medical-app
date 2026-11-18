import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'

const source = await readFile(new URL('../entry/src/main/ets/services/DeviceReminders.ets', import.meta.url), 'utf8')
const js = stripTypeScriptTypes(source.replace(/^import .*$/gm, ''), { mode: 'transform' }).replace(/^export /gm, '')
let owner = 4
let permission = true
let meals = []
let reminders = []
let next = 0
const scheduled = new Map()
const skipped = new Map()
const manager = {
  ReminderType: { REMINDER_TYPE_CALENDAR: 1 },
  ActionButtonType: { ACTION_BUTTON_TYPE_CLOSE: 0 },
  async publishReminder(req) { const id = ++next; scheduled.set(id, req); return id },
  async updateReminder(id, req) { assert.ok(scheduled.has(id)); scheduled.set(id, req) },
  async cancelReminder(id) { scheduled.delete(id); skipped.delete(id) },
  async getAllValidReminders() { return Array.from(scheduled, ([reminderId, reminderReq]) => ({ reminderId, reminderReq })) },
  async getExcludeDates(id) { return skipped.get(id) || [] },
  async deleteExcludeDates(id) { skipped.delete(id) },
  async addExcludeDate(id, day) { skipped.set(id, [...(skipped.get(id) || []), day]) }
}
async function rpc(service, method, [actor]) {
  assert.equal(actor, owner)
  if (method === 'list_reminders') return reminders
  const items = method === 'list_records' ? meals : []
  return { items, total: items.length }
}
const permissions = { SlotType:{CONTENT_INFORMATION:1},async isNotificationEnabled() { return permission }, async requestEnableNotification() { permission = true } }
const api = Function('manager', 'notificationManager', 'actorId', 'rpc', 'calendarOwner', js + '\nreturn { syncDeviceReminders, clearDeviceReminders, nextTime }')(manager, permissions, () => owner, rpc, () => 0)
const synchronize = () => api.syncDeviceReminders({abilityInfo:{bundleName:'test.app',name:'EntryAbility'}}, false)
await synchronize()
assert.equal(scheduled.size, 4)
assert.deepEqual(Array.from(scheduled.values(), r => r.dateTime.hour).sort((a,b)=>a-b), [8,9,12,18])

const findId = key => Array.from(scheduled).find(([,r]) => r.groupId.endsWith(':'+key))?.[0]
const today = new Date()
today.setHours(9, 0, 0, 0)
meals = [{ occurred_at: today.toISOString(), details: { meal_type: '晚餐' } }]
await synchronize()
assert.equal(skipped.get(findId('routine.dinner'))[0].toDateString(), today.toDateString())
assert.equal(skipped.get(findId('routine.lunch')), undefined)
meals[0].details = { meal: 'lunch' }
await synchronize()
assert.equal(skipped.get(findId('routine.dinner')), undefined)
assert.equal(skipped.get(findId('routine.lunch')).length, 1)
meals = []
const at = new Date(Date.now()+3600000).toISOString()
reminders = [{ id: 8, title: '喝水', scheduled_at: at, status: 'active', repeat_rule: 'none' }]
await synchronize()
assert.equal(scheduled.get(findId('r8')).content, '喝水')
assert.equal(skipped.size, 0)
await synchronize()
assert.equal(scheduled.size, 5)
reminders[0].status = 'paused'
await synchronize()
assert.equal(scheduled.size, 4)
owner = 5
await synchronize()
assert.ok(Array.from(scheduled.values()).every(r => r.groupId.startsWith('fs:5:')))
await api.clearDeviceReminders()
assert.equal(scheduled.size, 0)
permission = false
await synchronize()
assert.equal(scheduled.size, 0)
const monthEnd = api.nextTime(Date.parse('2028-01-31T09:00:00+08:00'), Date.parse('2028-02-15T00:00:00+08:00'), 'monthly')
assert.equal(new Date(monthEnd).toISOString(), '2028-02-29T01:00:00.000Z')
console.log('Passed routine times, recorded meal suppression, edits and deletions, member isolation, permissions, custom reminders and leap-month calculation.')

