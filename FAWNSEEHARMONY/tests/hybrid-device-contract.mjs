import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import { dispatchBusiness } from '../entry/src/main/ets/services/HybridBusinessApi.js';

const assets = new URL('../entry/src/main/resources/rawfile/fawnsee-web/', import.meta.url);
const bridge = fs.readFileSync(new URL('harmony-bridge.js', assets), 'utf8');
const html = fs.readFileSync(new URL('index.html', assets), 'utf8');
const entry = 'resource://rawfile/fawnsee-web/index.html';
const services = new URL('../entry/src/main/ets/services/',import.meta.url);

function source(file) {return fs.readFileSync(new URL(file,services),'utf8');}
function run(sourceText,globals,returned) {
  const js=stripTypeScriptTypes(sourceText,{mode:'transform'});
  return vm.runInNewContext(js+'\n'+returned,globals);
}

function page(url, child = false) {
  const sent = [], replies = [];
  const state = { location: { href: url }, FawnseeArkTransport: {
    ready() {}, postMessage(key, message) { sent.push({key, message: JSON.parse(message)}); }
  }, FAWNSEE_NATIVE: {receive(value) {replies.push(value);}} };
  state.window = state;
  state.top = child ? {} : state;
  vm.runInNewContext(bridge, state);
  return {state, sent, replies};
}

test('local page installs bridge before app boot and holds calls until native activation', () => {
  assert.ok(html.indexOf('src="harmony-bridge.js"') >= 0);
  assert.ok(html.indexOf('src="harmony-bridge.js"') < html.indexOf('src="app.js"'));
  for (const [url, child] of [['https://example.org/index.html',false],[entry,true]]) {
    assert.equal(page(url,child).state.AndroidBridge, undefined);
  }
  const {state,sent} = page(entry+'#auth/login');
  const request = {id:'mobile-1',action:'app.bootstrap',params:{}};
  state.AndroidBridge.postMessage(JSON.stringify(request));
  assert.equal(sent.length,0);
  state.__activateFawnseeBridge('test-only-challenge');
  assert.deepEqual(sent,[{key:'test-only-challenge',message:request}]);
});

test('chunked data retains exact photo and non-ASCII text across transport', () => {
  const {state,replies} = page(entry);
  const content = JSON.stringify({id:'photos',ok:true,result:{text:'餐照',data:'q'.repeat(550000)}});
  for(let start=0;start<content.length;start+=192000) state.__fawnseeInbox.append('photos',content.slice(start,start+192000));
  state.__fawnseeInbox.finish('photos');
  assert.deepEqual(replies,[content]);
});

test('real photo contract keeps the original bytes through staging, upload, record save and reopen', async () => {
  const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAJX4OE8AAAAASUVORK5CYII=','base64');
  const original = image.toString('base64'), storage = new Map(), nonce = randomUUID;
  let saved, uploaded = '', uploads = 0;
  const native = {
    id:nonce, owner:()=>2, scope:()=>'', resetJournal:async()=>'', guestCleanupPending:async()=>false,
    restore:async()=>{}, consent:async()=>true, allowed:()=>true,
    async checkPhoto(encoded,mime,size){
      assert.equal(mime,'image/png');
      assert.equal(size,image.length);
      assert.deepEqual(Buffer.from(encoded,'base64'),image);
    },
    async scopedFile(key,op,value){if(op==='get')return storage.get(key)||'';if(op==='set')storage.set(key,value);return '';},
    async rpc(service,method,args,kwargs){
      if(service==='files'&&method==='upload_bytes'){uploaded=args[2].value;uploads++;return 17;}
      if(service==='files'&&method==='get_file')return {id:17,user_id:2,media_type:'image/png',size_bytes:image.length,content:uploaded};
      if(service==='health'&&method==='add_life_record'){saved=kwargs;return {id:9};}
      throw Error('Unexpected service '+service+'.'+method);
    }
  };
  const payload = {_fawnsee_actor_id:2};
  const staged=await dispatchBusiness('record.photo.stage',{...payload,image:{media_type:'image/png',name:'餐照.png',data_base64:original,size_bytes:image.length}},native);
  assert.ok(staged.file_id<0);
  assert.equal(staged.src,'data:image/png;base64,'+original);
  const readDraft=await dispatchBusiness('record.photo.get',{...payload,file_id:staged.file_id},native);
  assert.equal(readDraft.src,staged.src);
  await dispatchBusiness('member.request',{action:'health.records.add',request_id:nonce(),payload:{...payload,category:'diet',content:'测试餐食',details:{photo_file_ids:[staged.file_id]}}},native);
  assert.equal(saved.content,'测试餐食');
  assert.deepEqual(saved.details.photo_file_ids,[17]);
  const reopened=await dispatchBusiness('record.photo.get',{...payload,file_id:saved.details.photo_file_ids[0]},native);
  assert.equal(reopened.src,staged.src);
  assert.equal(uploads,1);
  assert.deepEqual(Buffer.from(uploaded,'base64'),image);
});

test('system picker and camera return real data or an explicit cancellation', async () => {
  let picked=[];
  const code=source('HybridDevice.ets');
  const block=code.slice(code.indexOf('function integerParam('), code.indexOf('async function update('));
  const pick=run('let pickerOpen=false;'+block,{
    actorId:()=>2, capturePhoto:async()=>picked, pickPhotos:async()=>picked, chooseHybridDocument:async()=>[],
  },'pick');
  for(const source of ['gallery','camera']){
    const result=await pick({},'native.pickImage',{max_count:1,purpose:'general',source});
    assert.equal(result.cancelled,true);
    assert.equal(result.images.length,0);
  }
  picked=['data:image/png;base64,AQIDBA=='];
  for(const source of ['gallery','camera']){
    const result=await pick({},'native.pickImage',{max_count:1,purpose:'general',source});
    assert.equal(result.cancelled,false);
    assert.equal(result.images[0].size_bytes,4);
    assert.equal(result.images[0].data_base64,'AQIDBA==');
  }
});

test('相机仅接受 SDK 成功回执和指定输出里的真实照片，空返回、无设备与取消均不读临时文件',async()=>{
  let devices=[{cameraPosition:1}], answer,reads=0,opens=0,unlinks=0
  const code=source('Camera.ets').replace(/^import .*$/gm,'').replaceAll('export ','')
  const globals={camera:{CameraPosition:{CAMERA_POSITION_BACK:1},getCameraManager:()=>({getSupportedCameras:()=>devices})},
    fs:{OpenMode:{CREATE:1,READ_WRITE:2},open:async()=>{opens++;return 1},close:async()=>{},unlink:async()=>{unlinks++}},
    fileUri:{getUriFromPath:p=>'file://app'+p},util:{generateRandomUUID:()=>'qa'},readPhoto:async()=>{reads++;return 'real-image'},
    cameraPicker:{PickerMediaType:{PHOTO:'photo'},pick:async(_c,_types,profile)=>{assert.equal(profile.cameraPosition,devices[0].cameraPosition);return answer}}}
  const {capturePhoto,CameraUnavailable}=run(code,globals,'({capturePhoto,CameraUnavailable})')
  const ctx={cacheDir:'/cache'}, valid={resultCode:0,resultUri:'file://app/cache/capture-qa.jpg',mediaType:'photo'}
  for(answer of [undefined,null,{...valid,resultUri:''},{...valid,resultUri:'file://media/unknown'},{...valid,mediaType:'video'}])
    await assert.rejects(capturePhoto(ctx),e=>e instanceof CameraUnavailable && !/undefined|resultCode/.test(e.message))
  answer={resultCode:-1,resultUri:''};assert.equal((await capturePhoto(ctx)).length,0);assert.equal(reads,0)
  devices=[];await assert.rejects(capturePhoto(ctx),/没有可用的系统相机/);assert.equal(reads,0)
  devices=[{cameraPosition:2}];answer=valid
  assert.equal((await capturePhoto(ctx))[0],'real-image');assert.equal(reads,1);assert.equal(unlinks,opens)
})

test('主对话框控件延后装载或被替换后，照片和更多功能仍响应，同一来源只发出一次',()=>{
  const code=fs.readFileSync(new URL('app.js',assets),'utf8')
  const body=code.slice(code.indexOf('  function bindChildComposer()'),code.indexOf('  const CHILD='))
  const refs=new Map(), parentEvents=[], win=new EventTarget(), doc=new EventTarget()
  class Element extends EventTarget{
    constructor(){super();this.hidden=true;this.options=new Map()}
    setAttribute(k,v){this.options.set(k,v)}
    getAttribute(k){return this.options.get(k)}
    querySelector(k){return refs.get(k)}
    querySelectorAll(k){return k==='[data-ai-source]'?[refs.get('gallery')]:[]}
  }
  const all=['[data-ai-tools]','[data-ai-sheet]','[data-ai-voice-bar]','[data-primary-chat-input]','[data-ai-bigvoice]','[data-ai-photo]','[data-ai-tools-tray]','[data-ai-tool-file]','gallery']
  const mount=()=>{for(const key of all)refs.set(key,new Element());refs.get('gallery').setAttribute('data-ai-source','gallery')}
  doc.querySelector=k=>refs.get(k)
  doc.querySelectorAll=()=>refs.size?[refs.get('gallery'),refs.get('[data-ai-tool-file]')]:[]
  const bind=run(body,{document:doc,window:win,AbortController,parent:{postMessage:e=>parentEvents.push(e)}},'bindChildComposer')
  bind();mount();bind()
  const original=refs.get('[data-ai-photo]');original.dispatchEvent(new Event('click'))
  assert.equal(refs.get('[data-ai-sheet]').hidden,false)
  mount();bind();bind()
  original.dispatchEvent(new Event('click'));assert.equal(refs.get('[data-ai-sheet]').hidden,true)
  refs.get('[data-ai-photo]').dispatchEvent(new Event('click'));assert.equal(refs.get('[data-ai-sheet]').hidden,false)
  refs.get('gallery').dispatchEvent(new Event('click'));assert.equal(refs.get('[data-ai-sheet]').hidden,true)
  assert.equal(parentEvents.filter(e=>e.action==='ai-photo-source'&&e.value==='gallery').length,1)
  refs.get('[data-ai-tools]').dispatchEvent(new Event('click'));assert.equal(refs.get('[data-ai-tools-tray]').hidden,false)
})

test('只有明确选择后才从相册取图，按相册来源回传；取消或账号已变更不返回照片',async()=>{
  class CameraUnavailable extends Error{}
  class HybridError extends Error{constructor(code,message){super(message);this.code=code}}
  const raw=source('HybridDevice.ets'),code=raw.slice(raw.indexOf('function integerParam('),raw.indexOf('async function update('))
  let offered=0,albums=0,chooses=false,owner=2,switches=false,back=false
  const pick=run('let pickerOpen=false;'+code,{HybridError,CameraUnavailable,actorId:()=>owner,
    capturePhoto:async()=>{if(back)return [];throw new CameraUnavailable('没有相机。')},
    offerCameraGallery:async()=>{offered++;if(switches)owner++;return chooses},
    pickPhotos:async count=>{albums++;assert.equal(count,1);return ['data:image/png;base64,AQIDBA==']}
  },'pick')
  const take=()=>pick({},'native.pickImage',{source:'camera',purpose:'ai',max_count:1})
  assert.equal((await take()).cancelled,true);assert.equal(albums,0)
  chooses=true;const image=await take();assert.equal(image.source,'gallery');assert.equal(image.images.length,1);assert.equal(image.cancelled,false)
  back=true;assert.equal((await take()).cancelled,true);assert.equal(offered,2)
  back=false;switches=true;await assert.rejects(take(),{code:'account_changed'});assert.equal(albums,1)
})

test('rejecting a first-launch permission does not interrupt the others or repeat on restart', async () => {
  let persisted=new Map(), denied=[];
  const prefs={getSync:(k,v)=>persisted.get(k)??v,put:async(k,v)=>persisted.set(k,v),flush:async()=>{}};
  const block=source('HybridPermissions.ets').replace(/^import .*$/gm,'').replaceAll('export ','');
  const makeApp=()=>run(block,{requirePrivacy:()=>{},preferences:{getPreferencesSync:()=>prefs},
    notificationManager:{isNotificationEnabled:async()=>false,requestEnableNotification:async()=>{denied.push(['notifications']);throw Error('denied');}},
    bundleManager:{getBundleInfoForSelfSync:()=>({appInfo:{accessTokenId:7}}),BundleFlag:{GET_BUNDLE_INFO_WITH_APPLICATION:1}},
    abilityAccessCtrl:{GrantStatus:{PERMISSION_GRANTED:0},createAtManager:()=>({checkAccessTokenSync:()=>-1,
      requestPermissionsFromUser:async(_ctx,list)=>{denied.push(Array.from(list));throw Error('denied');}})}
  },'requestHybridPermissionsOnce');
  const launch=makeApp();await Promise.all([launch({}),launch({})]);await makeApp()({});
  assert.deepEqual(denied,[['notifications'],['ohos.permission.MICROPHONE'],['ohos.permission.READ_CALENDAR','ohos.permission.WRITE_CALENDAR']]);
});

test('AppGallery unknown offered version is not displayed as the installed version', async () => {
  const code=source('HybridDevice.ets');
  const block=code.slice(code.indexOf('async function update('),code.indexOf('export async function hybridDevice('));
  const update=run("let phase='idle',issue='';"+block,{
    bundleManager:{BundleFlag:{GET_BUNDLE_INFO_DEFAULT:0},getBundleInfoForSelfSync:()=>({versionCode:11869,versionName:'1.6.17'})},
    checkMarketUpdate:async()=>'available',openMarketUpdate:async()=>'opened'
  },'update');
  const offer=await update({},'native.update.check');
  assert.equal(offer.current_version_code,11869);
  assert.equal(offer.phase,'available');
  assert.equal(offer.version_code,undefined);
  assert.equal(offer.version_name,undefined);
  assert.equal((await update({},'native.update.start')).phase,'installer_opened');
});

test('reading records one completed system share and skips a dismissed sheet', async () => {
  const code=fs.readFileSync(new URL('app.js',assets),'utf8');
  const line=code.split('\n').find(value=>value.includes('async function share(s,copy)'));
  const build=({shared,copy=false})=>{
    const effects=[];
    const view={busy:new Set(),article:{title:'阅读'},id:1,shareOpen:true};
    const share=vm.runInNewContext('('+line.trim()+')',{
      owner:7, root:{FAWNSEE_READING:{safeUrl:()=>''}},api:()=>({native:async()=>({shared}),uuid:randomUUID}),
      current:()=>true, request:async(action)=>{effects.push(action);return {};},
      commit:()=>{},paintLayers:()=>{},paint:()=>{},ctx:{toast:()=>{}}
    });
    return share(view,copy).then(()=>effects);
  };
  assert.deepEqual(await build({shared:false}),[]);
  assert.deepEqual(await build({shared:true}),['reading.share']);
  assert.deepEqual(await build({copy:true}),['reading.share']);
});
