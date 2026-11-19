import { failure } from './HybridCloudApi.js'

// Only the two user-owned journal resources support offline edits. Other requests keep their online contracts.
const writes = new Set(['health.records.add','expansion.records.update','expansion.records.delete','expansion.observations.create','expansion.observations.update','expansion.observations.delete'])
const reads = new Set(['expansion.records.list','expansion.records.get','expansion.records.revisions','expansion.observations.list','expansion.observations.get','member.sync'])
const localActions=new Set(['local149.pending','local149.sync','local149.resolve','local149.status'])
const jobs=new Map(),wakeups=new Map()
export const journalAction=action=>writes.has(action)||reads.has(action)||localActions.has(action)
const key='records:outbox:v1'
function init(){return {pending:[],receipts:{},aliases:{}}}
const read=async n=>JSON.parse(await n.scopedFile(key,'get','')||'null')||init()
const commit=(n,queue)=>n.scopedFile(key,'set',JSON.stringify(queue))
const resource=a=>a.includes('observations')?'observations':'life_records'
const field=a=>resource(a)==='observations'?'observation_id':'record_id'
const create=a=>a==='health.records.add'||a==='expansion.observations.create'
const pendingId=op=>-Math.max(1,parseInt(op.replace(/-/g,'').slice(0,13),16))
const isDel=a=>a.endsWith('.delete')
const copy=x=>JSON.parse(JSON.stringify(x))
function fingerprint(value) {
  if(Array.isArray(value))return '['+value.map(fingerprint).join(',')+']'
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+fingerprint(value[k])).join(',')+'}'
  return JSON.stringify(value)
}
function ordered(owner,work){
  const previous=jobs.get(owner)||Promise.resolve()
  const pending=previous.catch(()=>{}).then(work)
  jobs.set(owner,pending)
  return pending.finally(()=>{if(jobs.get(owner)===pending)jobs.delete(owner)})
}
export async function settleJournal(owner){const work=jobs.get(owner);if(work)await work.catch(()=>{})}
function guard(n,owner,scope) {
  if(n.owner()!==owner||!scope||n.scope()!==scope)throw failure('account_changed','当前离线授权已改变，请联网核对账号。')
}
function patch(row,item){
  if(isDel(item.action))return null
  const b=item.payload
  const changes=item.action==='expansion.records.update'?b.changes:b
  const out={...row,...changes,id:item.target,version:'local:'+item.id,local_only:true,local_id:item.id,sync_status:item.state}
  delete out.record_id;delete out.observation_id;delete out.base_version;delete out.reason
  if(item.target<0)out.local_number=item.target
  return out
}
function queuedResult(item){return {...(patch({},item)||{}),id:item.target,local_number:item.target<0?item.target:undefined,local_id:item.id,local_only:true,status:item.state,version:'local:'+item.id}}
function queueView(q){
  return q.pending.map(i=>({operation_id:i.id,action:i.action,resource:resource(i.action),status:i.state==='pending'?'queued':i.state,created_at:i.created_at,last_error:i.error,local_number:i.target<0?i.target:undefined}))
}
function trimReceipts(q){
  const keys=Object.keys(q.receipts)
  for(const id of keys.slice(0,Math.max(0,keys.length-150)))if(!q.pending.some(i=>i.payload.base_version==='local:'+id))delete q.receipts[id]
  const ids=Object.keys(q.aliases)
  for(const i of ids.slice(0,Math.max(0,ids.length-200)))if(!q.pending.some(x=>String(x.target)===i))delete q.aliases[i]
}
function applyPending(list,items,kind,filter){
  const byId=new Map(list.map(row=>[row.id,row]))
  for(const item of items){
    if(resource(item.action)!==kind)continue
    if(isDel(item.action)){byId.delete(item.target);continue}
    const old=byId.get(item.target)
    if(!old&&!create(item.action))continue
    const updated=patch(old||{created_at:item.created_at,updated_at:item.created_at},item)
    if(filter.category&&updated.category!==filter.category || filter.kind&&updated.kind!==filter.kind)continue
    byId.set(item.target,updated)
  }
  return [...byId.values()].sort((a,b)=>String(b.occurred_at||b.measured_at||'').localeCompare(String(a.occurred_at||a.measured_at||''))||b.id-a.id)
}
function cachedProjection(list,q,kind,b,stamp){
  const rows=new Map(list.map(row=>[row.id,row]))
  for(const receipt of Object.values(q.receipts)){
    const effect=receipt.effect
    if(!effect||receipt.stamp<=stamp||resource(effect.action)!==kind)continue
    if(isDel(effect.action)){rows.delete(receipt.id);continue}
    const old=rows.get(receipt.id)
    if(!old&&!create(effect.action))continue
    const row={...old,...effect.changes,id:receipt.id,version:receipt.version||effect.changes.version||old?.version}
    if(b.category&&row.category!==b.category||b.kind&&row.kind!==b.kind){rows.delete(receipt.id);continue}
    rows.set(row.id,row)
  }
  return [...rows.values()]
}
async function findCachedRecord(n,kind,id,q){
  const list=kind==='observations'?'expansion.observations.list':'expansion.records.list'
  let offset=0
  for(let i=0;i<1000;i++){
    const raw=await n.recordCache(fingerprint([list,{limit:100,offset}]),'get','')
    if(!raw)break
    const saved=JSON.parse(raw),page=saved.fmt==='hybrid-record-v1'?saved.data:saved,rows=page.items||[]
    const row=rows.find(r=>r.id===id)
    if(row)return {row,stamp:saved.at||0}
    offset+=rows.length;if(!rows.length||offset>=page.total)break
  }
  const created=Object.values(q.receipts).find(r=>r.id===id&&r.effect&&resource(r.effect.action)===kind&&create(r.effect.action))
  return created?{row:{...created.effect.changes,id,version:created.version},stamp:created.stamp}:null
}
async function refreshList(n,a,b,run,q,owner,scope){
  const t=fingerprint([a,b]),kind=resource(a)
  let data,fromCache=false,stamp=0
  try{
    data=await run(a,b,n.id())
    guard(n,owner,scope)
    await n.recordCache(t,'set',JSON.stringify({fmt:'hybrid-record-v1',at:Date.now(),data}))
  }catch(e){
    if(!n.networkError(e)&&![502,503,504].includes(e.status))throw e
    guard(n,owner,scope)
    const raw=await n.recordCache(t,'get','')
    fromCache=true
    if(raw){const saved=JSON.parse(raw);data=saved.fmt==='hybrid-record-v1'?saved.data:saved;stamp=saved.at||0}
    else if(a.endsWith('.list') && (q.pending.some(i=>resource(i.action)===kind)||await n.recordCache(fingerprint([a,{...b,offset:0}]),'get','')))data={items:[],total:0,limit:b.limit||100,offset:b.offset||0,local_only:true}
    else if(a.endsWith('.get')){
      const cached=await findCachedRecord(n,kind,b[field(a)],q)
      if(!cached)throw failure('offline_unavailable','本机尚未缓存这条记录，请联网后重新打开。')
      data=cached.row;stamp=cached.stamp
    }
    else throw failure('offline_unavailable','本机尚未缓存这部分资料，请联网后重新打开。')
  }
  if(a==='member.sync'){
    if(q.pending.length)return {...data,revision:String(data.revision||'')+'_'+q.pending.map(i=>i.id+i.state).join('.'),local_record_page_limit:100}
    return data
  }
  if(a.endsWith('.revisions'))return data
  if(a.endsWith('.list')){
    if(fromCache){
      // The frontend offsets include local inserts/deletes. Repage cached cloud rows before applying the local changes.
      const size=b.limit||100,all=[];let offset=0
      for(let i=0;i<1000;i++){
        const raw=await n.recordCache(fingerprint([a,{...b,offset}]),'get','')
        if(!raw)break
        const saved=JSON.parse(raw),page=saved.fmt==='hybrid-record-v1'?saved.data:saved
        const rows=page.items||[];all.push(...rows)
        if(!rows.length||all.length>=page.total||rows.length<size)break
        offset+=rows.length
      }
      const base=cachedProjection(all,q,kind,b,stamp)
      const projected=applyPending(base,q.pending,kind,b)
      return {...data,items:projected.slice(b.offset||0,(b.offset||0)+size),total:projected.length,local_only:true}
    }
    if(!q.pending.length)return data
    // A paged cloud response is authoritative for its range. Include offline creations only on page one.
    const subset=q.pending.filter(i=>!create(i.action)||!b.offset)
    const items=applyPending(data.items||[],subset,kind,b)
    return {...data,items,total:Math.max(items.length,(data.total||0)+items.length-(data.items||[]).length)}
  }
  const id=b[field(a)]
  let row=fromCache?cachedProjection([data],q,kind,{},stamp).find(r=>r.id===id):data
  for(const item of q.pending){if(item.target===id&&resource(item.action)===kind)row=patch(row,item)}
  if(!row)throw failure('not_found','这条记录已在本机删除。')
  return row
}
function schedule(n,run,owner,scope,ms=10000){
  if(wakeups.has(owner))return
  const handle=setTimeout(()=>{
    wakeups.delete(owner)
    if(n.owner()!==owner||n.scope()!==scope)return
    ordered(owner,async()=>{const q=await read(n);if(!q.pending.length)return;await drain(n,run,q,owner,scope);if(q.pending.some(i=>i.state==='pending'))schedule(n,run,owner,scope,Math.min(180000,ms*2))}).catch(()=>{})
  },ms)
  if(handle?.unref)handle.unref()
  wakeups.set(owner,handle)
}
async function drain(n,run,q,owner,scope){
  for(const i of [...q.pending]){
    guard(n,owner,scope)
    if(!n.allowed('health')||await n.resetJournal())return
    if(i.state!=='pending')continue
    if(q.pending.some(prev=>prev!==i&&prev.target===i.target&&q.pending.indexOf(prev)<q.pending.indexOf(i)))continue
    let b=copy(i.payload)
    const targetKey=field(i.action)
    if(!create(i.action)){
      if(i.target<0 && !q.aliases[i.target])continue
      if(i.target<0)b[targetKey]=q.aliases[i.target]
      if(typeof b.base_version==='string'&&b.base_version.startsWith('local:')){
        const prev=q.receipts[b.base_version.slice(6)]
        if(!prev?.version)continue
        b.base_version=prev.version
      }
    }
    try{
      const answer=await run(i.action,b,i.id)
      guard(n,owner,scope)
      const id=typeof answer==='number'?answer:answer?.id||b[targetKey]
      if(!Number.isSafeInteger(id)||id<1)throw failure('protocol','云端返回的记录编号无效。')
      let version=typeof answer==='object'?answer?.version:null
      if(!version&&!isDel(i.action) && q.pending.some(next=>next.payload.base_version==='local:'+i.id)){
        version=(await run(resource(i.action)==='observations'?'expansion.observations.get':'expansion.records.get',{[targetKey]:id},n.id())).version
      }
      const resultFields=answer&&typeof answer==='object'?answer:{}
      const changes={...(i.action==='expansion.records.update'?b.changes:b),...resultFields}
      for(const field of ['base_version','reason','record_id','observation_id'])delete changes[field]
      q.receipts[i.id]={id,version:version||null,answer:typeof answer==='object'?{...answer}:answer,fingerprint:i.fingerprint,stamp:Date.now(),effect:{action:i.action,changes}}
      if(create(i.action))q.aliases[i.target]=id
      q.pending=q.pending.filter(item=>item!==i);trimReceipts(q);await commit(n,q)
      if(typeof n.reminders==='function')await n.reminders()
    }catch(e){
      if(n.networkError(e))return
      if(e.status===401||e.code==='not_authenticated'||e.code==='account_changed')throw e
      i.state=e.status===409||e.code==='conflict'?'conflict':'rejected'
      i.error=e.message||'云端未接受此项更改。'
      await commit(n,q)
    }
  }
}
export async function runJournal(a,b,op,n,owner,run){
  const scope=n.scope()
  // Without a valid offline lease, online requests can work but private snapshots must stay locked.
  if(!scope){
    if(localActions.has(a))throw failure('offline_unavailable','请联网重新验证账号后查看本机同步状态。')
    return run(a,b,op)
  }
  return ordered(owner,async()=>{
    guard(n,owner,scope);const q=await read(n)
    if(a==='local149.pending')return queueView(q)
    if(a==='local149.status')return {source:'local',pending_count:q.pending.length,offline_ready:true}
    if(a==='local149.resolve'){
      const item=q.pending.find(i=>i.id===b.operation_id)
      if(!item)throw failure('not_found','未找到本机同步变更。')
      if(b.decision==='keep_cloud'){
        if(q.pending.some(i=>i!==item&&(i.target===item.target&&create(item.action)||i.payload.base_version==='local:'+item.id)))throw failure('conflict','请先处理依赖这项更改的本机记录。')
        q.pending=q.pending.filter(i=>i!==item);await commit(n,q)
      }else if(b.decision==='retry_local'){
        if(item.state!=='pending'&&!create(item.action)){
          const f=field(item.action),id=item.target<0?q.aliases[item.target]:item.target
          if(!id)throw failure('conflict','请先同步本机新增的记录。')
          const fresh=await run(resource(item.action)==='observations'?'expansion.observations.get':'expansion.records.get',{[f]:id},n.id())
          if(!fresh.version)throw failure('conflict','云端版本无法确认。')
          const oldId=item.id;item.payload.base_version=fresh.version;item.id=n.id()
          for(const other of q.pending)if(other.payload.base_version==='local:'+oldId)other.payload.base_version='local:'+item.id
        }
        item.state='pending';item.error='';await commit(n,q);await drain(n,run,q,owner,scope)
      }else throw failure('validation','请选择云端或本机变更。')
      return {pending:q.pending.length,status:item.state}
    }
    if(a==='local149.sync'){await drain(n,run,q,owner,scope);return {scheduled:q.pending.some(i=>i.state==='pending'),pending_count:q.pending.length}}
    if(writes.has(a)){
      const fp=await n.digest(fingerprint([a,b])),prior=q.receipts[op]
      if(prior){if(prior.fingerprint!==fp)throw failure('conflict','相同操作编号已用于不同内容。');return prior.answer}
      let item=q.pending.find(i=>i.id===op)
      if(item&&item.fingerprint!==fp)throw failure('conflict','相同操作编号已用于不同内容。')
      if(!item){
        if(q.pending.length>=300)throw failure('limit','本机待同步记录较多，请联网完成同步后继续。')
        item={id:op,action:a,payload:copy(b),fingerprint:fp,target:create(a)?pendingId(op):b[field(a)],created_at:new Date().toISOString(),state:'pending'}
        q.pending.push(item);await commit(n,q)
      }
      await drain(n,run,q,owner,scope)
      if(q.receipts[op])return q.receipts[op].answer
      if(item.state!=='pending')throw failure(item.state,item.error)
      schedule(n,run,owner,scope)
      if(typeof n.localNotice==='function')n.localNotice()
      return queuedResult(item)
    }
    if(q.pending.length){await drain(n,run,q,owner,scope);if(q.pending.some(i=>i.state==='pending'))schedule(n,run,owner,scope)}
    if(a.endsWith('.get')){
      let id=b[field(a)]
      if(id<0){
        const members=q.pending.filter(i=>resource(i.action)===resource(a)&&i.target===id)
        if(members.length){let row={};for(const item of members)row=patch(row,item);if(row)return row;throw failure('not_found','本机记录已删除。')}
        if(q.aliases[id])b={...b,[field(a)]:q.aliases[id]}
      }
    }
    return refreshList(n,a,b,run,q,owner,scope)
  })
}

