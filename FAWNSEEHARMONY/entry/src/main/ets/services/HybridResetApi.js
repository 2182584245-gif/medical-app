import {failure} from './HybridCloudApi.js'
import {settleJournal} from './HybridJournalApi.js'

let active=null
export async function pendingReset(n) {
  const raw=await n.resetJournal()
  if(!raw)return null
  const r=JSON.parse(raw)
  if(!Number.isSafeInteger(r.owner)||r.owner<1||!r.op||!['submitted','confirmed'].includes(r.phase))throw failure('account_reset_pending','清空任务待核对。')
  return r
}
export function resetBootstrap(marker,user){
  return {user:user&&user.id===marker.owner?{id:marker.owner,username:user.username}:{id:marker.owner,username:marker.username},guest:null,app:{name:'凡思',transport:'arkweb'},cloud:{configured:true,authenticated:false},account_data_reset_pending:true,account_data_reset_local_pending:marker.phase==='confirmed',account_data_reset_reauthentication_required:marker.reauth===true}
}
export function resetAction(action,p,n,marker){
  const who=marker?.owner||n.owner()
  if(!Number.isSafeInteger(who)||who<=0||p._fawnsee_actor_id!==who)throw failure('account_changed','请继续验证正在清理的原账号。')
  if(active)return active
  const work=async()=>{
    if(action==='account.reset.phone.send'){
      if(!marker?.reauth||typeof p.phone!=='string')throw failure('account_reset_pending','请先核对当前清理任务。')
      return n.sms({phone:p.phone,purpose:'login'})
    }
    let item=marker
    if(!item){
      if(action!=='account.reset'||p.confirmation!=='清空账号内全部数据')throw failure('validation','请核对清空确认文字。')
      item={owner:who,username:n.user().username||'',op:n.id(),phase:'submitted',reauth:false}
      await n.resetJournal(JSON.stringify(item))
    }
    if(action==='account.reset.reauthorize'){
      if(!item.reauth)throw failure('validation','当前任务无需重新登录。')
      if(typeof p.username==='string'&&typeof p.password==='string'){
        await n.reauthorize('/v1/auth/login',{username:p.username,password:p.password},who)
      }else if(typeof p.phone==='string'&&typeof p.code==='string'&&typeof p.challenge_id==='string'){
        await n.reauthorize('/v1/auth/phone/login',{phone:p.phone,code:p.code,challenge_id:p.challenge_id},who)
      }else throw failure('validation','请使用原账号的密码或短信验证码。')
      if(n.owner()!==who)throw failure('account_changed','请使用正在清理的原账号。')
      item.reauth=false;await n.resetJournal(JSON.stringify(item))
    }
    if(item.phase==='submitted'){
      await settleJournal(who)
      try{
        if(n.owner()!==who)throw failure('not_authenticated','需要验证原账号。')
        const result=await n.rest('/v1/auth/account/reset','POST',{confirmation:'清空账号内全部数据',request_id:item.op},item.op)
        if(result.reset!==true||result.local_purge_required!==true||result.request_id!==item.op)throw failure('account_reset_pending','云端清空结果尚未确认，请使用本次任务重试。')
      }catch(e){
        if(e.status===401||e.code==='not_authenticated'||n.owner()!==who){
          item.reauth=true;await n.resetJournal(JSON.stringify(item));throw failure('account_reset_reauthentication_required','请验证正在清理的原账号，然后继续。')
        }
        throw failure('account_reset_pending',e.message||'清空结果待核对，请重试本次任务。')
      }
      item.phase='confirmed';item.reauth=false;await n.resetJournal(JSON.stringify(item))
    }
    try{
      await n.purgeLocal(who)
      if(n.owner()===who)await n.clear()
      n.guestIdentity(true)
      await n.resetJournal('')
    }catch(e){throw failure('local_reset_failed','云端已清空，请继续完成本机清理。')}
    return {reset:true,local_purged:true,reauthentication_required:true}
  }
  active=work().finally(()=>{active=null});return active
}

