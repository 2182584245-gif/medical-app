'use strict';

const { after, before, test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.FAWNSEE_PLAYWRIGHT || 'playwright');
const www = path.resolve(__dirname, '../../android/app/src/main/assets/www');
const photo = fs.readFileSync(path.join(www, 'assets/mobile-design/images/meal.webp')).toString('base64');

const stamp = '2026-11-15T12:30:00+08:00';
const uuid = n => 'c48dbdae-7610-43d8-b086-' + ('' + n).padStart(12, '0');
const tasks = Array.from({length:139}, (_, offset) => {
  const n = 139 - offset, diet = n <= 3;
  return {
    id: uuid(n), draft_id: uuid(n + 1000), assistant_message_id: 2000 + n * 2,
    title: diet ? '午饭' : '饮水', category: diet ? 'diet' : 'water',
    record_date: '2026-11-15', record_time: '12:30', status: diet ? 'ready' : 'needs_input',
    preview: n <= 2 ? '番茄炒蛋' : n === 3 ? '柿子鸡蛋汤' : '第' + n + '份饮水'
  };
});

function mockNative({tasks, photo, stamp, supported}) {
  const calls = [], history = [], completed = new Set();
  let current = null;
  const taskDraft = row => ({id:row.draft_id,kind:'record_draft',operation:'create',
    category:row.category,occurred_at:stamp,content:row.preview,target:null,blocked:false,missing_required:[],missing_optional:[],
    details:{food:row.preview,meal_type:'lunch'},photo_file_ids:[74]});
  window.recordTest = {calls,history,tasks,completed};
  const result = (msg,value) => queueMicrotask(() => FAWNSEE_NATIVE.receive({id:msg.id,ok:true,result:value}));
  const fail = (msg,code) => queueMicrotask(() => FAWNSEE_NATIVE.receive({id:msg.id,ok:false,error:{code,message:code}}));
  window.AndroidBridge = {postMessage(raw) {
    const msg = JSON.parse(raw), action = msg.params?.action, p = msg.params?.payload || {};
    calls.push({type:msg.action,action,p});
    if (msg.action === 'app.bootstrap') return result(msg,{user:{id:7,username:'fixture',display_name:'测试会员',role_code:'member',account_status:'active'},cloud:{endpoint:'https://synthetic.invalid'},data_generation:'b'.repeat(64)});
    if (msg.action === 'record.photo.get') return result(msg,{file_id:74,media_type:'image/webp',size_bytes:atob(photo).length,src:'data:image/webp;base64,'+photo});
    if (msg.action !== 'member.request') return result(msg,{});
    if (action === 'member.sync') return result(msg,{revision:'record-task-tests'});
    if (action === 'onboarding.get') return result(msg,{stage:'done',completed:true,tutorial_completed:true,profile:{user_id:7,display_name:'测试会员'}});
    if (action === 'preferences.get') return result(msg,{ai_context_consent:true,ai_personal_context_consent:true,ai_thinking_enabled:false});
    if (action === 'health.profile.get') return result(msg,{user_id:7,display_name:'测试会员'});
    if (action === 'chat.conversations.list') return result(msg,[{id:41,title:'记录核对'}]);
    if (action === 'chat.conversations.create') return result(msg,{id:41,title:'记录核对'});
    if (action === 'chat.greeting.ensure') return result(msg,{created:true,boot_id:p.boot_id,message:{id:900041,conversation_id:41,sequence_no:0,role:'assistant',status:'complete',provider:'fawnsee-welcome',content:'您好，今天想记录什么？'}});
    if (action === 'chat.messages.list') return result(msg,history);
    if (action === 'local149.draft.get') return result(msg,{text:''});
    if (action?.startsWith('local149.draft')) return result(msg,{});
    if (action === 'chat.record_tasks.capability') return result(msg,{record_tasks:supported});
    if (action === 'chat.record_tasks.list') {
      if (p.conversation_id !== 41) return fail(msg,'not_found');
      const candidates = tasks.filter(item => !completed.has(item.id) && (!p.query || `${item.title} ${item.preview}`.includes(p.query)));
      const offset = Number(p.cursor || 0), after = offset + p.limit;
      return result(msg,{tasks:candidates.slice(offset,after),total:candidates.length,next_cursor:after<candidates.length?String(after):null,active_task_id:current});
    }
    if (action === 'chat.record_tasks.focus') {
      const row = tasks.find(item => item.id === p.task_id && !completed.has(item.id));
      if (p.conversation_id !== 41 || !row) return fail(msg,'not_found');
      current = row.id;
      return result(msg,{task:row,draft:taskDraft(row)});
    }
    if (action === 'chat.records.confirm') {
      const row=tasks.find(item => item.draft_id === p.draft_id && item.assistant_message_id === p.assistant_message_id);
      if (!row || completed.has(row.id)) return fail(msg,'not_found');
      if (recordTest.holdCloudSync) {
        recordTest.queuedTask = row.id;
        return result(msg,{id:row.draft_id,status:'pending_sync',category:row.category,record_id:-88,version:null});
      }
      completed.add(row.id);
      return result(msg,{id:row.draft_id,status:'saved',category:row.category,record_id:88,version:'1ae2b570f81cbe02'});
    }
    if (action === 'chat.send') {
      const user = {id:3999+history.length,role:'user',status:'complete',content:p.content};
      const assistant = {id:user.id+1,role:'assistant',status:'complete',content:'已继续这条记录。'};
      history.push(user,assistant);
      return result(msg,{conversation:{id:41,title:'记录核对'},user_message:user,assistant_message:assistant});
    }
    return result(msg,[]);
  }};
}

let server, browser, origin;
before(async () => {
  const mime = {'.html':'text/html; charset=utf-8','.js':'text/javascript','.webp':'image/webp','.png':'image/png','.svg':'image/svg+xml','.woff2':'font/woff2'};
  server = http.createServer((request,response) => {
    const name = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname);
    const file = path.resolve(www, '.' + (name === '/' ? '/index.html' : name));
    if (!file.startsWith(www + path.sep)) return response.writeHead(404).end();
    try { response.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream'}).end(fs.readFileSync(file)); }
    catch { if (!response.headersSent) response.writeHead(404); response.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({executablePath:process.env.FAWNSEE_CHROME,headless:true,args:['--disable-background-networking','--no-first-run']});
});
after(async () => { await browser?.close(); if (server) await new Promise(resolve => server.close(resolve)); });

async function open(supported=true) {
  const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
  await context.addInitScript(mockNative,{tasks,photo,stamp,supported});
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  await page.route('**/*',route => /^(blob:|data:)/.test(route.request().url())||route.request().url().startsWith(origin+'/')?route.continue():route.abort());
  await page.goto(origin+'/index.html');
  await page.waitForFunction(() => FAWNSEE_MOBILE?.user?.id===7&&FAWNSEE_MOBILE?.sessionReady&&!FAWNSEE_FIRST_MEET_LIVE.active);
  await page.evaluate(() => go('#ai',true));
  const frame = page.frameLocator('#main-frame');
  await frame.locator('[data-primary-chat-input]').waitFor({state:'visible'});
  return {page,frame,errors,close:() => context.close()};
}

test('139 份同一聊天的待完成草稿可分页搜索，精确选中旧草稿并在原对话补答', async () => {
  const f = await open();
  try {
    const entry=f.frame.locator('[data-record-task-open]');
    await f.frame.getByRole('button',{name:/139/}).waitFor({state:'visible'});
    await entry.click();
    const dialog=f.frame.getByRole('dialog',{name:'待完成记录',exact:true});
    await dialog.locator('[data-record-task-id]').first().waitFor();
    assert.equal(await dialog.locator('[data-record-task-id]').count(),20);
    await dialog.getByRole('button',{name:'加载更多'}).click();
    await dialog.locator('[data-record-task-id="'+uuid(100)+'"]').waitFor();
    assert.equal(await dialog.locator('[data-record-task-id]').count(),40);
    await dialog.getByRole('searchbox').fill('番茄炒蛋');
    const chosen=dialog.locator('[data-record-task-id="'+uuid(1)+'"]');
    await chosen.waitFor();
    assert.equal(await dialog.locator('[data-record-task-id]').count(),2);
    assert.match(await chosen.textContent(),/午饭.*12:30.*番茄炒蛋.*来源消息/s);
    await chosen.click();
    const details=f.frame.getByRole('dialog',{name:'午饭',exact:true});
    await details.getByRole('img',{name:'记录照片 1'}).waitFor();
    assert.match(await details.textContent(),/番茄炒蛋/);
    await details.getByRole('button',{name:'返回对话'}).click();
    const input=f.frame.locator('[data-primary-chat-input]');
    await input.fill('刚才那份少盐');
    await f.frame.locator('[data-parent-action="ai-send"]').click();
    await f.page.waitForFunction(() => recordTest.calls.some(row => row.action==='chat.send'));
    const state=await f.page.evaluate(() => ({focus:recordTest.calls.filter(row=>row.action==='chat.record_tasks.focus').map(row=>row.p),send:recordTest.calls.find(row=>row.action==='chat.send').p}));
    assert.equal(state.focus[0].task_id,uuid(1));
    assert.equal(state.send.record_task_id,uuid(1));
    assert.equal(state.send.conversation_id,41);
    assert.deepEqual(f.errors,[]);
  } finally { await f.close(); }
});

test('刷新正在显示的智能助手页面时保留已打开的待核对列表及顶栏入口', async () => {
  const f = await open();
  try {
    const entry = f.frame.locator('[data-record-task-open]');
    await entry.click();
    const dialog = f.frame.getByRole('dialog', {name:'待完成记录', exact:true});
    await dialog.locator('[data-record-task-id]').first().waitFor();
    await f.page.evaluate(() => {
      const next = renderPrimary('ai').replace(/<body\b/i, '<body data-panel-refresh-test="yes"');
      FAWNSEE_EXPERIENCE.primary('ai', next);
    });
    await f.frame.locator('body[data-panel-refresh-test=yes]').waitFor();
    assert.equal(await dialog.isVisible(), true);
    assert.equal(await dialog.locator('[data-record-task-id]').count(), 20);
    await dialog.locator('.f-record-task-dismiss').click();
    await entry.click();
    await dialog.locator('[data-record-task-id]').first().waitFor();
  } finally { await f.close(); }
});

test('待完成列表能直接确认未加载到聊天历史里的旧草稿，自动更新剩余数量', async () => {
  const f = await open();
  try {
    await f.frame.locator('[data-record-task-open]').click();
    const list=f.frame.getByRole('dialog',{name:'待完成记录',exact:true});
    await list.getByRole('searchbox').fill('柿子鸡蛋汤');
    await list.getByRole('button',{name:/柿子鸡蛋汤/}).click();
    const details=f.frame.getByRole('dialog',{name:'午饭',exact:true});
    await details.getByRole('button',{name:'确认记录',exact:true}).click();
    await details.getByText('已记录',{exact:true}).waitFor();
    const writes=await f.page.evaluate(() => recordTest.calls.filter(row=>row.action==='chat.records.confirm').map(row=>row.p));
    assert.equal(writes.length,1);
    assert.equal(writes[0].draft_id,uuid(1003));
    assert.equal(writes[0].assistant_message_id,2006);
    await details.getByRole('button',{name:'返回对话'}).click();
    await f.frame.getByRole('button',{name:/138/}).waitFor();
    assert.deepEqual(f.errors,[]);
  } finally { await f.close(); }
});

test('未启用待完成记录的后端仍保留既有聊天入口', async () => {
  const f = await open(false);
  try {
    await f.page.waitForFunction(() => recordTest.calls.some(row=>row.action==='chat.record_tasks.capability'));
    assert.equal(await f.frame.locator('[data-record-task-open]').isVisible(),false);
    assert.equal(await f.frame.locator('[data-primary-chat-input]').isVisible(),true);
    assert.equal(await f.page.evaluate(() => recordTest.calls.filter(row=>row.action==='chat.record_tasks.list').length),0);
  } finally { await f.close(); }
});

test('草稿入口随最后一条处理而隐藏，新增后回到更新图标左侧', async () => {
  const f = await open();
  try {
    const entry = f.frame.locator('[data-record-task-open]');
    await entry.waitFor();
    assert.equal(await entry.evaluate(el => !!el.closest('header')),true);
    const hint = entry.getByText('有待确定的记录，点击查看',{exact:true});
    await hint.waitFor();
    assert.equal(await entry.evaluate(el => el.parentNode.nextElementSibling?.getAttribute('data-parent-action')),'app-update');
    assert.equal(await f.frame.locator('.ai-composer-panel [data-record-task-open]').count(),0);
    await f.page.evaluate(() => {
      for (const item of recordTest.tasks.slice(0,-1)) recordTest.completed.add(item.id);
      return FAWNSEE_DRAFT_TASKS.refresh();
    });
    await f.frame.getByRole('button',{name:/1条/}).click();
    const list = f.frame.getByRole('dialog',{name:'待完成记录',exact:true});
    await list.locator('[data-record-task-id]').click();
    const details = f.frame.getByRole('dialog');
    await details.getByRole('button',{name:'确认记录',exact:true}).click();
    await details.getByText('已记录',{exact:true}).waitFor();
    await details.getByRole('button',{name:'返回对话'}).click();
    await entry.waitFor({state:'hidden'});
    await f.page.evaluate(() => {
      recordTest.tasks.push({...recordTest.tasks.at(-1),id:'c48dbdae-7610-43d8-b086-000000000880'});
      return FAWNSEE_DRAFT_TASKS.refresh();
    });
    await hint.waitFor();
    assert.equal(await entry.evaluate(el => el.parentNode.nextElementSibling?.getAttribute('data-parent-action')),'app-update');
    await hint.click();
    await list.locator('[data-record-task-id="c48dbdae-7610-43d8-b086-000000000880"]').waitFor();
  } finally { await f.close(); }
});
test('窄屏气泡与更新、历史和智能设置各自可点击', async () => {
  const f = await open();
  try {
    const hint = f.frame.locator('[data-record-task-open] .f-record-task-hint');
    await hint.waitFor();
    for (const width of [320,375,390,430]) {
      await f.page.setViewportSize({width,height:844});
      await f.page.evaluate(() => new Promise(resolve => requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const layout = await hint.evaluate(label => {
        const doc = label.ownerDocument;
        const update = doc.querySelector('[data-parent-action="app-update"]');
        const hit = node => {
          const r = node.getBoundingClientRect();
          return {x:r.x,y:r.y,w:r.width,h:r.height,
            hit:node.contains(doc.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};
        };
        return {width:doc.documentElement.clientWidth,
          hint:hit(label), icon:hit(label.parentElement), artTop:label.parentElement.querySelector('svg').getBoundingClientRect().top, update:hit(update),
          history:hit(doc.querySelector('[data-parent-route="#ai/history"]')),
          settings:hit(doc.querySelector('[data-parent-route="#ai/settings"]'))};
      });
      if (process.env.FAWNSEE_TASK_EVIDENCE) {
        fs.mkdirSync(process.env.FAWNSEE_TASK_EVIDENCE,{recursive:true});
        await f.page.screenshot({path:path.join(process.env.FAWNSEE_TASK_EVIDENCE,'web-ai-header-'+width+'.png')});
        fs.writeFileSync(path.join(process.env.FAWNSEE_TASK_EVIDENCE,'web-ai-header-'+width+'.json'),JSON.stringify(layout,null,2));
      }
      for(const [key,box] of Object.entries(layout)){
        if(key==='width'||key==='artTop')continue;
        assert.ok(box.x>=0&&box.y>=0&&box.x+box.w<=layout.width+1&&box.hit,JSON.stringify({width,key,box,viewport:layout.width}));
      }
      assert.ok(layout.hint.w>100&&layout.hint.h<=30,JSON.stringify({width,hint:layout.hint}));
      assert.ok(layout.hint.y+layout.hint.h<layout.artTop,JSON.stringify({width,hint:layout.hint,artTop:layout.artTop}));
    }
  } finally { await f.close(); }
});

test('迟到的旧对话计数不会显示到新会话，隐藏时发生的变化在返回后同步', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<header class="f-header-actions"><button data-parent-action="app-update">更新</button></header><div class="ai-composer-panel"></div>');
    await page.addScriptTag({path:path.join(www,'record-task-switcher.js')});
    await page.evaluate(async () => {
      let owner='account-one', conversation=41, onAI=true;
      const pending={41:0,42:0,43:0};
      let delayNext=false, release=null;
      function mount() {
        const user=owner, id=conversation;
        FAWNSEE_DRAFT_TASKS.mount(document,{
          owner:user,conversation:id,isCurrent:()=>owner===user&&conversation===id&&onAI,
          api:async(action,params)=>{
            if(action==='chat.record_tasks.capability')return {record_tasks:true};
            if(params.conversation_id!==id)throw Error('wrong conversation');
            const total=pending[id];
            if(delayNext){delayNext=false;await new Promise(resolve=>release=resolve);}
            return {tasks:[],total};
          }
        });
      }
      window.scopeTest={
        change:async(id,total)=>{pending[id]=total;await FAWNSEE_DRAFT_TASKS.refresh();},
        switchTo:(u,c)=>{owner=u;conversation=c;mount();},
        delay:()=>{delayNext=true;FAWNSEE_DRAFT_TASKS.refresh();},
        finish:()=>release(),visible:flag=>{onAI=flag;if(flag)mount();}
      };
      mount();
    });
    const entry=page.locator('[data-record-task-open]');
    await entry.waitFor({state:'attached'});
    await page.evaluate(()=>scopeTest.change(41,1));
    await entry.waitFor({state:'visible'});
    await page.evaluate(()=>{scopeTest.delay();scopeTest.switchTo('account-one',42);});
    await entry.waitFor({state:'hidden'});
    await page.evaluate(()=>scopeTest.finish());
    assert.equal(await entry.isVisible(),false);
    await page.evaluate(()=>{scopeTest.visible(false);return scopeTest.change(42,2);});
    await page.evaluate(()=>scopeTest.visible(true));
    await page.getByRole('button',{name:/2条/}).waitFor();
    await page.evaluate(()=>scopeTest.switchTo('account-two',43));
    await entry.waitFor({state:'hidden'});
    await page.evaluate(()=>scopeTest.switchTo(null,43));
    assert.equal(await entry.count(),0);
  } finally {await page.close();}
});

test('安卓先存本机，云端完成后在原页面刷新最后一份待办', async () => {
  const f = await open();
  try {
    await f.frame.locator('[data-record-task-open]').waitFor();
    await f.page.evaluate(() => {
      recordTest.holdCloudSync = true;
      recordTest.tasks.slice(0,-1).forEach(item=>recordTest.completed.add(item.id));
      return FAWNSEE_DRAFT_TASKS.refresh();
    });
    const entry=f.frame.getByRole('button',{name:/1条/});
    await entry.click();
    const dialog=f.frame.getByRole('dialog');
    await dialog.locator('[data-record-task-id]').click();
    await dialog.getByRole('button',{name:'确认记录',exact:true}).click();
    await dialog.getByText('已存本机',{exact:true}).waitFor();
    await dialog.getByRole('button',{name:'返回对话'}).click();
    assert.ok(await entry.isVisible());
    const before=await f.page.evaluate(()=>recordTest.calls.filter(r=>r.action==='chat.record_tasks.list').length);
    await f.page.evaluate(()=>{
      recordTest.completed.add(recordTest.queuedTask);
      FAWNSEE_NATIVE.receive({event:'localSync149',result:{pending:0}});
    });
    await entry.waitFor({state:'hidden',timeout:4500});
    const after=await f.page.evaluate(()=>recordTest.calls.filter(r=>r.action==='chat.record_tasks.list').length);
    assert.ok(after>before);
  } finally {await f.close();}
});
