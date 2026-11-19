const uuid=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
function bound(n){if(!n.owner||n.owner!==n.currentOwner())throw Error('登录状态已变更。')}
export async function cancelRecordDraft(n,message,id){
  bound(n)
  if(!Number.isSafeInteger(message)||message<1||typeof id!=='string'||!uuid.test(id))throw Error('草稿标识无效。')
  if(n.status(id)==='saved'||n.pendingSave(id))throw Error('这份草稿已经保存或保存结果尚待核对。')
  if(n.status(id)==='cancelled'&&n.cancelSynced(id))return {id,status:'cancelled'}
  const result=await n.remoteCancel(message,id)
  bound(n)
  if(result?.id!==id||result?.status!=='cancelled')throw Error('取消结果暂时无法核对。')
  if(n.status(id)==='saved'||n.pendingSave(id))throw Error('草稿保存状态已变化，请重新核对。')
  await n.commitCancelled(id)
  bound(n)
  return result
}
function scanPath(path,cursor){
  const [base,query='']=path.split('?')
  const parts=query.split('&').filter(p=>p&&!/^(?:limit|cursor)=/.test(p))
  parts.push('limit=40')
  if(cursor)parts.push('cursor='+encodeURIComponent(cursor))
  return base+'?'+parts.join('&')
}
export async function reconcileCancelledTasks(packet,n,path,reload){
  bound(n)
  const cancelled=new Set(n.localCancelledIds().filter(id=>uuid.test(id)&&n.status(id)==='cancelled'&&!n.cancelSynced(id)&&!n.pendingSave(id)))
  if(!cancelled.size)return packet
  const matches=[],seen=new Set()
  let cursor=''
  // Collect before cancelling so changing the task set cannot invalidate a page cursor.
  for(let pages=0;pages<32;pages++){
    const page=await n.getTasks(scanPath(path,cursor));bound(n)
    if(!Array.isArray(page?.tasks))throw Error('待办列表暂时无法核对。')
    for(const task of page.tasks){
      if(cancelled.has(task.draft_id)&&Number.isSafeInteger(task.assistant_message_id)&&task.assistant_message_id>0){
        matches.push(task);cancelled.delete(task.draft_id)
      }
    }
    const next=page.next_cursor
    if(!cancelled.size||!next||typeof next!=='string'||seen.has(next))break
    seen.add(next);cursor=next
  }
  let changed=false
  for(const task of matches){
    bound(n)
    try{await cancelRecordDraft(n,task.assistant_message_id,task.draft_id);changed=true}
    catch(error){bound(n);/* Keep the authoritative task visible when cancellation could not be confirmed. */}
  }
  bound(n)
  const result=changed?await reload():packet
  bound(n)
  return result
}
