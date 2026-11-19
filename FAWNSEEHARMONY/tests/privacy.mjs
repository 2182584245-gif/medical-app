import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import {stripTypeScriptTypes} from 'node:module'
import {test} from 'node:test'

async function compile(relative) {
  const src = await readFile(new URL('../entry/src/main/ets/' + relative, import.meta.url),'utf8')
  return stripTypeScriptTypes(src.replace(/^import .*$/gm,'').replace(/^export /gm,''),{mode:'transform'})
}
const choices = await compile('services/PrivacyChoices.ets')
const weather = await compile('pages/TodayWeather.ets')

function run(store) {
  const preferences = {getPreferencesSync() {return {
    getSync:(k, fallback)=>store.has(k)?store.get(k):fallback,
    async put(k,v){store.set(k,v)}, async flush(){}
  }}}
  return Function('preferences',choices+'\nreturn {readPrivacy,requirePrivacy,acceptPrivacy,optionalAllowed,setOptional}')(preferences)
}

test('upgrade requires a local choice and optional approvals are specific to account and purpose',async()=>{
  const stored = new Map([['accepted_version','old-policy']])
  const state = run(stored)
  assert.equal(state.readPrivacy({}),false)
  assert.throws(()=>state.requirePrivacy())
  await assert.rejects(state.setOptional('ai',3,true))
  await state.acceptPrivacy()
  for(const use of ['ai','weather','health'])assert.equal(state.optionalAllowed(use,3),false)
  await state.setOptional('ai',3,true)
  assert.equal(state.optionalAllowed('ai',3),true)
  assert.equal(state.optionalAllowed('ai',0),false)
  assert.equal(state.optionalAllowed('health',3),false)
  const reopened = run(stored)
  assert.equal(reopened.readPrivacy({}),true)
  assert.equal(reopened.optionalAllowed('ai',3),true)
  await reopened.setOptional('ai',3,false)
  assert.equal(reopened.optionalAllowed('ai',3),false)
})

test('weather never constructs an HTTP client before separate permission',async()=>{
  let count=0
  const http={createHttp(){count++;throw Error('Unexpected weather connection')}}
  const getWeather=Function('http','optionalAllowed','actorId',weather+'\nreturn todayWeather')(http,()=>false,()=>4)
  assert.equal(await getWeather(),'天气未开启')
  assert.equal(count,0)
})
