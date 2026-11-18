import { common } from '@kit.AbilityKit'
import { actorId, CloudError, CloudNetworkError, recordCacheScope } from '../services/Cloud'
import { clearRecordSnapshots, forgetRecordMutation, readRecordSnapshot, recordCacheEpoch, writeRecordSnapshot } from '../services/RecordsCacheStore'
import { getRecord, JournalRecord, loadPhoto, recordList, Revision, revisions } from './RecordsData'

export interface RecordResult<T> { value:T; cachedAt:number }
export interface RecordDetail { record:JournalRecord; history:Revision[]; historyLoaded:boolean }
function scopeFor(owner:number):string {
  return owner>0&&owner===actorId()?recordCacheScope(owner):''
}
function verifyOwner(owner:number):void {
  if(owner<=0||owner!==actorId())throw Error('请重新打开当前账号的记录。')
}
function verifyTicket(owner:number,ticket:number):void {
  verifyOwner(owner)
  if(recordCacheEpoch(owner)!==ticket)throw Error('记录状态已改变，请重新打开。')
}
async function networkFallback(ctx:common.Context,owner:number,ticket:number,scope:string,error:Error,key:string):Promise<void> {
  if(error instanceof CloudError){
    if(error.status===401||error.status===403)await clearRecordSnapshots(ctx,owner)
    if(error.status===404||error.status===410){
      if(key.startsWith('detail:'))await forgetRecordMutation(ctx,owner,key.slice(7))
      else await clearRecordSnapshots(ctx,owner)
    }
  }
  if(!(error instanceof CloudNetworkError))throw error
  verifyOwner(owner)
  if(!scope||scope!==scopeFor(owner))throw Error('本次网络连接失败，离线查看需要有效的登录授权，请联网后重新登录。')
  if(ticket!==recordCacheEpoch(owner))throw Error('记录状态已改变，请重新打开。')
}
async function cached<T>(ctx:common.Context,owner:number,scope:string,key:string,ticket:number):Promise<RecordResult<T>|undefined> {
  const item=await readRecordSnapshot(ctx,owner,scope,key,ticket)
  verifyOwner(owner)
  if(scopeFor(owner)!==scope||ticket!==recordCacheEpoch(owner))throw Error('离线授权已改变，请重新打开。')
  if(!item)return undefined
  return {value:JSON.parse(item.data) as T,cachedAt:item.time}
}
export async function recordsSnapshot(ctx:common.Context,owner:number):Promise<RecordResult<JournalRecord[]>> {
  verifyOwner(owner)
  const scope=scopeFor(owner), ticket=recordCacheEpoch(owner)
  let rows:JournalRecord[]
  try{rows=await recordList()}catch(e){
    await networkFallback(ctx,owner,ticket,scope,e as Error,'list')
    const prior=await cached<JournalRecord[]>(ctx,owner,scope,'list',ticket)
    if(prior)return prior
    throw Error('当前离线，本机还没有同步过此账号的记录列表。')
  }
  verifyOwner(owner)
  if(scope&&scope===scopeFor(owner)){
    const live:Map<string,string>=new Map()
    for(const row of rows){
      live.set('detail:'+row.key,row.raw.version)
      for(const id of row.photos)live.set('photo:'+id,'')
    }
    await writeRecordSnapshot(ctx,owner,scope,'list',JSON.stringify(rows),ticket,'',live).catch(()=>{})
  }
  verifyTicket(owner,ticket)
  return {value:rows,cachedAt:0}
}
export async function detailSnapshot(ctx:common.Context,owner:number,key:string):Promise<RecordResult<RecordDetail>> {
  verifyOwner(owner)
  if(!/^[ro][1-9]\d*$/.test(key))throw Error('这条记录不存在。')
  const scope=scopeFor(owner),ticket=recordCacheEpoch(owner),name='detail:'+key
  let value:RecordDetail
  let fresh:JournalRecord|undefined=undefined
  try{
    const record=await getRecord(key)
    verifyOwner(owner)
    fresh=record
    value={record:record,history:await revisions(key),historyLoaded:true}
  }catch(e){
    await networkFallback(ctx,owner,ticket,scope,e as Error,name)
    const prior=await cached<RecordDetail>(ctx,owner,scope,name,ticket)
    if(prior&&(!fresh||fresh.raw.version===prior.value.record.raw.version))return prior
    if(fresh){
      const partial:RecordDetail={record:fresh,history:[],historyLoaded:false}
      await writeRecordSnapshot(ctx,owner,scope,name,JSON.stringify(partial),ticket,fresh.raw.version).catch(()=>{})
      verifyTicket(owner,ticket)
      return {value:partial,cachedAt:Date.now()}
    }
    const list=await cached<JournalRecord[]>(ctx,owner,scope,'list',ticket)
    const row=list?.value.find((r:JournalRecord)=>r.key===key)
    if(row&&list)return {value:{record:row,history:[],historyLoaded:false},cachedAt:list.cachedAt}
    throw Error('当前离线，本机还没有同步过这条记录。')
  }
  verifyOwner(owner)
  if(scope&&scope===scopeFor(owner))await writeRecordSnapshot(ctx,owner,scope,name,JSON.stringify(value),ticket,value.record.raw.version).catch(()=>{})
  verifyTicket(owner,ticket)
  return {value:value,cachedAt:0}
}
export async function photoSnapshot(ctx:common.Context,owner:number,id:number):Promise<RecordResult<string>> {
  verifyOwner(owner)
  const scope=scopeFor(owner),ticket=recordCacheEpoch(owner),name='photo:'+id
  let value:string
  try{value=await loadPhoto(id)}catch(e){
    await networkFallback(ctx,owner,ticket,scope,e as Error,name)
    const prior=await cached<string>(ctx,owner,scope,name,ticket)
    if(prior)return prior
    throw Error('照片尚未缓存，可联网后查看原图。')
  }
  verifyOwner(owner)
  if(scope&&scope===scopeFor(owner))await writeRecordSnapshot(ctx,owner,scope,name,JSON.stringify(value),ticket).catch(()=>{})
  verifyTicket(owner,ticket)
  return {value:value,cachedAt:0}
}
