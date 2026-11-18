"""Exercise the installed HarmonyOS app using hdc and a dedicated, logged-in test member."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', type=Path, help='Optional directory for screenshots of the test member')
parser.add_argument('--photo-conversation', type=int, help='Optional conversation ID containing one pending synthetic diet photo draft')
parser.add_argument('--photo-sha256', help='SHA-256 of the photo supplied to that draft')
args = parser.parse_args()
if args.photo_conversation and not re.fullmatch('[0-9a-f]{64}', args.photo_sha256 or ''):
    parser.error('--photo-conversation needs --photo-sha256')
hdc = os.getenv('HDC_EXECUTABLE') or shutil.which('hdc')
target = os.getenv('HDC_TARGET')
if not hdc or not target:
    raise SystemExit('Set HDC_EXECUTABLE or add hdc to PATH, and set HDC_TARGET.')
env = {k: os.environ['FAWNSEE_QA_'+k] for k in ('URL', 'USERNAME', 'PASSWORD')}
base = env['URL'].rstrip('/')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
token = ''
layout_path = '/data/local/tmp/fawnsee_records_'+uuid.uuid4().hex+'.json'
tag = 'RQA'+uuid.uuid4().hex[:12]
day = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=8)).strftime('%Y-%m-%d')
clock = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=8)).strftime('%H:%M')

def http(path, body):
    headers = {'Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer '+token
    req = urllib.request.Request(base+path, json.dumps(body).encode(), headers)
    with opener.open(req, timeout=30) as response:
        return json.load(response)

def rpc(service, method, arguments, kwargs=None):
    payload = {'args':[owner, *arguments], 'kwargs':kwargs or {}, 'request_id':str(uuid.uuid4())}
    return http('/v1/rpc/'+service+'/'+method, payload)['result']

def records():
    offset, all_rows = 0, []
    while True:
        page = rpc('expansion', 'list_records', [{'limit':100,'offset':offset}])
        all_rows.extend(page['items'])
        offset += len(page['items'])
        if not page['items'] or offset >= page['total']:
            return all_rows

def erase(record_id):
    row = rpc('expansion','get_record',[{'record_id':record_id}])
    rpc('expansion','delete_record',[{'record_id':record_id,'base_version':row['version'],'reason':'自动验收清理'}])

def run(*cmd):
    p = subprocess.run([hdc,'-t',target,*map(str,cmd)],capture_output=True,timeout=40)
    out = (p.stdout+p.stderr).decode('utf-8','replace').strip()
    if p.returncode:
        raise RuntimeError(out)
    return out

def shell(*cmd):
    return run('shell',*cmd)

def nodes():
    shell('uitest','dumpLayout','-p',layout_path)
    tree = json.loads(shell('cat',layout_path))
    result = []
    def walk(node):
        a = node.get('attributes',{})
        b = list(map(int,re.findall(r'-?\d+',a.get('bounds',''))))
        if len(b)==4 and b[2]>b[0] and b[3]>b[1]:
            result.append((a,b))
        for child in node.get('children',[]):
            walk(child)
    walk(tree)
    return result

def lookup(value, field='text'):
    return next((b for a,b in nodes() if a.get(field)==value), None)

def scroll(up=True):
    view=nodes()
    w=max(b[2] for _,b in view)
    h=max(b[3] for _,b in view)
    y1,y2=int(h*.73),int(h*.29)
    shell('uitest','uiInput','swipe',w//2,y1 if up else y2,w//2,y2 if up else y1,800)
    time.sleep(.25)

def locate(value, field='text', direction=None):
    for _ in range(8):
        box = lookup(value,field)
        if box:
            return box
        if direction is not None:
            scroll(direction)
        else:
            time.sleep(.35)
    raise AssertionError('Expected '+field+' '+value)

def tap(value, field='text', direction=None):
    x,y,X,Y=locate(value,field,direction)
    shell('uitest','uiInput','click',(x+X)//2,(y+Y)//2)
    time.sleep(.35)

def type_into(name, value):
    x,y,X,Y=locate('record-input-'+name,'id',True)
    shell('uitest','uiInput','inputText',(x+X)//2,(y+Y)//2,value)
    time.sleep(.3)
    shell('uitest','uiInput','keyEvent','Back')
    time.sleep(.3)

def screenshot(label):
    if not args.artifacts:
        return
    args.artifacts.mkdir(exist_ok=True,parents=True)
    remote='/data/local/tmp/fawnsee_record_'+uuid.uuid4().hex+'.png'
    try:
        shell('uitest','screenCap','-p',remote)
        run('file','recv',remote,str((args.artifacts/(label+'.png')).resolve()))
    finally:
        shell('rm','-f',remote)

def home():
    tab=lookup('tab-records','id')
    if tab:
        tap('tab-records','id')
    locate('生活记录 · 凡思日常')

def printed(name):
    print('PASS',name,flush=True)

def seven_forms():
    kinds=[('diet','饮食','餐次'),('water','饮水','饮水量 · 毫升 *'),('activity','运动','运动方式 *'),
           ('sleep','睡眠','睡眠时长 · 小时 *'),('environment','居住环境','室温 · ℃'),
           ('medical','医疗检查','事项名称 *'),('metric','身体指标','体重数值 *')]
    for cat,label,field in kinds:
        home()
        tap('record-category-'+cat,'id',False)
        locate('记录'+label)
        locate(field,direction=True)
        screenshot('form-'+cat)
        printed(label+'原生表单')
        shell('uitest','uiInput','keyEvent','Back')
        time.sleep(.35)

def water_crud():
    home()
    tap('record-category-water','id',False)
    tap('500 毫升',direction=True)
    type_into('notes',tag)
    tap('保存记录',direction=True)
    locate('生活记录 · 凡思日常')
    own=[r for r in records() if (r.get('details') or {}).get('notes')==tag]
    assert len(own)==1 and own[0]['details']['amount_ml']==500
    rid=own[0]['id']
    printed('饮水 500 毫升已保存')
    tap('record-row-r'+str(rid),'id',True)
    tap('编辑记录',direction=True)
    tap('200 毫升',direction=True)
    type_into('reason',tag)
    tap('保存修改',direction=True)
    locate('生活记录 · 凡思日常')
    row=rpc('expansion','get_record',[{'record_id':rid}])
    assert row['details']['amount_ml']==200
    assert any(r['reason']==tag for r in rpc('expansion','list_record_revisions',[{'record_id':rid}])['items'])
    printed('同一条记录改为 200 毫升，并保存了修改原因')
    tap('record-row-r'+str(rid),'id',True)
    tap('删除记录',direction=True)
    tap('确认删除')
    locate('生活记录 · 凡思日常')
    assert not any(r['id']==rid for r in records())
    printed('确认删除后云端列表同步')

def existing_photo_draft(conversation_id, expected_digest):
    tap('tab-ai','id')
    tap('ai-icon-history','id')
    tap('ai-open-chat-'+str(conversation_id),'id',True)
    locate('确认记录',direction=True)
    photos=[(a,b) for a,b in nodes() if a.get('id','').startswith('ai-draft-photo-')]
    assert len(photos)==1, 'One visible pending photo is required.'
    fid=int(photos[0][0]['id'].split('-')[-1])
    import base64
    def fingerprint():
        f=rpc('files','get_file',[fid])
        return hashlib.sha256(base64.b64decode(f['content']['value'],validate=True)).hexdigest()
    assert fingerprint()==expected_digest
    screenshot('device-meal-draft')
    before={r['id'] for r in records()}
    tap('确认记录')
    tap('查看已保存记录',direction=True)
    locate('记录详情')
    added=[r for r in records() if r['id'] not in before and fid in (r.get('details') or {}).get('photo_file_ids',[])]
    assert len(added)==1 and added[0]['category']=='diet'
    locate('record-photo-'+str(fid),'id',True)
    screenshot('device-meal-detail')
    tap('record-photo-'+str(fid),'id',True)
    locate('关闭')
    screenshot('device-meal-full')
    assert fingerprint()==expected_digest
    printed('草稿核对、确认保存、重开同一条饮食记录及全屏照片')

if __name__=='__main__':
    manifest = Path(__file__).resolve().parents[1] / 'AppScope' / 'app.json5'
    bundle = json.loads(manifest.read_text(encoding='utf-8-sig'))['app']['bundleName']
    launch = shell('aa', 'start', '-a', 'EntryAbility', '-b', bundle)
    if 'start ability successfully' not in launch.lower():
        raise SystemExit(f'Could not start {bundle}: {launch}')
    locate('tab-ai', 'id')
    login=http('/v1/auth/login', {'username':env['USERNAME'],'password':env['PASSWORD']})
    token=login['access_token']
    owner=login['user']['id']
    probe=None
    try:
        probe=rpc('health','add_life_record',[],{
            'category':'water','occurred_at':day+'T'+clock+':00+08:00','content':'账号联调核验',
            'source':'manual','details':{'amount_ml':151,'notes':'auth-'+tag}
        })
        # Switch away and back to fetch current data even if the app was already on this tab.
        tap('tab-ai','id')
        home()
        locate('auth-'+tag,direction=True)
        printed('已在手机界面确认本次测试账号')
        seven_forms()
        water_crud()
        if args.photo_conversation:
            existing_photo_draft(args.photo_conversation,args.photo_sha256)
    finally:
        shell('rm','-f',layout_path)
        if probe is not None:
            erase(probe)
        for record in records():
            if (record.get('details') or {}).get('notes')==tag:
                erase(record['id'])

