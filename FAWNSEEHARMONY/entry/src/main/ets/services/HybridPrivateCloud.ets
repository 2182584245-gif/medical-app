import { common } from '@kit.AbilityKit'
import { relationalStore } from '@kit.ArkData'
import { actorId, recordCacheScope } from './Cloud'

let pending:Promise<void>=Promise.resolve()
function serial<T>(work:()=>Promise<T>):Promise<T>{
  const next=pending.then(work)
  pending=next.then(()=>{},()=>{})
  return next
}
async function open(context:common.Context):Promise<relationalStore.RdbStore>{
  const db=await relationalStore.getRdbStore(context,{name:'HybridPrivate.db',securityLevel:relationalStore.SecurityLevel.S2,encrypt:true})
  await db.executeSql('CREATE TABLE IF NOT EXISTS objects(owner INTEGER NOT NULL,scope TEXT NOT NULL,name TEXT NOT NULL,part INTEGER NOT NULL,content TEXT NOT NULL,PRIMARY KEY(owner,scope,name,part))')
  return db
}
export async function clearHybridPrivate(context:common.Context,owner:number):Promise<void>{
  await serial(async()=>{
    const db=await open(context)
    await db.executeSql('DELETE FROM objects WHERE owner=?',[owner])
  })
}
export async function hybridPrivate(context:common.Context,scope:string,key:string,mode:string,value:string,guestScope:()=>string):Promise<string>{
  const owner=actorId()
  if(!scope||key.length>600||value.length>22000000)throw Error('本机资料未能验证，或保存内容超过限制。')
  const guard=():void=>{
    if(owner!==actorId()||(owner?recordCacheScope(owner):guestScope())!==scope)throw Error('账号状态已改变。')
  }
  return serial(async()=>{
    guard()
    const db=await open(context)
    guard()
    if(mode==='get'){
      const rows=await db.querySql('SELECT content FROM objects WHERE owner=? AND scope=? AND name=? ORDER BY part',[owner,scope,key])
      const parts:string[]=[]
      try{while(rows.goToNextRow())parts.push(rows.getString(0))}finally{rows.close()}
      guard();return parts.join('')
    }
    if(mode!=='set'&&mode!=='delete')throw Error('本机资料操作无效。')
    db.beginTransaction()
    try{
      await db.executeSql('DELETE FROM objects WHERE owner=? AND scope=? AND name=?',[owner,scope,key])
      if(mode==='set'){
        // Avoid splitting surrogate pairs when slicing non-ASCII draft text.
        for(let offset=0,part=0;offset<value.length;part++){
          let end=Math.min(offset+100000,value.length)
          if(end<value.length&&value.charCodeAt(end-1)>=0xd800&&value.charCodeAt(end-1)<=0xdbff)end--
          await db.executeSql('INSERT INTO objects(owner,scope,name,part,content) VALUES(?,?,?,?,?)',[owner,scope,key,part,value.slice(offset,end)])
          offset=end
        }
      }
      guard();db.commit()
    }catch(e){db.rollBack();throw e as Error}
    return value
  })
}

