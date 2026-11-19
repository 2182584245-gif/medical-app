import {http} from '@kit.NetworkKit';
import { optionalAllowed } from '../services/PrivacyChoices';
import { actorId } from '../services/Cloud';
interface Place {success:boolean;city:string;latitude:number;longitude:number;}
interface Details {air_temperature:number;}
interface Instant {details:Details;}
interface SymbolSummary {symbol_code:string;}
interface Hours {summary:SymbolSummary;}
interface ForecastData {instant:Instant;next_1_hours?:Hours;next_6_hours?:Hours;}
interface ForecastRow {time:string;data:ForecastData;}
interface ForecastProperties {timeseries:ForecastRow[];}
interface Report {properties:ForecastProperties;}
const missing='位置暂不可用 · 天气暂不可用';
let cached:string=missing;
let expires=0;
let pending:Promise<string>|null=null;
async function get<T>(url:string):Promise<T>{
  if(!optionalAllowed('weather',actorId()))throw Error('天气未开启');
  const conn=http.createHttp();
  try{
    const resp=await conn.request(url,{method:http.RequestMethod.GET,header:{'User-Agent':'FAWNSEE-Harmony/1.0 (+https://39.106.166.15/aliyun)'},connectTimeout:4000,readTimeout:6000,expectDataType:http.HttpDataType.STRING,maxLimit:2*1024*1024});
    if(resp.responseCode!==200||typeof resp.result!=='string')throw Error('天气暂不可用');
    return JSON.parse(resp.result) as T;
  }finally{conn.destroy();}
}
async function update():Promise<string>{
  let city='位置暂不可用';
  try{
    const pos=await get<Place>('https://ipwho.is/?lang=zh-CN');
    if(!pos.success||!pos.city||!Number.isFinite(pos.latitude)||!Number.isFinite(pos.longitude)||Math.abs(pos.latitude)>90||Math.abs(pos.longitude)>180)throw Error('位置暂不可用');
    city=/[\u3400-\u9fff]/.test(pos.city)?pos.city.slice(0,100):'所在地';
    const report=await get<Report>('https://api.met.no/weatherapi/locationforecast/2.0/compact?lat='+pos.latitude.toFixed(4)+'&lon='+pos.longitude.toFixed(4));
    const rows=report.properties.timeseries;
    const now=Date.now();
    const nearest=rows.reduce((a:ForecastRow,b:ForecastRow)=>Math.abs(Date.parse(a.time)-now)<=Math.abs(Date.parse(b.time)-now)?a:b);
    if(Math.abs(Date.parse(nearest.time)-now)>7200000)throw Error('天气预报已过期');
    const temp=nearest.data.instant.details.air_temperature;if(!Number.isFinite(temp)||Math.abs(temp)>100)throw Error('温度无效');
    const symbol=(nearest.data.next_1_hours||nearest.data.next_6_hours)?.summary.symbol_code.split('_')[0]||'';
    const labels=new Map<string,string>([['clearsky','晴'],['fair','晴间多云'],['partlycloudy','多云'],['cloudy','阴'],['fog','雾']]);
    const weather=labels.get(symbol)||(symbol.includes('thunder')?'雷雨':symbol.includes('snow')||symbol.includes('sleet')?'雨雪':symbol.includes('rain')?'雨':'天气状况未知');
    cached=city+' · '+weather+' · '+Math.round(temp)+'°C';expires=now+600000;
  }catch{cached=city+' · 天气暂不可用';expires=Date.now()+60000;}
  return cached;
}
export async function todayWeather():Promise<string>{
  if(!optionalAllowed('weather',actorId()))return '天气未开启';
  if(Date.now()<expires)return cached;
  if(!pending)pending=update();
  try{return await pending;}finally{pending=null;}
}
