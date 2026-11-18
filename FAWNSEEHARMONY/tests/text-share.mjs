import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

const root = new URL('../entry/src/main/ets/', import.meta.url)
const source = await readFile(new URL('services/TextShare.ets', root), 'utf8')
const serviceCode = stripTypeScriptTypes(source.replace(/^import .*$/gm, '').replace('export async', 'async'), {mode:'transform'})

function shareKit() {
  const listeners = new Map()
  const ctx = {}
  let shown
  let showError
  class SharedData {
    constructor(record) { this.record = record }
  }
  class ShareController {
    constructor(data) { this.data = data }
    on(event, handler) { listeners.set(event, handler) }
    off(event) { listeners.delete(event) }
    show(context, options) {
      assert.equal(context, ctx)
      assert.equal(options.selectionMode, 0)
      shown = this.data.record
      return showError ? Promise.reject(showError) : Promise.resolve()
    }
  }
  const system = {SharedData, ShareController, SelectionMode:{SINGLE:0}}
  const share = Function('uniformTypeDescriptor', 'systemShare', serviceCode+'\nreturn shareText;')(
    {UniformDataType:{PLAIN_TEXT:'general.plain-text'}}, system)
  return {ctx, share, listeners, shown:() => shown, rejectShow:error => {showError = error}}
}

test('opening a system sheet sends only the selected title and plain text and waits for a result', async () => {
  const kit = shareKit()
  let settled = false
  const result = kit.share(kit.ctx, '分享标题', '标题\n正文').then(value => {settled = true; return value})
  await Promise.resolve()
  assert.equal(settled, false)
  assert.deepEqual(kit.shown(), {utd:'general.plain-text', title:'分享标题', content:'标题\n正文'})
  kit.listeners.get('dismiss')()
  assert.equal(await result, false)
  assert.equal(kit.listeners.size, 0)
})

test('a completion event counts once, and the dismissal after it does not change the result', async () => {
  const kit = shareKit()
  const result = kit.share(kit.ctx, '分享标题', '正文')
  kit.listeners.get('shareCompleted')({targetAbilityInfo:{name:'example-target'}})
  kit.listeners.get('dismiss')()
  assert.equal(await result, true)
  assert.equal(kit.listeners.size, 0)
})

test('a system panel error is visible to the caller and all listeners are released', async () => {
  const kit = shareKit()
  const error = new Error('system panel unavailable')
  kit.rejectShow(error)
  await assert.rejects(kit.share(kit.ctx, '分享标题', '正文'), error)
  assert.equal(kit.listeners.size, 0)
})

// Exercise the article action itself: it must never write a share before local confirmation.
const articleSource = await readFile(new URL('pages/CommunityKnowledge.ets', root), 'utf8')
const method = articleSource.slice(articleSource.indexOf('  private async shareArticle('), articleSource.indexOf('  private async saveFood('))
const articleCode = stripTypeScriptTypes('class ArticlePage {\n'+method+'\n}', {mode:'transform'})
function articleAction(choice) {
  const writes = [], toasts = [], copied = []
  const state = {owner:5}
  const db = async (path, method, body) => {writes.push({path,body}); return {data_generation:2,status:'applied'}}
  const share = async (_, title, text) => {
    assert.equal(title, '一份食谱')
    assert.equal(text, '一份食谱\nhttps://example.com/recipe')
    if (choice==='switch') state.owner=6
    return choice!=='cancel'
  }
  const board={getSystemPasteboard:()=>({setData:async d=>copied.push(d)}),createData:(_,s)=>s}
  const pageClass = Function('cloud','actorId','uuid','stringValue','safeTarget','blockText','shareText','pasteboard',
    articleCode+'\nreturn ArticlePage')(db,()=>state.owner,()=>'op-1',(row,key)=>row[key]||'',v=>v,()=> '正文章节',share,board)
  const page = new pageClass()
  page.article={title:'一份食谱',source_url:'https://example.com/recipe'}
  page.contentId=()=>'recipe-12'
  page.ticket=1
  page.getUIContext=()=>({getHostContext:()=>({}),getPromptAction:()=>({showToast:({message})=>toasts.push(message)})})
  return {page,writes,toasts,copied}
}

test('article cancellation and account changes produce no server calls', async () => {
  for(const outcome of ['cancel','switch']){
    const {page,writes,toasts}=articleAction(outcome)
    await page.shareArticle(false)
    assert.equal(writes.length, 0)
    assert.equal(toasts.length, 0)
  }
})

test('confirmed article sharing records the captured article once, and explicit copy still copies its excerpt', async () => {
  for(const copy of [false,true]){
    const {page,writes,copied,toasts}=articleAction('select')
    await page.shareArticle(copy)
    assert.equal(writes.length, 2)
    assert.deepEqual(writes[1].body, {operation_id:'op-1',action:'reading.share',payload:{article_id:'recipe-12'},data_generation:2})
    assert.deepEqual(copied,copy?['一份食谱\n正文章节']:[])
    assert.deepEqual(toasts,copy?['已复制文章内容']:[])
  }
})
