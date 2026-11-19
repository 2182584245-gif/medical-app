import test from 'node:test'
import assert from 'node:assert/strict'
import {cancelRecordDraft,reconcileCancelledTasks} from '../entry/src/main/ets/services/HybridRecordCancellation.js'
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222',c='33333333-3333-4333-8333-333333333333'
function host(){
  const states=new Map(),synced=new Set(),pending=new Set(),events=[]
  return {owner:7,actor:7,states,synced,pending,events,currentOwner(){return this.actor},status:id=>states.get(id)||'',
    cancelSynced:id=>synced.has(id),pendingSave:id=>pending.has(id),localCancelledIds:()=>[...states].filter(([,s])=>s==='cancelled').map(([id])=>id),
    async remoteCancel(message,id){events.push(['cancel',message,id]);return {id,status:'cancelled'}},
    async commitCancelled(id){events.push(['commit',id]);states.set(id,'cancelled');synced.add(id)}}
}
test('云端成功且回执完全匹配后才标取消；失败可以重试，不会产生假已取消',async()=>{
  const n=host();n.remoteCancel=async()=>{throw Error('offline')}
  await assert.rejects(cancelRecordDraft(n,19,a),/offline/)
  assert.equal(n.status(a),'');assert.equal(n.synced.size,0)
  n.remoteCancel=async()=>({id:b,status:'cancelled'})
  await assert.rejects(cancelRecordDraft(n,19,a),/无法核对/);assert.equal(n.status(a),'')
  n.remoteCancel=async()=>({id:a,status:'saved'})
  await assert.rejects(cancelRecordDraft(n,19,a),/无法核对/)
  n.remoteCancel=async()=>({id:a,status:'cancelled'})
  assert.deepEqual(await cancelRecordDraft(n,19,a),{id:a,status:'cancelled'})
  n.remoteCancel=async()=>{throw Error('重复请求')}
  assert.equal((await cancelRecordDraft(n,19,a)).status,'cancelled')
})
test('保存中或已保存不允许取消，账号切换后不将旧回复写到新账号',async()=>{
  const n=host();n.states.set(a,'saved')
  await assert.rejects(cancelRecordDraft(n,19,a),/保存/)
  n.states.delete(a);n.pending.add(a)
  await assert.rejects(cancelRecordDraft(n,19,a),/保存/);assert.equal(n.events.length,0)
  n.pending.clear();n.remoteCancel=async()=>{n.actor=8;return {id:a,status:'cancelled'}}
  await assert.rejects(cancelRecordDraft(n,19,a),/登录状态/);assert.equal(n.status(a),'')
})
test('历史补同步先分页收集，再取消明确的本机cancelled；saved与其他已处理状态不冒充取消',async()=>{
  const n=host();n.states.set(a,'cancelled');n.states.set(b,'saved');n.states.set(c,'decided')
  n.getTasks=async path=>{
    const limit=Number(new URL(path,'https://example.invalid').searchParams.get('limit'))
    assert.ok(Number.isInteger(limit)&&limit>=1&&limit<=40,'生产待办接口只接受1至40条分页')
    n.events.push(['page',path])
    return path.includes('cursor=')?{tasks:[{draft_id:a,assistant_message_id:19}],next_cursor:null}:
      {tasks:[{draft_id:b,assistant_message_id:21},{draft_id:c,assistant_message_id:22}],next_cursor:'last:2'}
  }
  const result=await reconcileCancelledTasks({total:3},n,'/v1/ai/record-tasks?conversation_id=8&limit=1',async()=>{n.events.push(['reload']);return {total:2}})
  assert.equal(result.total,2)
  assert.deepEqual(n.events.map(e=>e[0]),['page','page','cancel','commit','reload'])
  assert.ok(n.events[0][1].includes('conversation_id=8')&&n.events[0][1].includes('limit=40'))
  assert.ok(n.events[1][1].includes('cursor=last%3A2'))
  assert.deepEqual(n.events[2],['cancel',19,a]);assert.equal(n.status(b),'saved');assert.equal(n.status(c),'decided')
})
test('无明确旧取消时不增加网络请求；旧服务/断网失败时保持云端待办真实可见',async()=>{
  const n=host();let reloaded=false
  n.getTasks=async()=>{throw Error('不应扫描')}
  const packet={total:1,tasks:[{draft_id:a,assistant_message_id:19}]}
  assert.equal(await reconcileCancelledTasks(packet,n,'/v1/ai/record-tasks',async()=>{reloaded=true}),packet)
  assert.equal(reloaded,false)
  n.states.set(a,'cancelled');n.getTasks=async()=>packet;n.remoteCancel=async()=>{throw Error('404')}
  assert.equal(await reconcileCancelledTasks(packet,n,'/v1/ai/record-tasks',async()=>{reloaded=true}),packet)
  assert.equal(reloaded,false);assert.equal(n.cancelSynced(a),false)
})
