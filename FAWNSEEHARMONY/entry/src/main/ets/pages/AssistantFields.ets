import { Data, child, stringValue } from './AssistantService'
export const categoryNames: Map<string,string> = new Map([
  ['diet','饮食'],['water','饮水'],['activity','运动'],['sleep','睡眠'],['environment','居住环境'],['medical','医疗检查'],['metric','身体指标']
])
const terms:Map<string,string>=new Map([['breakfast','早餐'],['lunch','午餐'],['dinner','晚餐'],['snack','加餐'],['other','其他'],['consultation','就诊'],['medication','用药记录'],['weight','体重'],['glucose','血糖'],['blood_pressure','血压'],['lipids','血脂'],['height','身高'],['custom','自定义'],['fasting','空腹'],['before_meal','餐前'],['after_meal','餐后'],['random','随机'],['unspecified','未说明'],['morning','早上'],['forenoon','上午'],['noon','中午'],['afternoon','下午'],['evening','晚上'],['dusk','傍晚'],['night','深夜'],['dawn','凌晨']])
export class FieldRow {
  label: string
  value: string
  before: string = ''
  constructor(label:string,value:string){this.label=label;this.value=value}
}
function fields(draft:Data, category:string):FieldRow[]{
  const d=child(draft,'details')
  let date=stringValue(draft,'record_date'),time=stringValue(draft,'record_time')
  const hint=terms.get(stringValue(draft,'record_time_hint'))
  if (!time && hint) time=hint+' · 待补时间'
  if (draft['occurred_at']){
    const stamp=new Date(String(draft['occurred_at'])).getTime()
    if (Number.isFinite(stamp)){ const iso=new Date(stamp+8*3600000).toISOString(); date=iso.slice(0,10);time=iso.slice(11,16) }
  }
  const result:FieldRow[]=[new FieldRow('记录日期',date||'未填写'),new FieldRow('发生时间',time||'未填写')]
  const val=(key:string,unit:string=''):string=>{const s=stringValue(d,key);return s?(terms.get(s)||s)+unit:''}
  const add=(name:string,data:string)=>{result.push(new FieldRow(name,data||'未填写'))}
  const extras=(pairs:string[][]):string=>pairs.filter((p:string[])=>!!val(p[1],p[2])).map((p:string[])=>p[0]+val(p[1],p[2])).join('；')
  const note=val('notes')
  if (category==='diet'){
    add('餐次',val('meal_type')||val('meal'));add('吃了什么',val('food'));add('热量',val('calories_kcal',' 千卡'))
    add('其他说明',[note,extras([['重量：','amount_g',' 克'],['含糖量：','sugar_g',' 克']]),d['estimated']===true?'AI 估算':''].filter((s:string)=>s.length>0).join('；'))
  } else if(category==='water'){add('饮水量',val('amount_ml',' 毫升'));add('其他说明',note)}
  else if(category==='activity'){add('运动方式',val('activity')||val('activity_type'));add('时长',val('duration_minutes',' 分钟'));add('步数',val('steps',' 步'));add('其他说明',[note,extras([['消耗：','energy_kcal',' 千卡']])].filter((s:string)=>s.length>0).join('；'))}
  else if(category==='sleep'){add('睡眠时长',val('duration_hours',' 小时'));add('其他说明',[note,extras([['睡眠感受：','quality',' / 5']])].filter((s:string)=>s.length>0).join('；'))}
  else if(category==='environment'){add('室温',val('temperature_c',' °C'));add('湿度',val('humidity_percent','%'));add('环境情况',[val('description'),note,extras([['通风：','ventilation_minutes',' 分钟'],['环境评分：','quality_score',' / 100']])].filter((s:string)=>s.length>0).join('；'))}
  else if(category==='medical'){add('事项名称',val('name'));add('地点或机构',val('location'));add('具体事项',[val('description'),d['event_type']!=='other'?val('event_type'):''].filter((s:string)=>s.length>0).join('；'))}
  else {const kind=stringValue(d,'kind');add('身体指标类型',val('kind'));if(kind==='custom'||kind==='lipids')add(kind==='custom'?'指标名称':'指标项目',val('metric_name'));add(kind==='blood_pressure'?'收缩压数值':kind==='custom'?'数值':val('kind')+'数值',val('value',' '+val('unit')));if(kind==='blood_pressure')add('舒张压数值',val('diastolic',' '+val('unit')));if(kind==='glucose')add('测量情境',val('context'));add('其他说明',note)}
  return result
}
export function draftFields(d:Data):FieldRow[]{
  const category=stringValue(d,'category'), result=fields(d,category), old=child(child(d,'target'),'before')
  if (Object.keys(old).length) {const before=fields(old,category); result.forEach((r:FieldRow)=>{const prev=before.find((p:FieldRow)=>p.label===r.label);if(prev && prev.value!==r.value)r.before=prev.value})}
  return result
}
