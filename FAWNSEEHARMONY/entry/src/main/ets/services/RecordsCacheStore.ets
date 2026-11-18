import { common } from '@kit.AbilityKit'
import { relationalStore } from '@kit.ArkData'

export interface CachedRecordValue { data:string; time:number }
const epochs:Map<number,number>=new Map()
let queue:Promise<void>=Promise.resolve()

export function recordCacheEpoch(owner:number):number { return epochs.get(owner)??0 }
function serial<T>(job:()=>Promise<T>):Promise<T> {
  const work=queue.then(job)
  queue=work.then(()=>{},()=>{})
  return work
}
async function store(ctx:common.Context):Promise<relationalStore.RdbStore> {
  const db=await relationalStore.getRdbStore(ctx,{name:'RecordReadCache.db',securityLevel:relationalStore.SecurityLevel.S2,encrypt:true})
  await db.executeSql('CREATE TABLE IF NOT EXISTS snapshots(owner INTEGER NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL, stamp INTEGER NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(owner,scope,name))')
  return db
}
export function clearRecordSnapshots(ctx:common.Context,owner:number):Promise<void> {
  epochs.set(owner,recordCacheEpoch(owner)+1)
  return serial(async()=>{
    const db=await store(ctx)
    await db.executeSql('DELETE FROM snapshots WHERE owner=?',[owner])
  })
}
export function forgetRecordMutation(ctx:common.Context,owner:number,key:string):Promise<void> {
  epochs.set(owner,recordCacheEpoch(owner)+1)
  return serial(async()=>{
    const db=await store(ctx)
    await db.executeSql("DELETE FROM snapshots WHERE owner=? AND (name='list' OR name=?)",[owner,'detail:'+key])
  })
}
export function readRecordSnapshot(ctx:common.Context,owner:number,scope:string,name:string,ticket:number):Promise<CachedRecordValue|undefined> {
  return serial(async()=>{
    if(recordCacheEpoch(owner)!==ticket)return undefined
    const db=await store(ctx)
    // Harmony's RDB cursor window is smaller than a single original photograph.
    const rows=await db.querySql('WITH RECURSIVE pieces(pos,size) AS (SELECT 1,length(payload) FROM snapshots WHERE owner=? AND scope=? AND name=? UNION ALL SELECT pos+65536,size FROM pieces WHERE pos+65536<=size) SELECT s.stamp,substr(s.payload,p.pos,65536) FROM pieces p JOIN snapshots s ON s.owner=? AND s.scope=? AND s.name=? ORDER BY p.pos',[owner,scope,name,owner,scope,name])
    const parts:string[]=[]
    let time=0
    try{while(rows.goToNextRow()){time=rows.getLong(0);parts.push(rows.getString(1))}}finally{rows.close()}
    if(!parts.length||time>Date.now()+5000||recordCacheEpoch(owner)!==ticket)return undefined
    const value:CachedRecordValue={data:parts.join(''),time:time}
    return value
  })
}
export function writeRecordSnapshot(ctx:common.Context,owner:number,scope:string,name:string,data:string,ticket:number,
  version:string='',live?:Map<string,string>):Promise<void> {
  return serial(async()=>{
    if(recordCacheEpoch(owner)!==ticket)return
    const db=await store(ctx)
    if(recordCacheEpoch(owner)!==ticket)return
    db.beginTransaction()
    try{
    await db.executeSql('DELETE FROM snapshots WHERE owner=? AND scope<>?',[owner,scope])
    await db.executeSql('INSERT OR REPLACE INTO snapshots(owner,scope,name,stamp,version,payload) VALUES(?,?,?,?,?,?)',[owner,scope,name,Date.now(),version,data])
    if(live){
      // A complete fresh server list supersedes deleted records, old revisions, and orphan photos.
      const rows=await db.querySql("SELECT name,version FROM snapshots WHERE owner=? AND scope=? AND name<>'list'",[owner,scope])
      const stale:string[]=[]
      try{while(rows.goToNextRow()){const k=rows.getString(0);if(live.get(k)!==rows.getString(1))stale.push(k)}}finally{rows.close()}
      for(const k of stale)await db.executeSql('DELETE FROM snapshots WHERE owner=? AND scope=? AND name=?',[owner,scope,k])
    }
    if(name.startsWith('photo:')||name.startsWith('detail:')){
      const photo=name.startsWith('photo:')
      await db.executeSql('DELETE FROM snapshots WHERE rowid IN (SELECT rowid FROM snapshots WHERE owner=? AND scope=? AND name LIKE ? ORDER BY stamp DESC,rowid DESC LIMIT -1 OFFSET ?)',[owner,scope,photo?'photo:%':'detail:%',photo?24:160])
    }
    db.commit()
    }catch(e){db.rollBack();throw e as Error}
  })
}
