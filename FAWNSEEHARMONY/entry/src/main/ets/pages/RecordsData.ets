import { util } from '@kit.ArkTS'
import { relationalStore } from '@kit.ArkData'
import { common } from '@kit.AbilityKit'
import { actorId, rpc, CloudNetworkError } from '../services/Cloud'

export type Fields = Record<string, string>
type Body = Record<string, Object>
interface Page<T> { items: T[]; total: number }
export interface LifeRow {
  id: number
  category?: string
  content?: string
  occurred_at?: string
  created_at?: string
  source?: string
  version: string
  linked_meal_id?: number
  details?: Body
  kind?: string
  measured_at?: string
  unit?: string
  value?: number
  diastolic?: number
  metric_name?: string
  context?: string
  notes?: string
}
export interface Photo {
  id: number
  uri: string
  operation: string
  local?:boolean
}
export interface Revision {
  created_at: string
  reason: string
  before?: LifeRow
  after?: LifeRow
}
export interface JournalRecord {
  key: string
  category: string
  fields: Fields
  photos: number[]
  raw: LifeRow
}
interface StoredDraft { fields: Fields; ids?: number[]; photos?: Photo[]; operation: string; baseVersion?:string }
interface StoredPhoto { id: number; user_id: number; media_type: string; content: string }
const contextZh: Fields = { 'unspecified':'未说明', 'fasting':'空腹', 'before_meal':'餐前', 'after_meal':'餐后', 'random':'随机' }
const contextEn: Fields = { '未说明':'unspecified', '空腹':'fasting', '餐前':'before_meal', '餐后':'after_meal', '随机':'random' }
export const metricNames: Fields = { 'weight':'体重', 'glucose':'血糖', 'blood_pressure':'血压', 'lipids':'血脂', 'height':'身高', 'custom':'自定义' }
export const metricUnits: Fields = { 'weight':'kg', 'glucose':'mmol/L', 'blood_pressure':'mmHg', 'lipids':'mmol/L', 'height':'cm', 'custom':'' }

export function uuid(): string { return util.generateRandomUUID(true) }
export function chinaDate(stamp: string = new Date().toISOString()): string {
  const ms = Date.parse(stamp)
  return Number.isFinite(ms) ? new Date(ms + 8 * 3600000).toISOString().slice(0, 16) : ''
}
export function newFields(cat: string): Fields {
  const now = chinaDate()
  const fields: Fields = { 'date':now.slice(0,10), 'time':now.slice(11,16) }
  if (cat === 'diet') fields['meal'] = '午餐'
  if (cat === 'water') fields['amount_ml'] = '250'
  if (cat === 'metric') {
    fields['kind'] = 'weight'
    fields['unit'] = 'kg'
    fields['context'] = '未说明'
  }
  return fields
}
function normalize(raw: LifeRow, observation: boolean): JournalRecord {
  const d: Body = raw.details ?? {}
  const f: Fields = {}
  Object.keys(d).forEach((key: string) => {
    const value = d[key]
    if (typeof value === 'number' || typeof value === 'string') f[key] = String(value)
  })
  const stamp = chinaDate((observation ? raw.measured_at : raw.occurred_at) ?? '')
  f['date'] = stamp.slice(0, 10)
  f['time'] = stamp.slice(11, 16)
  const category = observation ? 'metric' : raw.category ?? ''
  if (observation) {
    f['kind'] = raw.kind ?? 'weight'; f['value'] = String(raw.value ?? ''); f['unit'] = raw.unit ?? ''
    f['notes'] = raw.notes ?? ''; f['context'] = contextZh[raw.context ?? 'unspecified'] ?? '未说明'
    f['diastolic'] = String(raw.diastolic ?? ''); f['metric_name'] = raw.metric_name ?? ''
  } else if (category === 'diet') {
    const meal=f['meal_type'] || f['meal'] || '其他'
    const meals:Fields={'breakfast':'早餐','lunch':'午餐','dinner':'晚餐','snack':'加餐','other':'其他','unspecified':'未说明'}
    f['meal'] = meals[meal] || meal
    f['food'] = f['food'] || raw.content || ''
  } else if (category === 'activity') {
    f['activity'] = f['activity'] || f['activity_type'] || raw.content || ''
  } else if (category === 'medical') {
    f['name'] = f['name'] || raw.content || ''
  } else if (category === 'environment') {
    f['description'] = f['description'] || raw.content || ''
  }
  const ids = d['photo_file_ids']
  return { key:(observation?'o':'r')+raw.id, category:category, fields:f,
    photos:Array.isArray(ids) ? ids as number[] : [], raw:raw }
}
async function pages(method: string, actor: number): Promise<LifeRow[]> {
  let offset = 0
  const rows: LifeRow[] = []
  while (true) {
    if (actor !== actorId()) throw Error('账号已改变。')
    const args: Body = { 'limit':100, 'offset':offset }
    const response = await rpc<Page<LifeRow>>('expansion', method, [actor,args])
    rows.push(...response.items)
    offset += response.items.length
    if (offset >= response.total || !response.items.length) return rows
  }
}
export async function recordList(): Promise<JournalRecord[]> {
  const actor = actorId()
  const both = await Promise.allSettled([pages('list_records',actor), pages('list_observations',actor)])
  for(const result of both){
    if(result.status==='rejected'&&!(result.reason instanceof CloudNetworkError))throw result.reason as Error
  }
  const records=both[0],observations=both[1]
  if(records.status==='rejected')throw records.reason as Error
  if(observations.status==='rejected')throw observations.reason as Error
  const all = records.value.map((r: LifeRow) => normalize(r,false)).concat(observations.value.map((r: LifeRow) => normalize(r,true)))
  return all.sort((a: JournalRecord,b: JournalRecord) =>
    (b.fields['date']+b.fields['time']).localeCompare(a.fields['date']+a.fields['time']) ||
    (b.raw.created_at ?? '').localeCompare(a.raw.created_at ?? ''))
}
function identity(key: string): Body {
  if (!/^[ro][1-9]\d*$/.test(key)) throw Error('这条记录不存在。')
  const body: Body = {}; body[key.startsWith('o')?'observation_id':'record_id']=Number(key.slice(1)); return body
}
export async function getRecord(key: string): Promise<JournalRecord> {
  const obs = key.startsWith('o')
  const row = await rpc<LifeRow>('expansion', obs?'get_observation':'get_record',[actorId(),identity(key)])
  return normalize(row,obs)
}
export async function revisions(key: string): Promise<Revision[]> {
  if (!key.startsWith('r')) return []
  const body = identity(key); body['limit'] = 100
  const page = await rpc<Page<Revision>>('expansion','list_record_revisions',[actorId(),body])
  return page.items.filter((item: Revision) => !!item.before && !!item.after)
}
export function summary(cat: string,f: Fields): string {
  switch(cat) {
    case 'diet': return [f['meal'],f['food']].filter((x:string)=>!!x).join(' · ')
    case 'water': return '饮水 '+(f['amount_ml'] || '')+' 毫升'
    case 'activity': return [f['activity'], f['duration_minutes'] ? f['duration_minutes']+' 分钟':'', f['steps'] ? f['steps']+' 步':''].filter((s:string)=>!!s).join(' · ')
    case 'sleep': return '睡眠 '+(f['duration_hours'] || '')+' 小时'
    case 'environment': return [f['temperature_c']?f['temperature_c']+' ℃':'', f['humidity_percent']?'湿度 '+f['humidity_percent']+'%':'',f['description']].filter((s:string)=>!!s).join(' · ') || '居住环境记录'
    case 'medical': return f['name'] || '医疗检查'
    default: return (metricNames[f['kind']]||'指标')+' '+f['value']+(f['kind']==='blood_pressure'?'/'+f['diastolic']:'')+' '+ f['unit']
  }
}
export function shortNote(r:JournalRecord):string {
  const f=r.fields
  if (r.category==='diet' && f['calories_kcal']) return f['calories_kcal']+' 千卡'+(r.raw.details?.['estimated']===true?' · AI估算':'')
  if (f['kind']==='glucose') return f['context']
  return f['notes'] || f['location'] || ''
}
export class FieldSpec {
  key:string; label:string; hint:string; min:number; max:number; integer:boolean; required:boolean; type:string
  constructor(key:string,label:string,type:string='text',hint:string='',required:boolean=false,min:number=-Infinity,max:number=Infinity,integer:boolean=false) {
    this.key=key; this.label=label; this.type=type; this.hint=hint; this.required=required; this.min=min; this.max=max; this.integer=integer
  }
}
export function fieldsFor(cat:string, kind:string): FieldSpec[] {
  const notes = (hint:string) => new FieldSpec('notes','其他说明','area',hint)
  if(cat==='diet') return [new FieldSpec('meal','餐次','meal'),new FieldSpec('food','吃了什么','photo','',true),
    new FieldSpec('calories_kcal','热量 · 千卡','number','可选填',false,0,20000,true),notes('可选填，用于了解吃饭时的其他情况')]
  if(cat==='water') return [new FieldSpec('amount_ml','饮水量 · 毫升','number','',true,1,10000),notes('可选填，用于了解饮水时的其他情况')]
  if(cat==='activity') return [new FieldSpec('activity','运动方式','text','例如：散步',true),
    new FieldSpec('duration_minutes','时长/分钟','number','',false,0,1440,true),
    new FieldSpec('steps','步数','number','',false,0,200000,true),notes('可选填，用于了解运动时的其他情况')]
  if(cat==='sleep') return [new FieldSpec('duration_hours','睡眠时长 · 小时','number','',true,0,24),notes('可选填，用于了解睡眠时的其他情况')]
  if(cat==='environment') return [new FieldSpec('temperature_c','室温 · ℃','number','',false,-80,80),
    new FieldSpec('humidity_percent','湿度 · %','number','',false,0,100),new FieldSpec('description','环境情况','photo','可选填，用于详细了解居住环境')]
  if(cat==='medical') return [new FieldSpec('name','事项名称','text','例如：门诊复查',true),
    new FieldSpec('location','地点或机构'),new FieldSpec('description','具体事项','photo','建议填写，用于了解医疗情况')]
  const specs:FieldSpec[] = []
  if(kind==='custom'||kind==='lipids') specs.push(new FieldSpec('metric_name',kind==='custom'?'指标名称':'指标项目','text',kind==='custom'?'例如：体脂率':'例如：总胆固醇',kind==='custom'))
  const label=kind==='blood_pressure'?'收缩压':kind==='custom'?'':metricNames[kind]
  specs.push(new FieldSpec('value',label+'数值','number',metricUnits[kind]||'无单位',true,0.000001))
  if(kind==='blood_pressure') specs.push(new FieldSpec('diastolic','舒张压数值','number','mmHg',true,0.000001))
  if(kind==='glucose') specs.push(new FieldSpec('context','测量情境','context'))
  specs.push(notes('可选填，用于了解该项目身体的其他情况'))
  return specs
}
export function validatedFields(cat:string, values:Fields, photoCount:number, edit:boolean):Fields {
  const f:Fields = JSON.parse(JSON.stringify(values)) as Fields
  Object.keys(f).forEach((key:string)=> { f[key]=f[key].trim() })
  if(!/^\d{4}-\d{2}-\d{2}$/.test(f['date']) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(f['time'])) throw Error('请选择有效的记录日期和发生时间。')
  if(photoCount && cat==='diet' && !f['food']) f['food']='照片记录'
  if(photoCount && (cat==='medical'||cat==='environment') && !f['description']) f['description']='照片记录'
  if(photoCount && cat==='medical' && !f['name']) f['name']='照片记录'
  for(const spec of fieldsFor(cat,f['kind'])) {
    const v=f[spec.key]
    if(spec.required && !v) throw Error('请填写'+spec.label+'。')
    if(spec.type==='number' && v) {
      const n=Number(v)
      if(!Number.isFinite(n)||n<spec.min||n>spec.max||(spec.integer&&!Number.isInteger(n))) throw Error('请核对'+spec.label+'的数值。')
    }
  }
  if(cat==='environment' && !['description','temperature_c','humidity_percent','ventilation_minutes','quality_score','notes'].some((k:string)=>!!f[k])) throw Error('请至少填写一项环境情况。')
  if(f['kind']==='blood_pressure' && Number(f['diastolic'])>=Number(f['value'])) throw Error('舒张压应低于收缩压。')
  if(edit && !f['reason']) throw Error('请填写简短的修改原因。')
  return f
}
export async function storeRecord(cat:string, f:Fields, ids:number[], old:JournalRecord | undefined, op:string):Promise<void> {
  const actor=actorId(), stamp=f['date']+'T'+f['time']+':00+08:00'
  if(old?.raw.linked_meal_id) throw Error('此记录关联电脑版餐食明细，请在电脑版编辑。')
  if(cat==='metric') {
    const b:Body={'value':Number(f['value']),'unit':f['unit']||'','measured_at':stamp,'notes':f['notes']||'','context':contextEn[f['context']]||'unspecified'}
    if(f['kind']==='blood_pressure') b['diastolic']=Number(f['diastolic'])
    if(f['kind']==='custom'||f['kind']==='lipids') b['metric_name']=f['metric_name']||''
    if(old) { b['observation_id']=old.raw.id; b['base_version']=old.raw.version; b['reason']=f['reason'] }
    else b['kind']=f['kind']
    await rpc<LifeRow>('expansion',old?'update_observation':'create_observation',[actor,b],{} as Body,op)
    return
  }
  const details:Body={}
  const existing:Body=old?.raw.details ?? {}
  const visible=fieldsFor(cat,'').map((spec:FieldSpec)=>spec.key)
  Object.keys(existing).forEach((k:string)=>{
    if(!visible.includes(k) && !(cat==='diet' && k==='meal_type') && !(cat==='activity' && k==='activity_type')) details[k]=existing[k]
  })
  for(const spec of fieldsFor(cat,'')) {
    const val=f[spec.key]
    if(val?.length) details[spec.key]=spec.type==='number'?Number(val):val
  }
  if(cat==='medical') details['event_type']=details['event_type']||'other'
  if(['diet','environment','medical'].includes(cat)) details['photo_file_ids']=ids
  let content=summary(cat,f)
  if(cat==='activity') content=f['activity']
  if(cat==='environment') content=f['description']||'居住环境记录'
  if(f['notes']&&cat!=='medical') content+='；'+f['notes']
  const changes:Body={'category':cat,'occurred_at':stamp,'content':content,'details':details}
  if(old) {
    const b:Body={'record_id':old.raw.id,'base_version':old.raw.version,'reason':f['reason'],'changes':changes}
    await rpc<LifeRow>('expansion','update_record',[actor,b],{} as Body,op)
  } else {
    changes['source']='manual'
    await rpc<number>('health','add_life_record',[actor],changes,op)
  }
}
export async function removeRecord(r:JournalRecord,op:string):Promise<void> {
  const b=identity(r.key); b['base_version']=r.raw.version; b['reason']='用户在手机端删除记录'
  await rpc<Object>('expansion',r.category==='metric'?'delete_observation':'delete_record',[actorId(),b],{} as Body,op)
}
export function knownPhoto(id:number):Photo { return {id:id,uri:'',operation:uuid()} }
export async function uploadPhoto(photo:Photo,actor:number):Promise<number> {
  if(photo.id>0) return photo.id
  const match=/^data:image\/(jpeg|png|webp);base64,([A-Za-z0-9+/]+=*)$/.exec(photo.uri)
  if(!match || match[2].length>Math.ceil(2*1024*1024/3)*4) throw Error('请选择不超过 2 MB 的 JPG、PNG 或 WebP 照片。')
  const bytes:Body={}; bytes['$__type']='bytes'; bytes['value']=match[2]
  const name='凡思记录照片-'+photo.operation+'.'+(match[1]==='jpeg'?'jpg':match[1])
  const id=await rpc<number>('files','upload_bytes',[actor,name,bytes],{} as Body,photo.operation)
  if(!Number.isSafeInteger(id)||id<=0) throw Error('照片上传结果未确认，请重试。')
  return id
}
export async function loadPhoto(id:number):Promise<string> {
  const actor=actorId()
  const file=await rpc<StoredPhoto>('files','get_file',[actor,id])
  if(file.id!==id||file.user_id!==actor||!['image/jpeg','image/png','image/webp'].includes(file.media_type)||typeof file.content!=='string') throw Error('照片暂时无法读取，请重试。')
  return 'data:'+file.media_type+';base64,'+file.content
}
async function draftDb(ctx:common.Context):Promise<relationalStore.RdbStore> {
  const store=await relationalStore.getRdbStore(ctx,{name:'RecordDrafts.db',securityLevel:relationalStore.SecurityLevel.S2,encrypt:true})
  await store.executeSql('CREATE TABLE IF NOT EXISTS drafts (owner INTEGER NOT NULL, category TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(owner, category))')
  return store
}
export async function readDraft(ctx:common.Context,actor:number,cat:string):Promise<StoredDraft|undefined> {
  const db=await draftDb(ctx)
  // Large drafts include at most four photos. Return bounded cursor fields from a single SQLite snapshot.
  const query='WITH RECURSIVE pieces(pos,size) AS ('+
    'SELECT 1,length(data) FROM drafts WHERE owner=? AND category=? UNION ALL '+
    'SELECT pos+65536,size FROM pieces WHERE pos+65536<=size) '+
    'SELECT substr(d.data,p.pos,65536) FROM pieces p JOIN drafts d ON d.owner=? AND d.category=? ORDER BY p.pos'
  const result=await db.querySql(query,[actor,cat,actor,cat])
  try {
    const parts:string[]=[]
    while(result.goToNextRow()) parts.push(result.getString(0))
    return parts.length?JSON.parse(parts.join('')) as StoredDraft:undefined
  } finally { result.close() }
}
export async function writeDraft(ctx:common.Context,actor:number,cat:string,f:Fields,photos:Photo[],op:string,baseVersion?:string):Promise<void> {
  const db=await draftDb(ctx)
  const stored=photos.map((p:Photo):Photo=>({id:p.id,uri:p.id===0||p.local?p.uri:'',operation:p.operation,local:p.local}))
  const draft:StoredDraft={fields:f,photos:stored,operation:op,baseVersion:baseVersion}
  await db.executeSql('INSERT OR REPLACE INTO drafts(owner,category,data) VALUES(?,?,?)',[actor,cat,JSON.stringify(draft)])
}
export async function dropDraft(ctx:common.Context,actor:number,cat:string):Promise<void> {
  const db=await draftDb(ctx); await db.executeSql('DELETE FROM drafts WHERE owner=? AND category=?',[actor,cat])
}
