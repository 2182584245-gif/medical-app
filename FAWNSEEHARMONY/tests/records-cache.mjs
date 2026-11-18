// Node 24+; exercises the shipped ArkTS with a local SQLite file and deterministic transport failures.
import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import { stripTypeScriptTypes } from 'node:module'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const dir=await mkdtemp(join(tmpdir(),'fawnsee-record-cache-'))
const file=join(dir,'cache.sqlite')
let db=new DatabaseSync(file)
let owner=17, scope='server-a', failure, beforeList
const ctx={}
const photo='data:image/jpeg;base64,'+'Ab01'.repeat(188000)
function row(key,day,version,photos=[]){
  return {key,category:'diet',fields:{date:day,time:'12:10',food:'测试餐点'},photos,raw:{version}}
}
let rows=[row('r17','2026-11-02','v1',[19]),row('r18','2026-11-01','v1')]
const history=[{reason:'核实时间',created_at:'2026-11-02T12:00:00Z'}]
class CloudError extends Error {constructor(status){super('HTTP '+status);this.status=status}}
class CloudNetworkError extends Error {}
function usable(){if(failure)throw failure}
const helpers={
  actorId:()=>owner,recordCacheScope:id=>id===owner?scope:'',CloudError,CloudNetworkError,
  recordList:async()=>{if(beforeList)await beforeList;usable();return rows},
  getRecord:async key=>{usable();return rows.find(r=>r.key===key)??Promise.reject(new CloudError(404))},
  revisions:async()=>{usable();return history},loadPhoto:async()=>{usable();return photo},
  preferences:{getPreferencesSync:()=>({clear:async()=>{},flush:async()=>{}})},
  relationalStore:{SecurityLevel:{S2:2},getRdbStore:async(_,config)=>{
    assert.equal(config.encrypt,true);assert.equal(config.securityLevel,2)
    return {
      beginTransaction:()=>db.exec('BEGIN'),commit:()=>db.exec('COMMIT'),rollBack:()=>db.exec('ROLLBACK'),
      executeSql:async(sql,args=[])=>db.prepare(sql).run(...args),
      querySql:async(sql,args=[])=>{
        const command=db.prepare(sql)
        command.setReturnArrays(true)
        const result=command.all(...args)
        let i=-1
        return {goToNextRow:()=>++i<result.length,getString:col=>result[i][col],getLong:col=>result[i][col],close(){}}
      }
    }
  }}
}
globalThis.fawnseeCacheTests=helpers
function url(s){return 'data:text/javascript;base64,'+Buffer.from(s).toString('base64')}
async function moduleFor(path,replacements){
  let source=stripTypeScriptTypes(await readFile(new URL('../entry/src/main/ets/'+path,import.meta.url),'utf8'))
  for(const [key,target] of Object.entries(replacements))source=source.replace("'"+key+"'",JSON.stringify(target))
  return url(source)
}
const kit=url('export const {preferences,relationalStore}=globalThis.fawnseeCacheTests')
const abi=url('export const common={}')
const storeUrl=await moduleFor('services/RecordsCacheStore.ets',{'@kit.ArkData':kit,'@kit.AbilityKit':abi})
const domain=url('export const {recordList,getRecord,revisions,loadPhoto}=globalThis.fawnseeCacheTests; export const JournalRecord=undefined, Revision=undefined')
const auth=url('export const {actorId,recordCacheScope,CloudError,CloudNetworkError}=globalThis.fawnseeCacheTests')
const sources={'@kit.AbilityKit':abi,'../services/Cloud':auth,'../services/RecordsCacheStore':storeUrl,'./RecordsData':domain}
const lib=await import(await moduleFor('pages/RecordsCache.ets',sources))
const privateData=await import(await moduleFor('services/PrivateData.ets',{'@kit.AbilityKit':abi,'@kit.ArkData':kit,'./RecordsCacheStore':storeUrl}))
const store=await import(storeUrl)
privateData.attachPrivateStorage(ctx)
const list=()=>lib.recordsSnapshot(ctx,owner)
const detail=(key='r17')=>lib.detailSnapshot(ctx,owner,key)
const media=()=>lib.photoSnapshot(ctx,owner,19)
let checks=0
const pass=msg=>{checks++;console.log('PASS '+msg)}
async function warm(){failure=undefined;await list();await detail();await media()}
try{
  await warm()
  assert.equal((await list()).cachedAt,0);assert.equal((await media()).value,photo)
  pass('成功联网读取后创建加密缓存')
  db.close();db=new DatabaseSync(file);failure=new CloudNetworkError()
  const result=await list()
  assert.ok(result.cachedAt>0);assert.deepEqual(result.value.filter(r=>r.fields.date==='2026-11-01').map(r=>r.key),['r18'])
  pass('关闭再打开数据库后断网可查看列表与日期筛选')
  assert.deepEqual((await detail()).value.history,history);assert.ok((await detail()).cachedAt>0)
  const unseen=await detail('r18');assert.equal(unseen.value.historyLoaded,false)
  assert.equal(unseen.value.record.key,'r18')
  pass('已看详情及修改历史恢复；仅同步列表时保留未下载历史标志')
  assert.equal((await media()).value,photo);assert.ok((await media()).cachedAt>0)
  pass('大于游标窗口的原图分段回读一致')
  owner=27
  await assert.rejects(list,/还没有同步/);await assert.rejects(media,/尚未缓存/)
  owner=17;scope='server-b';await assert.rejects(list,/还没有同步/);scope=''
  await assert.rejects(list,/有效的登录授权/);scope='server-a'
  pass('同设备不同会员、不同服务器和过期租约均隔离')
  owner=0;await assert.rejects(list,/当前账号/);owner=17
  for(const fault of [new CloudError(500),new CloudError(429),new SyntaxError('坏的响应'),new Error('证书或权限错误')]){
    failure=fault;await assert.rejects(list,e=>e===fault)
  }
  pass('游客、HTTP 错误、解析错误和证书拒绝不显示缓存')
  for(const status of [401,403]){
    await warm();failure=new CloudError(status);await assert.rejects(detail,e=>e.status===status)
    failure=new CloudNetworkError();await assert.rejects(list,/还没有同步/);await assert.rejects(media,/尚未缓存/)
  }
  pass('401、403 后没有回退，后续断网也不能恢复旧缓存')
  await warm()
  rows=[row('r17','2026-11-02','v2',[])]
  await list();failure=new CloudNetworkError()
  assert.equal((await detail()).value.record.raw.version,'v2');assert.equal((await detail()).value.historyLoaded,false)
  await assert.rejects(detail.bind(null,'r18'),/还没有同步/);await assert.rejects(media,/尚未缓存/)
  pass('更新列表后旧修订、已删除的记录和解绑照片失效')
  await warm();failure=new CloudError(404)
  await assert.rejects(detail,e=>e.status===404);failure=new CloudNetworkError()
  await assert.rejects(detail,/还没有同步/)
  pass('云端已删除记录不会从较旧列表复活')
  await warm()
  let release;beforeList=new Promise(resolve=>{release=resolve})
  const pending=list();await privateData.clearMemberRecordCache(owner);release();await assert.rejects(pending,/状态已改变/);beforeList=undefined
  failure=new CloudNetworkError();await assert.rejects(list,/还没有同步/);await assert.rejects(media,/尚未缓存/)
  pass('清理与在途旧响应并发时旧缓存不重新写入')
  await warm();await privateData.removeMemberDrafts(owner)
  failure=new CloudNetworkError();await assert.rejects(list,/还没有同步/);await assert.rejects(media,/尚未缓存/)
  pass('账号数据重置同时删除列表、详情和原图')
  await warm();await store.forgetRecordMutation(ctx,owner,'r17')
  failure=new CloudNetworkError();await assert.rejects(list,/还没有同步/);await assert.rejects(detail,/还没有同步/)
  pass('保存或删除后旧版本即时失效')
  console.log(checks+' offline checks passed.')
}finally{db.close();await rm(file,{force:true});await rm(dir,{recursive:true,force:true})}
