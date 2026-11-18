import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

// Run the shipped component data and lifecycle methods with controlled RPC responses.
// ArkUI's declarative builders and state decorators are the only parts omitted.
const page = name => readFile(new URL('../entry/src/main/ets/pages/' + name + '.ets', import.meta.url), 'utf8')
const reads = (await page('MineReads')).replace(/^import .*$/m, '')
const mineSource = await page('Mine')
const parts = mineSource.slice(mineSource.indexOf('enum Part'), mineSource.indexOf('const menuImages'))
function component(source, end) {
  return source.slice(source.indexOf('export struct'), source.indexOf(end))
    .replace('export struct', 'export class')
    .replace(/@State\s*|@Prop\s*|@Watch\('[a-zA-Z]+'\)\s*/g, '') + '\n}'
}
const src = [
  'const actorId=()=>db.owner, currentMember=()=>({username:"member-"+db.owner});',
  'const rpc=(_service,key,...args)=>db.read(key,...args), expansion=(key,...args)=>db.read(key,...args), cloud=(key,...args)=>db.read(key,...args);',
  reads, parts, component(mineSource,'  @Builder'), component(await page('MineProfile'),'  build(){')
].join('\n').replaceAll('export class','class')
const makePages = new Function('db', stripTypeScriptTypes(src, {mode:'transform'})+'; return {MinePage,MineProfile,Part}')
const settled = () => new Promise(resolve => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((r,j)=>{resolve=r; reject=j})
  return {promise,resolve,reject}
}
function fixture() {
  const values = new Map(Object.entries({
    get_profile:{display_name:'林',ai_preferred_name:'小林'},
    get_preferences:{avatar_data:'photo-one'},
    get_service_summary:{membership:{status:'active'}},
    list_records:{items:[],total:3},list_observations:{items:[],total:0},
    get_growth:{balance:0,streak_days:0},list_points:{items:[{id:1}],total:9},
    '/v1/member/forum/discovery?limit=100&offset=0':{items:[],total:0}
  }))
  const calls = []
  const db = {owner:1,read:async (key,...args)=>{
    calls.push({key,args})
    if(!values.has(key))throw Error('unknown test RPC '+key)
    const result=values.get(key)
    if(result instanceof Error)throw result
    return await result
  }}
  return {...makePages(db),db,values,calls}
}

test('我的主页显示先到的资料；单项失败、真实零和未取得的总数分开处理，重试只调用相应接口',async()=>{
  const {MinePage,Part,values,calls}=fixture()
  const slow=deferred()
  values.set('get_preferences',slow.promise)
  values.set('list_observations',Error('网络中断'))
  values.set('get_service_summary',Error('当前账号无权读取'))
  const view=new MinePage()
  view.aboutToAppear()
  await settled()
  assert.equal(view.nameValue,'林')
  assert.equal(view.profileReady,true)
  assert.equal(view.membershipReady,false)
  assert.equal(view.readErrors.get(Part.Membership),'当前账号无权读取')
  assert.equal(view.statValue('today/goals'),'0')
  assert.equal(view.statValue('community/mine'),'0')
  assert.equal(view.statValue('records'),'—')
  assert.equal(view.records,3)
  assert.equal(view.readErrors.get(Part.Observations),'网络中断')
  const before=calls.length
  values.set('list_observations',{total:0})
  await view.loadPart(Part.Observations)
  assert.deepEqual(calls.slice(before).map(c=>c.key),['list_observations'])
  assert.equal(view.statValue('records'),'3')
  assert.equal(view.readErrors.has(Part.Observations),false)
  slow.resolve({avatar_data:'photo-one'})
  await settled()
  assert.equal(view.avatar,'photo-one')
})

test('两次重试倒序、离开页面和切账号后返回的请求均不会覆盖新内容',async()=>{
  const {MinePage,Part,values,db}=fixture()
  const old=deferred()
  values.set('get_profile',old.promise)
  const view=new MinePage()
  view.aboutToAppear()
  values.set('get_profile',{display_name:'新版本'})
  await view.loadPart(Part.Profile)
  old.resolve({display_name:'旧版本'})
  await settled()
  assert.equal(view.nameValue,'新版本')
  const late=deferred()
  values.set('get_profile',late.promise)
  const inflight=view.loadPart(Part.Profile)
  db.owner=2
  values.set('get_profile',Error('无权访问'))
  view.routeChange()
  await settled()
  late.resolve({display_name:'上一账号'})
  await inflight
  assert.equal(view.nameValue,'')
  assert.equal(view.profileReady,false)
  assert.equal(view.readErrors.get(Part.Profile),'无权访问')
  const closing=deferred()
  values.set('get_preferences',closing.promise)
  const pending=view.loadPart(Part.Avatar)
  view.aboutToDisappear()
  closing.reject(Error('已经离开'))
  await pending
  assert.equal(view.readErrors.has(Part.Avatar),false)
})

test('积分明细翻页出错时保留成功取得的余额，旧页不冒充新页，明细可以单独重试',async()=>{
  const {MinePage,Part,values,calls}=fixture()
  const view=new MinePage()
  view.route='mine/growth'
  view.aboutToAppear()
  await settled()
  assert.equal(view.growth.balance,0)
  assert.equal(view.ledger[0].id,1)
  view.page=1
  values.set('list_points',Error('积分明细离线'))
  await view.loadPart(Part.Ledger)
  assert.equal(view.growth.balance,0)
  assert.deepEqual(view.ledger,[])
  assert.equal(view.total,-1)
  assert.equal(view.readErrors.get(Part.Ledger),'积分明细离线')
  values.set('list_points',{items:[{id:9}],total:9})
  await view.loadPart(Part.Ledger)
  assert.equal(view.ledger[0].id,9)
  assert.equal(calls.filter(c=>c.key==='get_growth').length,1)
  assert.equal(calls.at(-1).args[0].offset,8)
})

test('头像失败后重试不会覆盖已编辑的姓名；资料未加载时禁止空表单保存',async()=>{
  const {MineProfile,values,calls}=fixture()
  values.set('get_preferences',Error('头像离线'))
  const profile=new MineProfile()
  profile.aboutToAppear()
  await settled()
  assert.equal(profile.ready,true)
  assert.equal(profile.avatarError,'头像离线')
  profile.nameValue='正在修改的姓名'
  values.set('get_preferences',{avatar_data:'新头像'})
  await profile.loadAvatar()
  assert.equal(profile.nameValue,'正在修改的姓名')
  assert.equal(profile.avatar,'新头像')
  assert.equal(profile.avatarError,'')
  values.set('get_profile',Error('暂时离线'))
  const empty=new MineProfile()
  empty.aboutToAppear()
  await settled()
  assert.equal(empty.avatarReady,true)
  assert.equal(empty.ready,false)
  await empty.save()
  assert.equal(calls.some(c=>c.key==='save_profile'),false)
})
