import { cloud, rpc } from '../services/Cloud'
import { Fields, chinaDate, uuid } from './RecordsData'

interface Utterance { role: string; content: string }
interface Format { type: string }
interface Extraction { model: string; response_format: Format; messages: Utterance[] }
interface Answer { content: string }
type Details = Record<string,Object>
interface Candidate { category:string; occurred_at:string; content:string; details:Details }
interface Proposal { records:Candidate[] }
export interface VoiceRecord { category:string; stamp:string; content:string; details:Details; op:string }
const vocabulary:Fields={
  'diet':'meal meal_type food calories_kcal amount_g sugar_g notes estimated',
  'water':'amount_ml notes', 'activity':'activity activity_type duration_minutes steps energy_kcal notes',
  'sleep':'duration_hours quality notes', 'environment':'temperature_c humidity_percent ventilation_minutes quality_score description notes',
  'medical':'event_type name description location', 'metric':'kind value unit context diastolic metric_name notes'
}
export async function proposeVoiceRows(text:string):Promise<VoiceRecord[]> {
  if(!text.trim()||text.length>4000) throw Error('请先核对一至四千字的录音文字。')
  const rules='只整理用户明确描述的已发生事项。仅输出 JSON 对象 {"records":[{"category":"diet|water|activity|sleep|environment|medical|metric","occurred_at":"带时区的ISO时间","content":"简述","details":{}}]}，最多8条。不可编造未说的数值或指标，不诊断。当前北京时间 '+chinaDate()+'+08:00。各类详情字段为 '+JSON.stringify(vocabulary)+'。身体指标 kind 使用 weight/glucose/blood_pressure/lipids/height/custom。context 使用 fasting/before_meal/after_meal/random/unspecified。'
  const body:Extraction={model:'deepseek-flash',response_format:{type:'json_object'},messages:[{role:'system',content:rules},{role:'user',content:text}]}
  const answer=await cloud<Answer>('/v1/ai/chat','POST',body)
  const result=JSON.parse(answer.content) as Proposal
  if(!Array.isArray(result.records)||!result.records.length || result.records.length>8) throw Error('未整理出可保存的记录，请修改文字后再试。')
  return result.records.map((r:Candidate):VoiceRecord=>{
    if(!vocabulary[r.category]||typeof r.content!=='string'||!r.content.trim()||!r.details||typeof r.details!=='object'||
      !/([+-]\d\d:\d\d|Z)$/.test(r.occurred_at)||!chinaDate(r.occurred_at)) throw Error('整理结果缺少有效的类别、内容或发生时间，请修改原话再试。')
    const allowed=vocabulary[r.category].split(' '),d:Details={}
    Object.keys(r.details).forEach((k:string)=>{
      const v=r.details[k]
      if(allowed.includes(k)&&(typeof v==='string'||typeof v==='number'&&Number.isFinite(v)||typeof v==='boolean')) d[k]=v
    })
    if(r.category==='metric'&&(!['weight','glucose','blood_pressure','lipids','height','custom'].includes(String(d['kind']))||typeof d['value']!=='number'||typeof d['unit']!=='string')) throw Error('身体指标信息不完整，请核对数值和单位后重新整理。')
    return {category:r.category,stamp:r.occurred_at,content:r.content,details:d,op:uuid()}
  })
}
export async function saveVoiceRow(row:VoiceRecord,actor:number):Promise<void> {
  if(row.category==='metric') {
    const b:Details=JSON.parse(JSON.stringify(row.details)) as Details
    b['measured_at']=row.stamp
    await rpc<Object>('expansion','create_observation',[actor,b],{} as Details,row.op)
  } else {
    const kwargs:Details={'source':'ai_confirmed','details':row.details}
    await rpc<number>('health','add_life_record',[actor,row.category,row.stamp,row.content],kwargs,row.op)
  }
}

