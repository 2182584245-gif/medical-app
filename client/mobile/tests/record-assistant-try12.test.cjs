'use strict';
const {after,before,test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const {chromium}=require(process.env.FAWNSEE_PLAYWRIGHT || 'playwright');

const www=path.resolve(__dirname,'../../android/app/src/main/assets/www');
const bundle=fs.readFileSync(path.join(www,'app.js'),'utf8');
assert.ok(bundle.includes('Chat record confirmation, using only server-validated drafts and receipts.'));
const screenshotDir=process.env.FAWNSEE_RECORD_SCREENSHOTS;
const dietScreenshot=process.env.FAWNSEE_DIET_SCREENSHOT;
const sample=fs.readFileSync(path.join(www,'assets/mobile-design/images/meal.webp'));
const stamp='2026-11-12T12:05:00+08:00';
const recordId=31;
const id=n=>'db247420-9513-40ab-b951-'+String(n).padStart(12,'0');
const draft=(n,fields={})=>({
  id:id(n),kind:'record_draft',category:'water',occurred_at:stamp,content:'饮水',details:{amount_ml:350},
  operation:'create',target:null,missing_required:[],missing_optional:[],blocked:false,...fields
});
const target={id:recordId,version:'7ca0632eb9a308d3',before:{occurred_at:stamp,content:'饮水',details:{amount_ml:250}}};
const edit=draft(2,{operation:'update',target});
const supplement=draft(3,{operation:'supplement',target,details:{amount_ml:250,notes:'散步回来后喝的'}});
const required=draft(4,{occurred_at:null,record_date:'2026-11-12',missing_required:[{field:'record_time',label:'发生时间'}]});
const refused=draft(5,{...required,id:id(5),blocked:true,supersedes_draft_id:id(4)});
const optional=draft(6,{missing_optional:[{field:'notes',label:'其他说明'}]});
const reply=(id,record_drafts,content='请核对我整理的记录。')=>({
  id,sequence_no:id,role:'assistant',status:'complete',content,record_drafts,
  activity:{version:1,status:'complete',steps:[{kind:'controlled_tools',status:'done',label:'已整理记录',detail:'本次协作：记录整理（完成）。'}]}
});
const syntheticPhoto={encoded:sample.toString('base64'),size:sample.length};
const samplePriorPhoto=fs.readFileSync(path.join(www,'assets/mobile-design/images/rest.webp')).toString('base64');

function bridge(options){
  const calls=[],history=[],decisions=new Map(),records=new Map(),files=new Map(),stamp='2026-11-12T12:05:00+08:00';
  for(const fileId of options.seedPhotos||[])files.set(fileId,options.priorPhoto);
  window.recordTest={calls,history,decisions,records,files,waiters:[],failNext:options.failNext||null,
    photoUnavailable:options.photoUnavailable||null};
  const r=(msg,result)=>queueMicrotask(()=>FAWNSEE_NATIVE.receive({id:msg.id,ok:true,result}));
  const e=(msg,code)=>queueMicrotask(()=>FAWNSEE_NATIVE.receive({id:msg.id,ok:false,error:{code,message:code}}));
  const messages=()=>history.map(row=>({...row,record_receipts:row.record_drafts?.map(d=>decisions.get(d.id)).filter(Boolean)}));
  window.AndroidBridge={postMessage(raw){
    const msg=JSON.parse(raw),action=msg.params?.action,p=msg.params?.payload||{};
    calls.push({type:msg.action,action,params:msg.action==='member.request'?p:msg.params||{}});
    if(msg.action==='app.bootstrap')return r(msg,{user:{id:7,username:'test-member',display_name:'测试会员',role_code:'member',account_status:'active'},cloud:{endpoint:'https://synthetic.invalid'},data_generation:'a'.repeat(64)});
    if(msg.action==='native.pickImage')return r(msg,{images:[{name:'acceptance-meal.webp',media_type:'image/webp',data_base64:options.photo.encoded,size_bytes:options.photo.size}]});
    if(msg.action==='record.photo.get'){
      const fileId=msg.params?.file_id;
      const encoded=files.get(fileId);
      if(!encoded||msg.params?._fawnsee_actor_id!==7)return e(msg,'not_found');
      if(recordTest.photoUnavailable===fileId)return e(msg,'temporary_unavailable');
      return r(msg,{file_id:fileId,media_type:'image/webp',size_bytes:atob(encoded).length,src:'data:image/webp;base64,'+encoded});
    }
    if(msg.action!=='member.request')return r(msg,{});
    if(action==='member.sync')return r(msg,{revision:'synthetic-record-tests'});
    if(action==='onboarding.get')return r(msg,{stage:'done',completed:true,tutorial_completed:true,profile:{user_id:7,display_name:'测试会员'}});
    if(action==='preferences.get')return r(msg,{ai_context_consent:true,ai_personal_context_consent:true,ai_thinking_enabled:false});
    if(action==='health.profile.get')return r(msg,{user_id:7,display_name:'测试会员'});
    if(action==='chat.conversations.list')return r(msg,[{id:41,title:'记录核对'}]);
    if(action==='chat.conversations.create')return r(msg,{id:41,title:'记录核对'});
    if(action==='chat.greeting.ensure')return r(msg,{created:true,boot_id:p.boot_id,message:{id:900041,conversation_id:41,sequence_no:0,role:'assistant',status:'complete',provider:'fawnsee-welcome',content:'您好，今天想记录什么？'}});
    if(action==='chat.messages.list')return r(msg,messages());
    if(action==='chat.message.attachments'||action==='chat.files.list')return r(msg,[]);
    if(action==='local149.draft.get')return r(msg,{text:''});
    if(action.startsWith('local149.draft'))return r(msg,{});
    if(action==='files.upload'){
      files.set(83,p.content);
      return r(msg,83);
    }
    if(action==='chat.send'){
      const response=options.responses[history.filter(row=>row.role==='assistant').length];
      if(!response)return e(msg,'fixture_exhausted');
      const user={id:response.id-1,role:'user',status:'complete',sequence_no:response.id-1,content:p.content};
      history.push(user,response);
      return r(msg,{conversation:{id:41,title:'记录核对'},user_message:user,assistant_message:response});
    }
    if(action==='chat.records.confirm'||action==='chat.records.cancel'){
      const old=decisions.get(p.draft_id);
      if(old)return r(msg,old);
      const found=history.find(row=>row.id===p.assistant_message_id)?.record_drafts?.find(d=>d.id===p.draft_id);
      if(!found)return e(msg,'not_found');
      const cancel=action.endsWith('.cancel');
      if(!cancel&&recordTest.failNext){const failure=recordTest.failNext;recordTest.failNext=null;return e(msg,failure);}
      if(!cancel&&(found.blocked||found.missing_required.length))return e(msg,'validation');
      const receipt={id:found.id,status:cancel?'cancelled':options.pending?'pending_sync':'saved',category:found.category};
      if(!cancel){
        const key=found.target?.id||(options.pending?-42:42);
        Object.assign(receipt,{record_id:key,version:'1ae2b570f81cbe02'});
        const details={...found.details,...(found.photo_file_ids?{photo_file_ids:found.photo_file_ids}:{})};
        records.set(key,{id:key,version:'1ae2b570f81cbe02',category:found.category,occurred_at:found.occurred_at||stamp,content:found.content,details,source:'ai_confirmed',created_at:stamp});
      }
      decisions.set(found.id,receipt);
      return r(msg,receipt);
    }
    if(action==='expansion.records.list'||action==='health.records.list')return r(msg,{items:[...records.values()],total:records.size});
    if(action==='expansion.records.get')return r(msg,records.get(p.record_id)||null);
    return r(msg,[]);
  }};
}

let server,browser,origin;
before(async()=>{
  server=http.createServer((request,response)=>{
    const pathname=decodeURIComponent(new URL(request.url,'http://127.0.0.1').pathname);
    const filename=path.resolve(www,'.'+(pathname==='/'?'/index.html':pathname));
    if(!filename.startsWith(www+path.sep)){response.writeHead(404).end();return;}
    const mime={'.html':'text/html; charset=utf-8','.js':'text/javascript','.webp':'image/webp','.png':'image/png','.jpg':'image/jpeg','.svg':'image/svg+xml','.woff2':'font/woff2'};
    try{const bytes=fs.readFileSync(filename);response.writeHead(200,{'Content-Type':mime[path.extname(filename)]||'application/octet-stream'}).end(bytes);}
    catch{response.writeHead(404).end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  origin='http://127.0.0.1:'+server.address().port;
  browser=await chromium.launch({executablePath:process.env.FAWNSEE_CHROME,headless:true,args:['--disable-background-networking','--no-first-run']});
  if(screenshotDir)fs.mkdirSync(screenshotDir,{recursive:true});
});
after(async()=>{await browser?.close();if(server)await new Promise(resolve=>server.close(resolve));});

async function scenario(responses,options={}){
  const width=options.width||390,height=options.height||844,context=await browser.newContext({viewport:{width,height},isMobile:true,hasTouch:true,reducedMotion:'reduce'});
  await context.addInitScript(bridge,{...options,responses,photo:syntheticPhoto,priorPhoto:samplePriorPhoto});
  const page=await context.newPage(),errors=[];
  page.setDefaultTimeout(10000);
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',route=>/^(blob:|data:)/.test(route.request().url())||route.request().url().startsWith(origin+'/')?route.continue():route.abort());
  await page.goto(origin+'/index.html');
  try{await page.waitForFunction(()=>FAWNSEE_MOBILE?.user?.id===7&&FAWNSEE_MOBILE?.sessionReady&&!FAWNSEE_FIRST_MEET_LIVE.active);}
  catch(error){
    const boot=await page.evaluate(()=>({account:FAWNSEE_MOBILE?.user?.id,ready:FAWNSEE_MOBILE?.sessionReady,startup:document.querySelector('[data-first-meet-gate]')?.textContent,calls:recordTest.calls.map(x=>x.action||x.type)}));
    await context.close();
    throw Error('Test startup did not settle '+JSON.stringify({boot,errors}),{cause:error});
  }
  await page.evaluate(()=>go('#ai',true));
  const frame=page.frameLocator('#main-frame');
  const input=frame.locator('[data-primary-chat-input]');
  await input.waitFor({state:'visible'});
  const card=number=>frame.locator('[data-ai-record-draft-id="'+id(number)+'"]');
  const controls=number=>frame.locator('[data-ai-record-draft-controls="'+id(number)+'"]');
  const send=async(content,photo=false)=>{
    if(photo){
      await frame.locator('[data-ai-photo]').click();
      await frame.getByRole('button',{name:'从相册选择',exact:true}).click();
      await page.waitForFunction(()=>!!FAWNSEE_AI.api.read().photo.image);
    }
    await input.fill(content);
    await frame.locator('[data-parent-action="ai-send"]').click();
  };
  const photograph=async(locator,name)=>{
    if(!screenshotDir)return;
    await page.getByText('正在打开相册…',{exact:true}).waitFor({state:'hidden'});
    await locator.evaluate(card=>card.scrollIntoView({block:'center'}));
    await page.screenshot({path:path.join(screenshotDir,name),animations:'disabled'});
  };
  return{page,frame,input,card,controls,send,photograph,errors,close:()=>context.close()};
}

test('饮食草稿字段和手动记录一致，直接对话补选填项后只可确认新版',async()=>{
  const first=draft(10,{category:'diet',content:'午饭吃了清蒸鱼',details:{food:'清蒸鱼',meal_type:'lunch'},
    missing_optional:[{field:'calories_kcal',label:'热量'},{field:'notes',label:'其他说明'}]});
  const updated=draft(11,{...first,id:id(11),details:{...first.details,calories_kcal:320,notes:'少盐'},
    missing_optional:[],supersedes_draft_id:id(10)});
  const firstReply='请核对这份饮食草稿。如果您愿意，可以直接回复补充热量或其他说明。';
  const finalReply='已补上热量和其他说明，请您核对更新后的草稿。';
  const f=await scenario([reply(102,[first],firstReply),reply(104,[updated],finalReply)],{height:dietScreenshot?930:844});
  try{
    await f.send('请记下今天 12:05 的午餐，吃了清蒸鱼。');
    const card=f.card(10),confirmation=f.controls(10);
    await confirmation.getByRole('button',{name:'确认记录'}).waitFor();
    assert.deepEqual(await card.locator('dt').allTextContents(),['记录日期','发生时间','餐次','吃了什么','热量','其他说明']);
    assert.deepEqual(await card.locator('dd').allTextContents(),['2026-11-12','12:05','午餐','清蒸鱼','未填写','未填写']);
    const message=f.frame.locator('[data-ai-message-id="102"]');
    const order=await message.evaluate((row,draftId)=>{
      const draft=row.querySelector('[data-ai-record-draft-id="'+draftId+'"]');
      const answer=row.querySelector('[data-ai-answer-body]');
      const controls=row.querySelector('[data-ai-record-draft-controls="'+draftId+'"]');
      const follows=(earlier,later)=>!!(earlier.compareDocumentPosition(later)&Node.DOCUMENT_POSITION_FOLLOWING);
      return follows(draft,answer)&&follows(answer,controls);
    },id(10));
    assert.equal(order,true);
    await message.getByText(firstReply,{exact:true}).waitFor();
    assert.equal(await message.locator('[data-ai-collaboration]').count(),0);
    assert.doesNotMatch(await message.textContent(),/本次协作/);
    await message.locator('[data-ai-activity] > summary').click();
    const step=message.locator('[data-activity-kind="record_assistant"]');
    await step.getByText('已调用记录助手',{exact:true}).waitFor();
    assert.equal(await step.locator('svg').count(),1);
    assert.equal(await step.locator('.ai-activity-step').evaluate(label=>getComputedStyle(label).color),'rgb(112, 112, 112)');
    assert.equal(await f.frame.getByRole('button',{name:'补充信息'}).count(),0);
    assert.equal(await f.page.getByText('已整理记录草稿，请逐条核对后保存。',{exact:true}).count(),0);
    if(dietScreenshot){
      fs.mkdirSync(path.dirname(dietScreenshot),{recursive:true});
      await card.evaluate(node=>{
        const activity=node.closest('[data-ai-message-id]').querySelector('[data-ai-activity]');
        activity.style.scrollMarginTop='84px';
        activity.scrollIntoView({block:'start'});
      });
      await f.page.screenshot({path:dietScreenshot,animations:'disabled'});
    }
    await f.page.waitForFunction(()=>!FAWNSEE_MOBILE_AI.isBusy()&&!FAWNSEE_MOBILE_AI.aiCooldownLeft());
    await f.send('热量 320 千卡，其他说明写少盐。');
    const newCard=f.card(11);
    await newCard.waitFor();
    assert.deepEqual((await newCard.locator('dd').allTextContents()).slice(-2),['320 千卡','少盐']);
    assert.equal(await card.getAttribute('data-record-state'),'superseded');
    assert.equal(await confirmation.count(),0);
    await f.controls(11).getByRole('button',{name:'确认记录'}).click();
    await newCard.getByRole('button',{name:'查看记录'}).waitFor();
    assert.deepEqual(await f.page.evaluate(()=>[...recordTest.decisions.keys()]),[id(11)]);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('七类卡片的标题仅显示类别，三种确认操作仍分别标明',async()=>{
  const categories=[
    ['diet','饮食记录'],['water','饮水记录'],['activity','运动记录'],['sleep','睡眠记录'],
    ['environment','居住环境记录'],['medical','医疗检查记录'],['metric','身体指标记录']
  ];
  const operations=[['create','确认记录'],['update','确认修改'],['supplement','确认补充']];
  const groups=operations.map(([operation],group)=>categories.map(([category],index)=>draft(100+group*10+index,{
    category,operation,details:{},target:operation==='create'?null:{id:recordId,version:'prior'}
  })));
  const f=await scenario(groups.map((rows,index)=>reply(102+index*2,rows)));
  try{
    for(const [index,[operation,action]] of operations.entries()){
      await f.page.waitForFunction(()=>!FAWNSEE_MOBILE_AI.isBusy()&&!FAWNSEE_MOBILE_AI.aiCooldownLeft());
      await f.send('整理草稿 '+(index+1));
      for(const [item,[category,title]] of categories.entries()){
        const number=100+index*10+item,card=f.card(number),controls=f.controls(number);
        const header=card.locator('header');
        await header.getByRole('heading',{name:title,exact:true}).waitFor();
        assert.equal(await card.getAttribute('aria-label'),title,category);
        assert.equal(await controls.getAttribute('aria-label'),title,category);
        assert.equal(await card.getAttribute('data-record-operation'),operation,category);
        assert.equal(await header.locator(':scope > *').count(),2,category);
        assert.equal(await header.textContent(),title+'待确认',category);
        assert.equal(await controls.getByRole('button',{name:action,exact:true}).count(),1,category);
      }
    }
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('其他六类记录均先按手动表单展示核对草稿，再显示对话与操作',async()=>{
  const cases=[
    {category:'water',details:{amount_ml:250},labels:['饮水量','其他说明'],values:['250 毫升','未填写']},
    {category:'activity',details:{activity:'散步',duration_minutes:35,steps:3200},
      labels:['运动方式','时长','步数','其他说明'],values:['散步','35 分钟','3200 步','未填写']},
    {category:'sleep',details:{duration_hours:7.5},labels:['睡眠时长','其他说明'],values:['7.5 小时','未填写']},
    {category:'environment',details:{temperature_c:24,humidity_percent:48,description:'窗户已打开'},
      labels:['室温','湿度','环境情况'],values:['24 °C','48%','窗户已打开']},
    {category:'medical',details:{name:'门诊复查',location:'测试机构',description:'预约后复查'},
      labels:['事项名称','地点或机构','具体事项'],values:['门诊复查','测试机构','预约后复查']},
    {category:'metric',details:{kind:'blood_pressure',value:122,diastolic:76,unit:'mmHg'},
      labels:['身体指标类型','收缩压数值','舒张压数值','其他说明'],values:['血压','122 mmHg','76 mmHg','未填写']},
    {category:'metric',details:{kind:'custom',value:20,unit:'%'},
      labels:['身体指标类型','指标名称','数值','其他说明'],values:['自定义','未填写','20 %','未填写'],
      missing:[{field:'metric_name',label:'指标名称'}]},
    {category:'metric',details:{kind:'glucose',value:5.4,unit:'mmol/L',context:'fasting'},
      labels:['身体指标类型','血糖数值','测量情境','其他说明'],values:['血糖','5.4 mmol/L','空腹','未填写']}
  ];
  const first=12;
  const records=cases.map(({category,details,missing},index)=>draft(first+index,{category,content:'核对示例',details,missing_required:missing||[]}));
  const response='请您核对这些草稿。自定义身体指标还缺指标名称，方便直接回复告诉我吗？';
  const f=await scenario([reply(102,records,response)]);
  try{
    await f.send('把这几条记录分别整理出来。');
    const message=f.frame.locator('[data-ai-message-id="102"]');
    await message.getByText(response,{exact:true}).waitFor();
    for(const [index,{category,labels,values,missing}] of cases.entries()){
      const number=first+index,card=f.card(number),controls=f.controls(number);
      assert.deepEqual(await card.locator('dt').allTextContents(),['记录日期','发生时间',...labels],category);
      assert.deepEqual(await card.locator('dd').allTextContents(),['2026-11-12','12:05',...values],category);
      assert.equal(await card.getAttribute('data-record-state'),missing?'incomplete':'ready',category);
      assert.equal(await controls.getByRole('button',{name:'确认记录'}).count(),missing?0:1,category);
      const inOrder=await card.evaluate((article,draftId)=>{
        const message=article.closest('[data-ai-message-id]');
        const answer=message.querySelector('[data-ai-answer-body]');
        const controls=message.querySelector('[data-ai-record-draft-controls="'+draftId+'"]');
        const precedes=(earlier,later)=>!!(earlier.compareDocumentPosition(later)&Node.DOCUMENT_POSITION_FOLLOWING);
        return precedes(article,answer)&&precedes(answer,controls);
      },id(number));
      assert.equal(inOrder,true,category);
    }
    assert.equal(await f.frame.getByRole('button',{name:'补充信息'}).count(),0);
    assert.equal(await message.locator('[data-ai-collaboration]').count(),0);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('七类核对卡保留已知时间段并忽略无效时间段',async()=>{
  const cases=[
    ['diet','evening',{meal_type:'dinner',food:'清蒸鱼'},'晚上 · 待补时间'],
    ['water','morning',{amount_ml:250},'早上 · 待补时间'],
    ['activity','afternoon',{activity:'散步'},'下午 · 待补时间'],
    ['sleep','night',{duration_hours:7.5},'深夜 · 待补时间'],
    ['environment','noon',{temperature_c:24},'中午 · 待补时间'],
    ['medical','forenoon',{name:'门诊复查'},'上午 · 待补时间'],
    ['metric','dawn',{kind:'weight',value:68,unit:'kg'},'凌晨 · 待补时间'],
    ['metric','toString',{kind:'glucose',value:5.4,unit:'mmol/L'},'未填写']
  ];
  const first=20;
  const records=cases.map(([category,record_time_hint,details],index)=>draft(first+index,{
    ...required,id:id(first+index),category,record_time_hint,details
  }));
  const f=await scenario([reply(102,records,'这些草稿还缺日期或具体时间，请直接回复补充。')]);
  try{
    await f.send('请整理这些还缺日期或时间的记录。');
    for(const [index,[category,_hint,_details,display]] of cases.entries()){
      const number=first+index,card=f.card(number);
      await card.waitFor();
      assert.deepEqual((await card.locator('dd').allTextContents()).slice(0,2),['2026-11-12',display],category);
      assert.equal(await card.getAttribute('data-record-state'),'incomplete',category);
      assert.equal(await f.controls(number).getByRole('button',{name:'确认记录'}).count(),0,category);
    }
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('晚上只有时间段时可以直接补几点，更新后只显示时分并可确认',async()=>{
  const pending=draft(28,{...required,id:id(28),category:'diet',content:'晚饭吃了清蒸鱼',
    details:{meal_type:'dinner',food:'清蒸鱼'},record_time_hint:'evening'});
  const complete=draft(29,{category:'diet',content:pending.content,details:pending.details,
    occurred_at:'2026-11-12T20:30:42+08:00',record_time_hint:'evening',supersedes_draft_id:id(28)});
  const replies=[reply(102,[pending],'今天晚上的这顿饭大约是几点？'),reply(104,[complete],'已补上时间，请核对这份草稿。')];
  const f=await scenario(replies,{height:screenshotDir?930:844});
  try{
    await f.send('记录今天晚上吃了清蒸鱼。');
    const initial=f.card(28);
    await initial.waitFor();
    assert.deepEqual((await initial.locator('dd').allTextContents()).slice(0,3),['2026-11-12','晚上 · 待补时间','晚餐']);
    assert.equal(await f.controls(28).getByRole('button',{name:'确认记录'}).count(),0);
    assert.equal(await f.frame.getByRole('button',{name:'补充信息'}).count(),0);
    await f.photograph(initial,'晚上待补时间.png');
    await f.page.waitForFunction(()=>!FAWNSEE_MOBILE_AI.isBusy()&&!FAWNSEE_MOBILE_AI.aiCooldownLeft());
    await f.send('晚上八点半。');
    const updated=f.card(29),confirm=f.controls(29).getByRole('button',{name:'确认记录'});
    await confirm.waitFor();
    assert.deepEqual((await updated.locator('dd').allTextContents()).slice(0,3),['2026-11-12','20:30','晚餐']);
    assert.equal(await initial.getAttribute('data-record-state'),'superseded');
    assert.equal(await f.controls(28).count(),0);
    await confirm.click();
    await updated.getByRole('button',{name:'查看记录'}).waitFor();
    assert.deepEqual(await f.page.evaluate(()=>[...recordTest.decisions.keys()]),[id(29)]);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('饮食草稿显示实际餐照，确认后重新打开能看见同一张照片',async()=>{
  const meal=draft(8,{category:'diet',content:'午饭吃了清蒸鱼',details:{food:'清蒸鱼',meal_type:'lunch'},photo_file_ids:[83]});
  const f=await scenario([reply(102,[meal])],{height:screenshotDir?1030:844});
  try{
    await f.send('把这张餐照和午饭内容一起记进饮食记录。',true);
    const card=f.card(8);
    await card.getByText('将随记录保存的照片',{exact:true}).waitFor();
    const preview=card.getByRole('img',{name:'记录照片 1'});
    await preview.waitFor();
    assert.equal(await preview.getAttribute('src'),'data:image/webp;base64,'+syntheticPhoto.encoded);
    assert.ok(await preview.evaluate(async image=>{await image.decode();return image.naturalWidth>0;}));
    await f.photograph(card,'饮食确认草稿照片.png');
    assert.deepEqual(await f.page.evaluate(()=>recordTest.calls.find(entry=>entry.action==='chat.send').params.attachments),[83]);
    assert.equal(await f.page.evaluate(()=>recordTest.records.size),0);
    assert.equal(await f.page.evaluate(()=>recordTest.files.get(83)),syntheticPhoto.encoded);

    await f.controls(8).getByRole('button',{name:'确认记录'}).click();
    await card.getByText('已随记录保存 1 张照片',{exact:true}).waitFor();
    const open=async()=>{
      await card.getByRole('button',{name:'查看记录'}).click();
      await f.page.waitForFunction(()=>location.hash==='#detail/r42');
      const photo=f.page.locator('[data-record-photo-detail] img');
      await photo.waitFor();
      assert.equal(await photo.getAttribute('src'),'data:image/webp;base64,'+syntheticPhoto.encoded);
      assert.deepEqual(await photo.evaluate(async image=>{
        await image.decode();
        return {loaded:image.naturalWidth>0,fit:getComputedStyle(image).objectFit};
      }),{loaded:true,fit:'contain'});
      const mealContent=await photo.evaluate(image=>image.closest('section.card').textContent);
      assert.match(mealContent,/清蒸鱼/);
    };
    await open();
    await f.page.evaluate(()=>go('#ai',true));
    await card.getByRole('button',{name:'查看记录'}).waitFor();
    await open();

    const saved=await f.page.evaluate(()=>({
      records:[...recordTest.records.values()].map(r=>r.details),
      uploadCount:recordTest.calls.filter(x=>x.action==='files.upload').length,
      readFiles:recordTest.calls.filter(x=>x.type==='record.photo.get').map(x=>x.params.file_id),
    }));
    assert.deepEqual(saved.records,[{meal_type:'lunch',food:'清蒸鱼',photo_file_ids:[83]}]);
    assert.equal(saved.uploadCount,1);
    assert.ok(saved.readFiles.length>=1&&saved.readFiles.every(fileId=>fileId===83));
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('图文更新旧记录时显示全部待保存照片，暂时读取失败可以重试',async()=>{
  const cases=[
    {category:'environment',operation:'update',details:{temperature_c:24,description:'客厅已经通风'},old:{temperature_c:22}},
    {category:'medical',operation:'supplement',details:{name:'视力检查',location:'社区门诊'},old:{name:'视力检查'}},
  ];
  for(const [index,width] of [320,375,390,430].entries()){
    const {category,operation,details,old}=cases[index%cases.length],number=58+index;
    const changed=draft(number,{category,operation,details,content:'图文调整',photo_file_ids:[82,83],
      target:{...target,before:{...target.before,details:old}}});
    const f=await scenario([reply(102,[changed])],{width,height:1030,seedPhotos:[82],photoUnavailable:83});
    try{
      await f.send('把新照片也附到刚才那条。',true);
      const card=f.card(number);
      const prior=card.getByRole('img',{name:'记录照片 1'});
      await prior.waitFor();
      assert.equal(await prior.getAttribute('src'),'data:image/webp;base64,'+samplePriorPhoto);
      const retry=card.getByRole('button',{name:'照片暂未加载，点击重试'});
      await retry.waitFor();
      await f.page.evaluate(()=>{recordTest.photoUnavailable=null;});
      await retry.click();
      const preview=card.getByRole('img',{name:'记录照片 2'});
      await preview.waitFor();
      assert.equal(await preview.getAttribute('src'),'data:image/webp;base64,'+syntheticPhoto.encoded);
      assert.ok(await card.evaluate(item=>item.scrollWidth<=item.clientWidth+1));
      if(width===390)await f.photograph(card,'旧记录照片核对.png');
      await f.controls(number).getByRole('button',{name:operation==='update'?'确认修改':'确认补充'}).click();
      await card.getByRole('button',{name:'查看记录'}).click();
      const images=f.page.locator('[data-record-photo-detail] img');
      await images.nth(1).waitFor();
      assert.equal(await images.count(),2);
      assert.deepEqual(await f.page.evaluate(()=>recordTest.records.get(31).details.photo_file_ids),[82,83]);
      assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  }
});

for(const [operation,number,record,text] of [
  ['新增',1,draft(1,{category:'diet',content:'午饭吃了清蒸鱼',details:{food:'清蒸鱼',meal_type:'lunch'}}),'把图里这顿午餐记下来。'],
  ['修改',2,edit,'按照片把昨天这条饮水记录改成350毫升。'],
  ['补充',3,supplement,'根据照片给这条旧饮水记录补充备注。']
]){
  test(operation+'，实际对话选择图片后核对并保存，重开仍显示回执',async()=>{
    const f=await scenario([reply(102,[record])]);
    try{
      await f.send(text,true);
      const card=f.card(number),button=f.controls(number).locator('[data-parent-action="ai-record-confirm"]');
      await button.waitFor();
      assert.equal(await f.page.evaluate(()=>recordTest.calls.find(x=>x.action==='chat.send').params.attachments.length),1);
      assert.equal(await f.page.evaluate(()=>recordTest.decisions.size),0);
      if(number!==1){assert.match(await card.textContent(),/原记录.*确认后/);assert.match(await card.textContent(),/250 毫升/);}
      if(number===2)await f.photograph(card,'记录助手-修改确认.png');
      await button.click();
      await card.locator('[data-parent-action="ai-record-open"]').waitFor();
      assert.equal(await card.getAttribute('data-record-state'),'saved');
      const log=await f.page.evaluate(()=>recordTest.calls.filter(x=>x.action==='chat.records.confirm'));
      assert.equal(log.length,1);
      assert.equal(log[0].params.assistant_message_id,102);
      assert.equal(log[0].params.draft_id,id(number));
      assert.equal(log[0].params.request_id,id(number));
      assert.equal(await f.page.evaluate(()=>location.hash),'#ai');
      await f.page.evaluate(()=>FAWNSEE_MOBILE_AI.hydrate());
      await f.page.evaluate(()=>render());
      await card.locator('[data-parent-action="ai-record-open"]').waitFor();
      assert.equal(await card.getAttribute('data-record-state'),'saved');
      if(number===2){
        await card.getByRole('button',{name:'查看记录'}).click();
        await f.page.waitForFunction(()=>location.hash==='#detail/r31');
      }
      assert.deepEqual(f.errors,[]);
    }finally{await f.close();}
  });
}

test('必填空缺让用户继续回答，明确拒绝后显示无法保存',async()=>{
  const f=await scenario([reply(102,[required],'请补充什么时候发生。'),reply(104,[refused],'缺少发生时间，这条记录暂时无法保存。')]);
  try{
    await f.send('请记我喝了 350 毫升水。');
    const card=f.card(4);
    await card.waitFor();
    assert.equal(await card.getAttribute('data-record-state'),'incomplete');
    assert.deepEqual((await card.locator('.f-rc-field dd').allTextContents()).slice(0,2),['2026-11-12','未填写']);
    assert.equal(await f.controls(4).locator('[data-parent-action="ai-record-confirm"]').count(),0);
    assert.equal(await f.frame.getByRole('button',{name:'补充信息'}).count(),0);
    assert.equal(await f.page.getByText('已整理记录草稿，请逐条核对后保存。',{exact:true}).count(),0);
    await f.photograph(card,'记录助手-必填待补.png');
    await f.page.waitForFunction(()=>!FAWNSEE_MOBILE_AI.isBusy()&&!FAWNSEE_MOBILE_AI.aiCooldownLeft());
    await f.send('我不想提供时间。');
    const last=f.card(5);
    await last.waitFor();
    assert.equal(await last.getAttribute('data-record-state'),'blocked');
    assert.match(await last.textContent(),/无法保存/);
    assert.equal(await last.locator('button').count(),0);
    assert.equal(await f.page.evaluate(()=>recordTest.decisions.size),0);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('回答必填问题后出现完整确认卡，较早的不全草稿仍不能保存',async()=>{
  const complete=draft(7,{supersedes_draft_id:id(4)});
  const f=await scenario([reply(102,[required],'请补充具体发生时间。'),reply(104,[complete])]);
  try{
    await f.send('请记我喝了 350 毫升水。');
    await f.card(4).waitFor();
    await f.page.waitForFunction(()=>!FAWNSEE_MOBILE_AI.isBusy()&&!FAWNSEE_MOBILE_AI.aiCooldownLeft());
    await f.send('昨天中午12:05。');
    const current=f.card(7);
    await f.controls(7).getByRole('button',{name:'确认记录'}).click();
    await current.getByRole('button',{name:'查看记录'}).waitFor();
    assert.equal(await f.card(4).getAttribute('data-record-state'),'superseded');
    assert.equal(await f.controls(4).count(),0);
    assert.deepEqual(await f.page.evaluate(()=>[...recordTest.decisions.keys()]),[id(7)]);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('可选备注为空仍可保存，且取消缺项卡不会写入记录',async()=>{
  const f=await scenario([reply(102,[optional,required],'请核对完整的记录。若愿意，其他说明可直接回复补上。')],{pending:true});
  try{
    await f.send('记今天 12:05 饮水 350 毫升，备注留空。');
    const card=f.card(6);
    const confirm=f.controls(6).getByRole('button',{name:'确认记录'});
    await confirm.waitFor();
    assert.match(await card.textContent(),/其他说明未填写/);
    assert.equal(await f.frame.getByRole('button',{name:'补充信息'}).count(),0);
    await f.controls(4).getByRole('button',{name:'暂不记录'}).click();
    await f.card(4).locator('[role="status"]').getByText('已取消').waitFor();
    assert.equal(await f.page.evaluate(()=>recordTest.records.size),0);
    await confirm.click();
    await card.locator('[data-parent-action="ai-record-open"]').waitFor();
    assert.equal(await card.getAttribute('data-record-state'),'pending_sync');
    assert.match(await card.textContent(),/已存本机.*继续同步/s);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

test('未知结果可重试同一请求，不变成重复提交',async()=>{
  const f=await scenario([reply(102,[edit])],{failNext:'network'});
  try{
    await f.send('把这条饮水改成350毫升。');
    const card=f.card(2),button=f.controls(2).getByRole('button',{name:'确认修改'});
    await button.click();
    await card.getByRole('alert').waitFor();
    assert.equal(await card.getAttribute('data-record-state'),'ready');
    await button.click();
    await card.getByRole('button',{name:'查看记录'}).waitFor();
    assert.deepEqual(await f.page.evaluate(()=>recordTest.calls.filter(x=>x.action==='chat.records.confirm').map(x=>x.params.request_id)),[id(2),id(2)]);
    assert.equal(await f.page.evaluate(()=>recordTest.records.size),1);
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});

for(const width of [320,375,390,430])test('生成页面在 '+width+'px 无横向溢出，主次操作至少 44px',async()=>{
  const long='较长的备注示例。'.repeat(28);
  const f=await scenario([reply(102,[{...edit,details:{...edit.details,notes:long}},required,supplement])],{width});
  try{
    await f.send('核对多条生活记录。');
    await f.controls(2).getByRole('button',{name:'确认修改'}).waitFor();
    const layout=await f.frame.locator('html').evaluate(root=>({
      inner:root.clientWidth,scroll:root.scrollWidth,
      buttons:[...root.querySelectorAll('[data-ai-record-draft-controls] .f-rc-button')].map(button=>({width:button.offsetWidth,height:button.offsetHeight}))
    }));
    assert.equal(layout.scroll,layout.inner);
    assert.equal(layout.buttons.length,5);
    assert.ok(layout.buttons.every(button=>button.width>=44&&button.height>=44));
    assert.deepEqual(f.errors,[]);
  }finally{await f.close();}
});
