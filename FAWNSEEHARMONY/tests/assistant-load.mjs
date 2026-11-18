import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

// Exercise the actual ArkTS page methods. ArkUI view construction, its storage
// decorator and the cloud/OS I/O are replaced for deterministic fault injection.
const source = await readFile(new URL('../entry/src/main/ets/pages/Assistant.ets', import.meta.url), 'utf8')
const component = source.slice(source.indexOf('export struct AssistantPage'), source.indexOf('  @Builder'))
  .replace('export struct AssistantPage', 'class AssistantPage')
  .replace(/@State\s*|@Prop\s*|@(?:Watch|StorageProp)\('[^']+'\)\s*/g, '') + '\n}'
const makeView = new Function('io', `
const {actorId,rpc,cloud,decisionStore,loadMessages,sendChat,checkAnswer,ChatOperation}=io;
const array=(data,key)=>data[key]||[], noteError=e=>e.message;
const stringValue=(data,key,fallback='')=>String(data[key]??fallback);
const KeyboardAvoidMode={OFFSET:0,RESIZE:1}, Edge={Bottom:0};
class Scroller { scrollEdge() {} }
` + stripTypeScriptTypes(component, {mode:'transform'}) + ';return new AssistantPage()')

const tick = () => new Promise(resolve => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((yes,no)=>{resolve=yes;reject=no})
  return {promise,resolve,reject}
}
function packet(content, before=0) {
  return {messages:[{id:1,role:'assistant',content}],has_more:before>0,next_cursor:before}
}
function setup() {
  const saved = new Map()
  const values = new Map(Object.entries({
    get_preferences:{mobile_ai_web_enabled:false,mobile_ai_personal_enabled:false},
    list_conversations:[{id:21}],ensure_greeting:{},'messages:21:0':packet('本次会话')
  }))
  const calls=[], sent=[]
  const owner={id:46}
  async function value(key) {
    calls.push(key)
    assert.ok(values.has(key),'Unexpected request '+key)
    const result=values.get(key)
    if(result instanceof Error)throw result
    return result
  }
  const store = {
    getSync:(key,fallback)=>saved.has(key)?saved.get(key):fallback,
    putSync:(key,item)=>saved.set(key,item), deleteSync:key=>saved.delete(key), flush:async()=>{}
  }
  const page=makeView({
    actorId:()=>owner.id, rpc:(_kind,method)=>value(method), cloud:async()=>({total:0}),
    decisionStore:()=>store, loadMessages:(conv,cursor)=>value('messages:'+conv+':'+cursor),
    checkAnswer:()=>value('checkAnswer'),
    ChatOperation:class {
      constructor(input,image,task,conversation,file) {Object.assign(this,{input,image,task,conversation,file,id:'check-test-op'})}
    },
    sendChat:async (operation,settings)=>{sent.push({operation,settings})}
  })
  page.getUIContext=()=>({getHostContext:()=>({}),getKeyboardAvoidMode:()=>0,setKeyboardAvoidMode:()=>{}})
  page.owner=owner.id
  return {page,values,saved,calls,sent,owner}
}

test('设置读取延迟不阻止会话和消息显示；会话列表失败时仍能独立取得偏好',async()=>{
  const {page,values}=setup()
  const slow=deferred()
  values.set('get_preferences',slow.promise)
  await page.init()
  assert.equal(page.conversation,21)
  assert.equal(page.messages[0].content,'本次会话')
  assert.equal(page.preferencesReady,false)
  slow.resolve({mobile_ai_web_enabled:false})
  await tick()
  assert.equal(page.preferencesReady,true)

  const other=setup()
  other.values.set('list_conversations',Error('会话列表离线'))
  await other.page.init()
  assert.equal(other.page.preferencesReady,true)
  assert.equal(other.page.error,'会话列表离线')
  assert.equal(other.page.conversation,0)
})

test('缺少设置可以先写草稿，发送会先重试偏好；只有读取成功后才交给聊天接口',async()=>{
  const {page,values,sent,saved}=setup()
  values.set('get_preferences',Error('网络异常'))
  await page.init()
  page.input='稍后发送这段话'
  page.saveInput()
  assert.equal(saved.get('draft_21'),page.input)
  await page.send()
  assert.equal(sent.length,0)
  assert.equal(page.preferencesReady,false)
  assert.ok(page.preferencesError)
  assert.equal(page.input,'稍后发送这段话')
  const safe={mobile_ai_web_enabled:false,mobile_ai_personal_enabled:false}
  values.set('get_preferences',safe)
  await page.send()
  assert.equal(sent.length,1)
  assert.deepEqual(sent[0].settings,safe)
  assert.equal(sent[0].operation.input,'稍后发送这段话')
  assert.equal(page.preferencesError,'')
})

test('欢迎语服务出错仍能读取已有消息和继续输入',async()=>{
  const {page,values,calls}=setup()
  values.set('ensure_greeting',Error('欢迎语暂不可用'))
  await page.init()
  assert.ok(calls.indexOf('messages:21:0')>calls.indexOf('ensure_greeting'))
  assert.equal(page.messages[0].content,'本次会话')
  assert.equal(page.historyError,'')
  assert.equal(page.error,'')
  assert.equal(page.pending,false)
})

test('历史失败原地重试、翻页失败重试同一游标，文字和附件始终保留',async()=>{
  const {page,values,calls}=setup()
  values.set('messages:21:0',Error('历史读取失败'))
  await page.init()
  assert.equal(page.historyPagingFailed,false)
  assert.ok(page.historyError)
  const file={name:'餐照.jpg',mime:'image/jpeg',content:'test'}
  page.input='这份草稿尚未发送';page.file=file;page.photo='data:image/jpeg;base64,test'
  let start=calls.length
  values.set('messages:21:0',packet('当前页',9))
  await page.read(page.historyPagingFailed)
  assert.deepEqual(calls.slice(start),['messages:21:0'])
  assert.equal(page.historyError,'')
  values.set('messages:21:9',Error('获取更早记录超时'))
  await page.read(true)
  assert.equal(page.historyPagingFailed,true)
  assert.equal(page.messages[0].content,'当前页')
  start=calls.length
  values.set('messages:21:9',packet('更早的消息'))
  await page.read(page.historyPagingFailed)
  assert.deepEqual(calls.slice(start),['messages:21:9'])
  assert.deepEqual(page.messages.map(row=>row.content),['更早的消息','当前页'])
  assert.equal(page.historyError,'')
  assert.equal(page.input,'这份草稿尚未发送')
  assert.equal(page.file,file)
  assert.equal(page.photo,'data:image/jpeg;base64,test')
})

test('慢的历史读取以及等待设置后才触发的发送，均不能写入后来选择的会话',async()=>{
  const {page,values,sent}=setup()
  const old=deferred(), prefs=deferred()
  values.set('messages:21:0',old.promise)
  values.set('get_preferences',prefs.promise)
  const boot=page.init()
  await tick()
  page.input='只发给旧会话'
  const sending=page.send()
  values.set('messages:22:0',packet('新会话'))
  await page.openChat(22)
  old.resolve(packet('过时的旧会话消息'))
  prefs.resolve({mobile_ai_web_enabled:false})
  await Promise.all([boot,sending])
  assert.equal(page.conversation,22)
  assert.equal(page.messages[0].content,'新会话')
  assert.equal(sent.length,0)
  assert.equal(page.input,'')
  assert.equal(page.historyError,'')
})

test('启动时先输入的文字和选择的附件保留在选中的会话，不会被旧的本地草稿覆盖',async()=>{
  const {page,values,saved}=setup()
  const sessions=deferred()
  values.set('list_conversations',sessions.promise)
  saved.set('draft_21','早前本地草稿')
  const boot=page.init()
  const file={name:'补充文字.txt',mime:'text/plain'}
  page.input='刚输入的新草稿';page.saveInput();page.file=file
  sessions.resolve([{id:21}])
  await boot
  assert.equal(page.conversation,21)
  assert.equal(page.input,'刚输入的新草稿')
  assert.equal(page.file,file)
  assert.equal(saved.get('draft_21'),'刚输入的新草稿')
})

test('恢复旧发送操作成功时保留本次新输入或无法比对的旧草稿，已确认相同的原稿才清理',async()=>{
  const first=setup()
  const sessions=deferred()
  first.saved.set('pending_21','old-op')
  first.saved.set('pendingText_21','相同文字')
  first.values.set('list_conversations',sessions.promise)
  first.values.set('checkAnswer','complete')
  const boot=first.page.init()
  const photo='data:image/jpeg;base64,new-photo'
  first.page.input='相同文字';first.page.photo=photo
  sessions.resolve([{id:21}])
  await boot
  assert.equal(first.page.input,'相同文字')
  assert.equal(first.page.photo,photo)
  assert.equal(first.saved.has('pending_21'),false)

  for(const hasOriginal of [false,true]){
    const fixture=setup()
    fixture.values.set('checkAnswer','complete')
    fixture.saved.set('draft_21','留在设备上的文字')
    fixture.saved.set('pending_21','old-op')
    if(hasOriginal)fixture.saved.set('pendingText_21','留在设备上的文字')
    await fixture.page.init()
    assert.equal(fixture.page.recoveryId,'')
    assert.equal(fixture.page.input,hasOriginal?'':'留在设备上的文字')
    assert.equal(fixture.saved.has('draft_21'),!hasOriginal)
  }
})
