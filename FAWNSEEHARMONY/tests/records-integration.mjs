// Run with Node 24 or later. Network calls use the real test server, including authentication.
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { DatabaseSync } from 'node:sqlite'

const base = (process.env.FAWNSEE_QA_URL || '').replace(/\/$/, '')
const username = process.env.FAWNSEE_QA_USERNAME
const password = process.env.FAWNSEE_QA_PASSWORD
if (!base || !username || !password) throw new Error('Set FAWNSEE_QA_URL, FAWNSEE_QA_USERNAME and FAWNSEE_QA_PASSWORD for a dedicated test member.')
const login = await fetch(base+'/v1/auth/login', {
  method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username, password })
})
assert.equal(login.status, 200, 'The test member must be able to log in.')
const session = await login.json()
const actor = session.user.id
async function rpc(service, method, args, kwargs = {}, request_id = randomUUID()) {
  const response = await fetch(base+'/v1/rpc/'+service+'/'+method, {
    method:'POST', headers:{Authorization:'Bearer '+session.access_token,'Content-Type':'application/json'},
    body:JSON.stringify({args,kwargs,request_id})
  })
  assert.ok(response.ok, service+'.'+method+' returned HTTP '+response.status)
  const envelope = await response.json()
  return JSON.parse(JSON.stringify(envelope.result), (_, item) => {
    if (!item?.$__type) return item
    return item.value ?? item.items ?? item.fields
  })
}

// Execute the same ArkTS domain code as the UI. Only the device APIs are replaced here.
const sqlite = new DatabaseSync(':memory:')
const rdb = {
  SecurityLevel:{S2:2},
  getRdbStore: async () => ({
    executeSql:async (sql,args=[])=>sqlite.prepare(sql).run(...args),
    querySql:async (sql,args=[])=>{
      const stmt=sqlite.prepare(sql)
      stmt.setReturnArrays(true)
      const rows=stmt.all(...args)
      let index=-1
      return {goToNextRow:()=>++index<rows.length,getString:i=>rows[index][i],close(){}}
    }
  })
}
function moduleUrl(source) { return 'data:text/javascript;base64,'+Buffer.from(source).toString('base64') }
globalThis.recordTestDevice = {rpc, actorId:()=>actor, util:{generateRandomUUID:()=>randomUUID()}, relationalStore:rdb}
const replacements = new Map([
  ['../services/Cloud','export const {rpc,actorId}=globalThis.recordTestDevice; export class CloudNetworkError extends Error {}'],
  ['@kit.ArkTS','export const {util}=globalThis.recordTestDevice'],
  ['@kit.ArkData','export const {relationalStore}=globalThis.recordTestDevice'],
  ['@kit.AbilityKit','export const common={}']
])
let source = stripTypeScriptTypes(await readFile(new URL('../entry/src/main/ets/pages/RecordsData.ets',import.meta.url),'utf8'))
for (const [name, code] of replacements) source = source.replace("'"+name+"'",JSON.stringify(moduleUrl(code)))
const lib = await import(moduleUrl(source))

const cases = {
  diet:{meal:'午餐',food:'米饭与青菜',calories_kcal:'410'},
  water:{amount_ml:'360'}, activity:{activity:'散步',duration_minutes:'25',steps:'2000'},
  sleep:{duration_hours:'7.5'}, environment:{temperature_c:'23',humidity_percent:'52',description:'开窗通风'},
  medical:{name:'复诊记录',location:'测试机构',description:'例行复查'},
  metric:{kind:'blood_pressure',value:'120',diastolic:'78',unit:'mmHg'}
}
const day = lib.chinaDate(new Date(Date.now()-86400000).toISOString()).slice(0,10)
// Synthetic 48x36 solid-color PNG, generated specifically for this test.
const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAkCAIAAABAJy5dAAAAVUlEQVR4nM3OQQEAIBCAMCSXOWxjd/9XQJZga99DicRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjMRIjL8D0wOHgAFXfMVFRwAAAABJRU5ErkJggg=='
let checks = 0
function pass(label) { checks++; console.log('PASS '+label) }
function tagged(record, tag) {return Object.values(record.fields).some(v=>v.includes(tag))}

async function verifyCategory(category, values, fileId) {
  const tag='harmony-test-'+randomUUID()
  const ids=['diet','environment','medical'].includes(category)?[fileId]:[]
  const fields=Object.assign(lib.newFields(category),values,{date:day,time:'09:45',notes:tag})
  if(category==='environment') fields.description+=' '+tag
  if(category==='medical') fields.name+=' '+tag
  const input=lib.validatedFields(category,fields,ids.length,false)
  let deleted=false
  try {
    const operation=lib.uuid()
    await lib.storeRecord(category,input,ids,undefined,operation)
    await lib.storeRecord(category,input,ids,undefined,operation)
    const matching=(await lib.recordList()).filter(r=>r.category===category&&tagged(r,tag))
    assert.equal(matching.length,1)
    const old=await lib.getRecord(matching[0].key)
    assert.equal(old.fields.time,'09:45')
    assert.equal(old.fields.date,day)
    if(ids.length){assert.deepEqual(old.photos,ids);assert.equal(await lib.loadPhoto(old.photos[0]),png)}
    pass(category+' 新增、相同操作重试、详情'+(ids.length?'与原图':''))
    const changes={...old.fields,time:'20:00',reason:'核对发生时间'}
    await lib.storeRecord(category,lib.validatedFields(category,changes,ids.length,true),ids,old,lib.uuid())
    const updated=await lib.getRecord(old.key)
    assert.equal(updated.fields.time,'20:00')
    assert.notEqual(updated.raw.version,old.raw.version)
    if(ids.length) assert.equal(await lib.loadPhoto(updated.photos[0]),png)
    if(category!=='metric') assert((await lib.revisions(old.key)).some(h=>h.reason==='核对发生时间'))
    pass(category+' 修改后重开详情')
    await assert.rejects(lib.storeRecord(category,changes,ids,old,lib.uuid()),/HTTP 409/)
    pass(category+' 版本冲突保护')
    await lib.removeRecord(updated,lib.uuid())
    assert(!(await lib.recordList()).some(r=>r.key===old.key))
    deleted=true
    pass(category+' 删除并刷新列表')
  } finally {
    if(!deleted) for(const row of await lib.recordList()) {
      if(row.category===category&&tagged(row,tag)) await lib.removeRecord(row,lib.uuid())
    }
  }
}

let photoId
try {
  const upload={id:0,uri:png,operation:lib.uuid()}
  photoId=await lib.uploadPhoto(upload,actor)
  assert.equal(await lib.uploadPhoto(upload,actor),photoId)
  assert.equal(await lib.loadPhoto(photoId),png)
  pass('照片上传幂等与原文件回读')
  for(const [category,fields] of Object.entries(cases)) await verifyCategory(category,fields,photoId)
  assert.equal(lib.validatedFields('diet',lib.newFields('diet'),1,false).food,'照片记录')
  assert.throws(()=>lib.validatedFields('diet',lib.newFields('diet'),0,false))
  assert.throws(()=>lib.validatedFields('water',{...lib.newFields('water'),amount_ml:'10001'},0,false))
  assert.throws(()=>lib.validatedFields('metric',{...lib.newFields('metric'),kind:'blood_pressure',value:'90',diastolic:'110'},0,false))
  pass('图片单独记录、必填及数值边界')
  const photo={id:0,uri:'data:image/png;base64,'+'A'.repeat(2500000),operation:lib.uuid()}
  const op=lib.uuid()
  await lib.writeDraft({},actor,'diet',lib.newFields('diet'),[photo],op)
  assert.equal(await lib.readDraft({},actor+1,'diet'),undefined)
  const draft=await lib.readDraft({},actor,'diet')
  assert.deepEqual(draft.photos[0],photo)
  assert.equal(draft.operation,op)
  await lib.dropDraft({},actor,'diet')
  assert.equal(await lib.readDraft({},actor,'diet'),undefined)
  const captured={id:11,uri:png,operation:lib.uuid(),local:true}
  const remote={id:12,uri:png,operation:lib.uuid()}
  await lib.writeDraft({},actor,'edit:r991',lib.newFields('diet'),[captured,remote],op,'server-version-1')
  const editDraft=await lib.readDraft({},actor,'edit:r991')
  assert.equal(editDraft.baseVersion,'server-version-1')
  assert.equal(editDraft.operation,op)
  assert.deepEqual(editDraft.photos[0],captured)
  assert.equal(editDraft.photos[1].uri,'')
  await lib.dropDraft({},actor,'edit:r991')
  pass('本机断网图文暂存、分段恢复、账号隔离')
  assert.equal(checks,31)
  console.log(checks+' checks passed.')
} finally {
  if(photoId) await rpc('files','delete_file',[actor,photoId])
  sqlite.close()
}

