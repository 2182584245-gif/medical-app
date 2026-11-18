import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'

const pages=new URL('../entry/src/main/ets/pages/',import.meta.url)
const postSource=await readFile(new URL('Community.ets',pages),'utf8')
const articleSource=await readFile(new URL('CommunityKnowledge.ets',pages),'utf8')
class CloudError extends Error {
  constructor(status,message){super(message);this.status=status}
}
function methods(source,start,end) {
  return source.slice(source.indexOf(start),source.indexOf(end))
}
const cases=[
  { name:'post', source:methods(postSource,'  private async loadDetail(', '  private async engage('),
    start:view=>view.loadDetail(), body:'/v1/member/forum/detail/42', comments:'/v1/member/forum/detail/42/comments?limit=30&offset=0',
    title:view=>view.detail.title, error:view=>view.commentError, retry:view=>view.loadPostPart(true,view.revision) },
  { name:'article', source:methods(articleSource,'  private async load(more:', '  private async socialAction('),
    start:view=>view.load(), body:'/v1/member/reading/articles/42',comments:'/v1/member/reading/comments?article_id=42&limit=30&offset=0',
    title:view=>view.article.title, error:view=>view.commentsError, retry:view=>view.readComments('') }
]

function setup(spec,responder){
  const calls=[],state={owner:5}
  const code=stripTypeScriptTypes('class View {\n'+spec.source+'\n}',{mode:'transform'})
  const View=Function('cloud','actorId','CloudError','noteError','stringValue','array','nextOffset',
    code+'\nreturn View')(
    async path=>{calls.push(path);return responder(path)},()=>state.owner,CloudError,
    error=>error.message,(data,key)=>data[key]||'',(data,key)=>data[key]||[],page=>page.next_offset??-1)
  const view=new View()
  Object.assign(view,{route:spec.name==='post'?'community/posts/42':'community/knowledge/42',revision:0,ticket:0,
    comments:[],detail:{},article:{},social:{},postError:'',error:'',commentsError:'',commentError:'',
    replies:[],thread:'',imageCache:new Map(),images:new Map(),socialReady:false,rootNext:-1,commentNext:-1,
    contentId:()=>42,catalogue:()=>false,food:()=>false,education:()=>false,loadImages:()=>{}})
  return {view,calls,state}
}

for(const spec of cases){
  test(spec.name+' body stays readable when comments fail; retry only fetches comments',async()=>{
    let fail=true
    const {view,calls}=setup(spec,path=>{
      if(path===spec.comments){
        if(fail)throw new Error('评论连接失败')
        return {items:[{content:'已恢复'}],next_offset:null}
      }
      return {id:42,title:'已加载正文'}
    })
    await spec.start(view)
    assert.equal(spec.title(view),'已加载正文')
    assert.equal(spec.error(view),'评论连接失败')
    fail=false;calls.length=0;await spec.retry(view)
    assert.deepEqual(calls,[spec.comments])
    assert.equal(spec.error(view),'')
    assert.equal(view.comments[0].content,'已恢复')
  })

  test(spec.name+' denied comments clear the detail and ignore a previously started late body',async()=>{
    let finish
    const {view}=setup(spec,path=>{
      if(path===spec.comments)throw new CloudError(403,'无权读取')
      if(path===spec.body)return new Promise(resolve=>{finish=resolve})
      return {}
    })
    const result=spec.start(view)
    await new Promise(resolve=>setImmediate(resolve))
    finish({id:42,title:'旧内容'})
    await result
    assert.equal(spec.title(view),undefined)
    assert.deepEqual(view.comments,[])
    assert.equal(view.error,'无权读取')
  })

  test(spec.name+' a delayed response is discarded after route change or account switch',async()=>{
    for(const change of ['route','account']){
      let finish
      const {view,state}=setup(spec,path=>path===spec.body?new Promise(resolve=>{finish=resolve}):{items:[]})
      const result=spec.start(view)
      await new Promise(resolve=>setImmediate(resolve))
      if(change==='route'){view.revision++;view.ticket++}else{state.owner=9}
      finish({id:42,title:'旧账号、旧地址'})
      await result
      assert.equal(spec.title(view),undefined)
    }
  })
}

test('unavailable article reactions stay unknown until an isolated retry succeeds',async()=>{
  const spec=cases[1]
  let fail=true
  const reaction='/v1/member/reading/social/42'
  const {view,calls}=setup(spec,path=>{
    if(path===reaction){if(fail)throw new Error('互动暂不可用');return {saved:true}}
    return path===spec.body?{title:'已加载正文'}:{items:[]}
  })
  await spec.start(view)
  assert.equal(spec.title(view),'已加载正文')
  assert.equal(view.socialReady,false);assert.equal(view.socialError,'互动暂不可用')
  calls.length=0;fail=false;await view.loadArticlePart(true,view.ticket)
  assert.deepEqual(calls,[reaction]);assert.equal(view.socialReady,true)
  assert.equal(view.social.saved,true)
})

test('a reaction waiting for account state cannot be written to a different article',async()=>{
  const code=stripTypeScriptTypes('class View {'+methods(articleSource,'  private async socialAction(', '  private async shareArticle(')+'}',{mode:'transform'})
  let resume
  const calls=[]
  const View=Function('cloud','actorId',code+'\nreturn View')(path=>{
    calls.push(path)
    return new Promise(resolve=>{resume=resolve})
  },()=>7)
  const view=new View()
  view.ticket=4;view.contentId=()=>'first'
  const result=view.socialAction('like','liked')
  view.ticket++;view.contentId=()=>'second'
  resume({data_generation:1});await result
  assert.deepEqual(calls,['/v1/member/local-sync/account-state'])
})
