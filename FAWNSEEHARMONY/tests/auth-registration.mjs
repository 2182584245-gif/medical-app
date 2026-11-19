import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const source=await readFile(new URL('../entry/src/main/ets/pages/Auth.ets',import.meta.url),'utf8')
const fields=source.slice(source.indexOf('export struct'),source.indexOf('  @Builder'))
  .replace('export struct','class').replace(/@State |@Prop /g,'')+'\n}'
const setup=Function('registerAccount','signIn','sendCode',
  stripTypeScriptTypes(fields,{mode:'transform'})+'\nreturn new AuthPage()')

function form(register,login){
  const page=setup(register,login,()=>{throw Error('SMS must not be requested for account registration')})
  page.route='auth/register';page.terms=true;page.username=' 测试名 ';page.password=' 123456 ';page.confirmation=page.password
  page.onNavigate=route=>{page.route=route}
  return page
}

test('account registration uses only username and unchanged password, then starts a normal login',async()=>{
  const trace=[]
  const page=form(async(...args)=>trace.push(args),async(...args)=>trace.push(args))
  await page.submit()
  assert.deepEqual(trace,[['测试名',' 123456 '],['/v1/auth/login',{username:'测试名',password:' 123456 '},false]])
  assert.equal(page.route,'signed-in')
})
test('a completed registration with a failed session goes to login and never registers twice',async()=>{
  let count=0
  const page=form(async()=>{count++},async()=>{throw Error('暂时离线')})
  await page.submit()
  assert.equal(page.route,'auth/login');assert.match(page.message,/账号已创建/);assert.equal(page.password,'')
  page.password=' 123456 ';await page.submit();assert.equal(count,1)
})
test('no consent, mismatched passwords, and too-short passwords are handled before a network call',async()=>{
  const page=form(()=>assert.fail('registration call'),()=>assert.fail('login call'))
  page.terms=false;await page.submit();assert.match(page.message,/隐私政策/)
  page.terms=true;page.confirmation='different';await page.submit();assert.match(page.message,/不一致/)
  page.password='短';page.confirmation='短';await page.submit();assert.match(page.message,/长度/)
})
test('occupied names remain editable and do not attempt account login',async()=>{
  const page=form(async()=>{throw Error('这个账号名已被使用。')},()=>assert.fail('unexpected login'))
  await page.submit()
  assert.equal(page.route,'auth/register');assert.match(page.message,/已被使用/);assert.equal(page.busy,false)
})
